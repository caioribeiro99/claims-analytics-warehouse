"""Test fixtures.

`warehouse` builds a real warehouse from a handful of inline records, through the
same code path as `make build`. The tests therefore assert the behavior of the
shipped SQL, not of a mock. `synthetic_build` generates the full deterministic
dataset once per session, builds it, and exposes the generator's own
expectations for end-to-end comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from claims_warehouse import synthetic
from claims_warehouse.build import build

PROVIDERS = [("PRV10001", "Alpha", "North"), ("PRV10002", "Beta", "South")]
PARTNERS = [
    ("FLAT", "Flat partner", "100", ""),  # flat $1.00 per claim
    ("ZERO", "Zero partner", "0", ""),  # flat $0.00: a real, known-zero term
    ("HALF", "Half partner", "", "50"),  # 50% of the service fee
]
PRODUCTS = [
    ("PRD-0001", "Test tablet", "generic", "EA"),
    ("PRD-0002", "Test solution", "brand", "ML"),
]

DROP = object()  # a record key set to DROP is removed from the payload


def _record(defaults: dict, overrides: dict) -> dict:
    merged = {**defaults, **overrides}
    return {key: value for key, value in merged.items() if value is not DROP}


def claim(**overrides) -> dict:
    defaults = {
        "claim_id": "c1",
        "provider_id": "PRV10001",
        "product_code": "PRD-0001",
        "gross_amount": 10.0,
        "units": 5,
        "service_fee": 2.0,
        "submitted_at": "2025-04-01T10:00:00",
    }
    return _record(defaults, overrides)


def lookup(**overrides) -> dict:
    defaults = {
        "lookup_id": "l1",
        "claim_id": None,
        "product_code": "PRD-0001",
        "partner_code": "FLAT",
        "channel": "web",
        "looked_up_at": "2025-04-01T09:30:00",
    }
    return _record(defaults, overrides)


def reversal(**overrides) -> dict:
    defaults = {"reversal_id": "r1", "claim_id": "c1", "reversed_at": "2025-04-05T10:00:00"}
    return _record(defaults, overrides)


def cost(
    product_code="PRD-0001", unit_cost="1.50000", effective="2025-03-01", published="2025-03-03"
):
    return (product_code, "EA", unit_cost, effective, published)


def one(con, sql: str, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def row(con, sql: str, params=None):
    return con.execute(sql, params or []).fetchone()


def rows(con, sql: str, params=None):
    return con.execute(sql, params or []).fetchall()


def _write_csv(path: Path, header: list[str], records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(",".join(record) + "\n" for record in records)
    path.write_text(",".join(header) + "\n" + body)


def write_source(
    root: Path,
    claims=(),
    lookups=(),
    reversals=(),
    costs=(),
    partners=PARTNERS,
    providers=PROVIDERS,
    products=PRODUCTS,
    files=None,
):
    """Write a complete source tree; `files` adds raw text files (path -> content) for
    deliveries that json.dumps cannot produce, such as repeated keys."""
    source = root / "source"
    for stream, records in (("claims", claims), ("lookups", lookups), ("reversals", reversals)):
        (source / stream).mkdir(parents=True, exist_ok=True)
        (source / stream / f"{stream}-1.json").write_text(json.dumps(list(records)))
    _write_csv(
        source / "providers" / "providers.csv", ["provider_id", "network", "region"], providers
    )
    _write_csv(
        source / "partners" / "partners.csv",
        ["partner_code", "partner_name", "flat_payout_cents", "revenue_share_pct"],
        partners,
    )
    _write_csv(
        source / "products" / "products.csv",
        ["product_code", "product_name", "category", "unit_of_measure"],
        products,
    )
    _write_csv(
        source / "reference_costs" / "publication-1.csv",
        ["product_code", "unit_of_measure", "unit_cost", "effective_date", "published_date"],
        costs,
    )
    for relative, content in (files or {}).items():
        (source / relative).write_text(content)
    return source


@pytest.fixture
def warehouse(tmp_path):
    connections = []

    def make(**sources):
        db = build(write_source(tmp_path, **sources), tmp_path / "warehouse.duckdb")
        con = duckdb.connect(str(db), read_only=True)
        connections.append(con)
        return con

    yield make
    for con in connections:
        con.close()


@pytest.fixture(scope="session")
def synthetic_build(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic")
    expected = synthetic.generate(root / "source")
    db = build(root / "source", root / "warehouse.duckdb")
    con = duckdb.connect(str(db), read_only=True)
    yield SimpleNamespace(source=root / "source", expected=expected, con=con)
    con.close()
