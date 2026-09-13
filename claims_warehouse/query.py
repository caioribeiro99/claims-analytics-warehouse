"""Run SQL against the warehouse, read-only.

    python -m claims_warehouse.query analyses/01_network_volume.sql [more.sql ...]
    python -m claims_warehouse.query -c "SELECT count(*) FROM fct_claims"

A statement ends on a line whose code (comments aside) ends with ';', which is
enough for the SQL in this repository. Comment lines at the top of a file are
printed as its header, so an analysis reads as a narrated result.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from claims_warehouse.build import DEFAULT_DB


def statements(sql: str):
    buffer: list[str] = []
    for line in sql.splitlines():
        buffer.append(line)
        if line.split("--")[0].rstrip().endswith(";"):
            yield "\n".join(buffer)
            buffer = []
    if any(line.split("--")[0].strip() for line in buffer):
        yield "\n".join(buffer)


def header(sql: str) -> list[str]:
    lines = []
    for line in sql.splitlines():
        if not line.startswith("--"):
            break
        lines.append(line)
    return lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run SQL against the warehouse (read-only).")
    parser.add_argument("sql_files", nargs="*", type=Path, help="files of ;-separated statements")
    parser.add_argument("-c", "--command", help="an inline SQL statement")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--max-rows", type=int, default=40)
    args = parser.parse_args(argv)
    if not (args.command or args.sql_files):
        parser.error("give one or more SQL files, or -c 'SQL'")
    if not args.db.exists():
        sys.exit(f"{args.db} not found - run `make build` first")

    with duckdb.connect(str(args.db), read_only=True) as con:
        sources = [(None, args.command)] if args.command else []
        sources += [(path, path.read_text(encoding="utf-8")) for path in args.sql_files]
        for path, sql in sources:
            if path is not None:
                print(f"\n=== {path}")
                print("\n".join(header(sql)))
            for statement in statements(sql):
                relation = con.sql(statement)
                if relation is not None:
                    relation.show(max_width=180, max_rows=args.max_rows)


if __name__ == "__main__":
    main()
