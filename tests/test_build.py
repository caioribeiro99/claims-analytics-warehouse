"""Build mechanics: atomic publication, delivery-level failures, portable metadata."""

import json

import pytest

from claims_warehouse.build import build
from claims_warehouse.checks import BuildError
from tests.conftest import (
    PARTNERS,
    PRODUCTS,
    PROVIDERS,
    claim,
    cost,
    lookup,
    one,
    row,
    rows,
    write_source,
)


def test_a_failed_build_leaves_the_previous_warehouse_untouched(warehouse, tmp_path):
    warehouse(claims=[claim(claim_id="first")])
    published = tmp_path / "warehouse.duckdb"
    before = published.read_bytes()
    with pytest.raises(BuildError):
        warehouse(
            claims=[claim(claim_id="c1")],
            lookups=[lookup(lookup_id="a", claim_id="c1"), lookup(lookup_id="b", claim_id="c1")],
        )
    assert published.read_bytes() == before
    assert not list(tmp_path.glob(".warehouse.duckdb.building*"))


def test_a_file_that_is_not_json_fails_the_build(tmp_path):
    source = write_source(tmp_path, claims=[claim()])
    (source / "claims" / "claims-2.json").write_text("{not json")
    with pytest.raises(BuildError, match="not valid JSON"):
        build(source, tmp_path / "warehouse.duckdb")
    assert not (tmp_path / "warehouse.duckdb").exists()


def test_a_file_that_is_not_an_array_fails_the_build(tmp_path):
    source = write_source(tmp_path)
    (source / "lookups" / "lookups-1.json").write_text('{"lookup_id": "l1"}')
    with pytest.raises(BuildError, match="JSON array"):
        build(source, tmp_path / "warehouse.duckdb")


def test_build_metadata_holds_no_machine_specific_paths(warehouse):
    con = warehouse(claims=[claim()])
    assert row(con, "SELECT source_name, synthetic_seed FROM build_info") == ("source", None)
    assert (
        one(con, "SELECT count(*) FROM staging.cost_publications WHERE source_file LIKE '%/%'") == 0
    )


@pytest.mark.parametrize(
    ("sources", "invariant"),
    [
        ({"lookups": [lookup(lookup_id="same"), lookup(lookup_id="same")]}, "lookup_id is unique"),
        ({"costs": [cost(), cost()]}, "at most once"),
        ({"providers": [*PROVIDERS, PROVIDERS[0]]}, "provider_id is unique"),
        ({"partners": [*PARTNERS, PARTNERS[0]]}, "partner_code is unique"),
        ({"products": [*PRODUCTS, PRODUCTS[0]]}, "product_code is unique"),
        ({"providers": [*PROVIDERS, ("", "Gamma", "East")]}, "reference keys are never null"),
    ],
    ids=["lookup id", "change-point twice", "provider", "partner", "product", "blank provider key"],
)
def test_a_broken_key_fails_the_build(warehouse, sources, invariant):
    with pytest.raises(BuildError, match=invariant):
        warehouse(**sources)


@pytest.mark.parametrize(
    ("partners", "costs"),
    [
        ([("ODD", "Fractional cents", "150.7", "")], []),
        ([("ODD", "Over-precise share", "", "12.345")], []),
        ([("ODD", "Negative share", "", "-40")], []),
        ([("ODD", "Share above 100", "", "140")], []),
        (PARTNERS, [cost(unit_cost="1.234567")]),
        (PARTNERS, [cost(unit_cost="-1.00000")]),
        (PARTNERS, [cost(unit_cost="")]),
        (PARTNERS, [("PRD-0001", "", "1.50000", "2025-03-01", "2025-03-03")]),
    ],
    ids=[
        "fractional cents",
        "over-precise share",
        "negative share",
        "share above 100",
        "over-precise unit cost",
        "negative unit cost",
        "missing unit cost",
        "missing unit of measure",
    ],
)
def test_reference_numbers_that_would_round_or_are_out_of_range_fail_the_build(
    warehouse, partners, costs
):
    with pytest.raises(BuildError, match="exact"):
        warehouse(claims=[claim()], partners=partners, costs=costs)


def test_a_file_that_is_not_utf8_fails_the_build(tmp_path):
    source = write_source(tmp_path, claims=[claim()])
    (source / "claims" / "claims-2.json").write_bytes(b'[{"claim_id": "\xff"}]')
    with pytest.raises(BuildError, match="not valid JSON"):
        build(source, tmp_path / "warehouse.duckdb")


def test_invalid_unicode_in_one_record_is_a_reject_not_a_crash(warehouse):
    broken = json.dumps([claim(claim_id="c9", provider_id="\ud800")])
    con = warehouse(files={"claims/claims-2.json": broken})
    assert rows(con, "SELECT source_file, reasons FROM rejects") == [
        ("claims-2.json", ["bad_provider_id"])
    ]
    assert "\\ud800" in one(con, "SELECT raw FROM rejects")
