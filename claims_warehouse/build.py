"""Build the warehouse from a source directory: full reload, atomic swap.

    python -m claims_warehouse.build [--source data/generated] [--db warehouse/claims_warehouse.duckdb]

1. Parse every event file exactly (numbers as Decimal, repeated keys remembered),
   apply the record contracts and load typed raw staging (Python).
2. Run the SQL model in sql/ in file-name order: reference data, the
   contract-valid subsets, rejects, reference costs, the three facts, audit views.
3. Assert the build invariants (claims_warehouse/checks.py).
4. Only when every check passes, atomically replace the previous warehouse file.
   A failed build never destroys a good warehouse, and a reader never sees a
   half-built one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from claims_warehouse import checks, validate

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
DEFAULT_SOURCE = Path("data/generated")
DEFAULT_DB = Path("warehouse/claims_warehouse.duckdb")

EVENT_STREAMS = (
    ("claims", validate.validate_claim),
    ("lookups", validate.validate_lookup),
    ("reversals", validate.validate_reversal),
)
BuildError = checks.BuildError


class JsonObject(dict):
    """A parsed JSON object that keeps its key/value pairs in delivery order.

    A plain dict silently keeps the last value of a repeated key. Remembering the
    pairs lets the contracts reject the record and keeps the repetition visible
    in the stored payload.
    """

    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        super().__init__(pairs)
        self.pairs = pairs
        self.duplicate_keys: set[str] = set()
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                self.duplicate_keys.add(key)
            seen.add(key)


class JsonNumber(Decimal):
    """A JSON number parsed exactly that also remembers its delivered literal, so 1.5e1 is
    validated as 15 but stored as 1.5e1."""

    def __new__(cls, literal: str) -> JsonNumber:
        number = super().__new__(cls, literal)
        number.literal = literal
        return number


def parse_json(text: str):
    """Parse a delivery exactly: every number becomes a JsonNumber, never a binary float."""
    return json.loads(
        text, parse_float=JsonNumber, parse_int=JsonNumber, object_pairs_hook=JsonObject
    )


def to_json(value) -> str:
    """Compact JSON for the stored payload.

    Numbers keep the digits they were delivered with and repeated keys stay
    visible, so `raw` shows what the producer sent (whitespace aside).
    """
    if isinstance(value, dict):
        pairs = value.pairs if isinstance(value, JsonObject) else value.items()
        return "{" + ",".join(f"{json.dumps(key)}:{to_json(item)}" for key, item in pairs) + "}"
    if isinstance(value, list):
        return "[" + ",".join(to_json(item) for item in value) + "]"
    if isinstance(value, JsonNumber):
        return value.literal
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value)


def sql_literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def read_events(directory: Path, validator):
    """Yield one typed staging row per record, across every *.json file in name order."""
    files = sorted(directory.glob("*.json"))
    if not files:
        raise BuildError(f"no .json files in {directory}")
    record_seq = 0
    for path in files:
        try:
            records = parse_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:  # invalid JSON or UTF-8, or an unparseable number literal
            raise BuildError(f"{path.name} is not valid JSON: {exc}") from exc
        if not isinstance(records, list):
            raise BuildError(f"{path.name} must contain a JSON array of records")
        for source_row, record in enumerate(records, start=1):
            typed, reasons = validator(record)
            yield {
                "record_seq": record_seq,
                "source_file": path.name,
                "source_row": source_row,
                # exact decimal text; DuckDB casts it into the DECIMAL columns
                **{k: format(v, "f") if isinstance(v, Decimal) else v for k, v in typed.items()},
                "schema_reasons": reasons,
                "raw": to_json(record),
            }
            record_seq += 1


def load_stream(con, stream: str, directory: Path, validator, workdir: Path) -> None:
    table = f"{stream}_raw"
    columns = con.execute(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'staging' AND table_name = ?
           ORDER BY ordinal_position""",
        [table],
    ).fetchall()
    ndjson = workdir / f"{stream}.ndjson"
    rows = 0
    with ndjson.open("w", encoding="utf-8") as out:
        for row in read_events(directory, validator):
            out.write(json.dumps(row) + "\n")  # ASCII-escaped, so any text is writable
            rows += 1
    if not rows:
        return
    types = ", ".join(f"{sql_literal(name)}: {sql_literal(dtype)}" for name, dtype in columns)
    con.execute(
        f"""INSERT INTO staging.{table} BY NAME
            SELECT * FROM read_json({sql_literal(ndjson)},
                                    format = 'newline_delimited', columns = {{{types}}})"""
    )


def run_sql_file(con, path: Path, tokens: dict[str, str]) -> None:
    sql = path.read_text(encoding="utf-8")
    for token, value in tokens.items():
        sql = sql.replace(token, value)
    try:
        con.execute(sql)
    except duckdb.Error as exc:
        raise BuildError(f"{path.name}: {exc}") from exc


def build(source: Path, db: Path) -> Path:
    """Build a warehouse from `source` and publish it at `db`. Returns the path."""
    source, db = Path(source), Path(db)
    if not source.is_dir():
        raise BuildError(f"source directory {source} not found (run `make data`)")
    db.parent.mkdir(parents=True, exist_ok=True)
    building = db.with_name(f".{db.name}.building")
    building.unlink(missing_ok=True)
    building.with_name(building.name + ".wal").unlink(missing_ok=True)

    tokens = {
        "__PROVIDERS__": sql_literal(source / "providers" / "*.csv"),
        "__PARTNERS__": sql_literal(source / "partners" / "*.csv"),
        "__PRODUCTS__": sql_literal(source / "products" / "*.csv"),
        "__COST_PUBLICATIONS__": sql_literal(source / "reference_costs" / "*.csv"),
    }
    sql_files = sorted(SQL_DIR.glob("*.sql"))
    con = duckdb.connect(str(building))
    try:
        run_sql_file(con, sql_files[0], tokens)  # 00: staging schema
        with tempfile.TemporaryDirectory(prefix="claims-build-") as workdir:
            for stream, validator in EVENT_STREAMS:
                load_stream(con, stream, source / stream, validator, Path(workdir))
        for path in sql_files[1:]:
            run_sql_file(con, path, tokens)
        record_build_info(con, source)
        checks.assert_invariants(con)
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        building.unlink(missing_ok=True)
        raise
    con.close()
    os.replace(building, db)
    return db


def record_build_info(con, source: Path) -> None:
    expected = source / "_expected.json"
    seed = json.loads(expected.read_text())["seed"] if expected.exists() else None
    con.execute(
        """CREATE TABLE build_info AS
           SELECT ?::TIMESTAMP AS built_at_utc, ? AS source_name,
                  ?::BIGINT AS synthetic_seed, ? AS duckdb_version""",
        [datetime.now(UTC).replace(tzinfo=None), source.name, seed, duckdb.__version__],
    )


def print_summary(con) -> None:
    print("\n== reconciliation: source = curated + rejected ==========================")
    for stream, source, curated, rejected, balanced in con.execute(
        "SELECT * FROM audit_reconciliation"
    ).fetchall():
        state = "ok" if balanced else "UNBALANCED"
        print(f"  {stream:<9} {source:>8,} = {curated:>8,} + {rejected:>5,}   {state}")
        reasons = con.execute(
            "SELECT reason, records FROM audit_reject_reasons WHERE stream = ?", [stream]
        ).fetchall()
        print("            reasons (overlapping): " + ", ".join(f"{r} {n}" for r, n in reasons))

    claims, reversal_pct, fee, payout, retained, unknown_payout_fee = con.execute(
        """SELECT count(*),
                  round(100.0 * count(*) FILTER (is_reversed) / count(*), 2),
                  coalesce(sum(net_service_fee), 0),
                  coalesce(sum(net_partner_payout), 0),
                  coalesce(sum(net_retained_fee), 0),
                  coalesce(sum(net_service_fee) FILTER (attribution_status <> 'attributed'), 0)
           FROM fct_claims"""
    ).fetchone()
    recorded, resolved, in_scope = con.execute(
        """SELECT round(100.0 * count(*) FILTER (has_claim_reference) / count(*), 2),
                  round(100.0 * count(*) FILTER (claim_resolved) / count(*), 2),
                  round(100.0 * count(*) FILTER (in_scope_conversion) / count(*), 2)
           FROM fct_lookups"""
    ).fetchone()
    coverage = con.execute(
        """SELECT reference_cost_status, count(*) FROM fct_claims
           GROUP BY ALL ORDER BY 2 DESC"""
    ).fetchall()
    print("\n== headline ===============================================================")
    print(f"  in-scope claims {claims:,} | reversed {reversal_pct}%")
    print(f"  conversion  recorded {recorded}% | resolved {resolved}% | in scope {in_scope}%")
    print(f"  net service fee ${fee:,.2f}")
    print(
        f"    = partner payout ${payout:,.2f} + retained fee ${retained:,.2f}"
        f" + fee with unknown payout ${unknown_payout_fee:,.2f}"
    )
    print("  reference cost  " + " | ".join(f"{status} {n:,}" for status, n in coverage))


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    try:
        db = build(args.source, args.db)
    except BuildError as exc:
        sys.exit(f"BUILD FAILED - {exc}")
    with duckdb.connect(str(db), read_only=True) as con:
        print_summary(con)
    print(f"\nwarehouse ready: {_display(db)}")


if __name__ == "__main__":
    main()
