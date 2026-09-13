"""Claim scope: out-of-network providers, ambiguous-id quarantine, broken redeliveries, fan-out."""

import pytest

from claims_warehouse.checks import BuildError
from tests.conftest import claim, lookup, one, reversal, rows


def test_out_of_network_claims_are_excluded_but_references_to_them_resolve(warehouse):
    con = warehouse(
        claims=[claim(claim_id="in"), claim(claim_id="out", provider_id="PRV99999")],
        lookups=[lookup(claim_id="out")],
        reversals=[reversal(claim_id="out")],
    )
    assert rows(con, "SELECT claim_id FROM fct_claims") == [("in",)]
    assert one(con, "SELECT reasons FROM rejects") == ["out_of_network"]
    assert one(con, "SELECT resolution_status FROM fct_lookups") == "claim_out_of_network"
    assert one(con, "SELECT resolution_status FROM fct_reversals") == "claim_out_of_network"


def test_an_out_of_network_claim_that_is_also_broken_carries_both_reasons(warehouse):
    con = warehouse(claims=[claim(provider_id="PRV99999", units=0)])
    assert sorted(one(con, "SELECT reasons FROM rejects")) == [
        "nonpositive_units",
        "out_of_network",
    ]


def test_colliding_claim_ids_are_quarantined_as_a_whole_group(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="dup", gross_amount=10.0),
            claim(claim_id="dup", gross_amount=99.0, provider_id="PRV10002"),
            claim(claim_id="dup", provider_id="PRV99999"),
            claim(claim_id="c2"),
        ],
        lookups=[lookup(claim_id="dup")],
        reversals=[reversal(claim_id="dup")],
    )
    assert rows(con, "SELECT claim_id FROM fct_claims") == [("c2",)]
    reasons = sorted(sorted(r) for (r,) in rows(con, "SELECT reasons FROM rejects"))
    assert reasons == [
        ["ambiguous_duplicate_id"],
        ["ambiguous_duplicate_id"],
        ["ambiguous_duplicate_id", "out_of_network"],
    ]
    assert one(con, "SELECT resolution_status FROM fct_lookups") == "claim_ambiguous_id"
    assert one(con, "SELECT resolution_status FROM fct_reversals") == "claim_ambiguous_id"


def test_a_broken_redelivery_does_not_quarantine_the_valid_original(warehouse):
    con = warehouse(
        claims=[claim(claim_id="c1"), claim(claim_id="c1", units="ten")],
        lookups=[lookup(claim_id="c1")],
    )
    assert rows(con, "SELECT claim_id FROM fct_claims") == [("c1",)]
    assert one(con, "SELECT reasons FROM rejects") == ["bad_units"]
    assert one(con, "SELECT resolution_status FROM fct_lookups") == "resolved"


def test_two_valid_lookups_claiming_one_claim_fail_the_build(warehouse):
    with pytest.raises(BuildError, match="fan out"):
        warehouse(
            claims=[claim(claim_id="c1")],
            lookups=[
                lookup(lookup_id="l1", claim_id="c1"),
                lookup(lookup_id="l2", claim_id="c1", partner_code="HALF"),
            ],
        )


def test_an_identical_redelivery_keeps_the_first_copy_and_rejects_the_rest(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="c1", units=30),
            claim(claim_id="c1", units=30.0),
            claim(claim_id="c2"),
        ],
        lookups=[lookup(claim_id="c1")],
        reversals=[reversal(claim_id="c1")],
    )
    assert rows(con, "SELECT claim_id, is_reversed FROM fct_claims ORDER BY 1") == [
        ("c1", True),
        ("c2", False),
    ]
    assert rows(con, "SELECT source_row, reasons FROM rejects") == [(2, ["duplicate_redelivery"])]
    assert one(con, "SELECT resolution_status FROM fct_lookups") == "resolved"


def test_ambiguity_counts_copies_from_any_provider_even_out_of_network(warehouse):
    con = warehouse(claims=[claim(claim_id="dup"), claim(claim_id="dup", provider_id="PRV99999")])
    assert one(con, "SELECT count(*) FROM fct_claims") == 0
    reasons = sorted(sorted(r) for (r,) in rows(con, "SELECT reasons FROM rejects"))
    assert reasons == [["ambiguous_duplicate_id"], ["ambiguous_duplicate_id", "out_of_network"]]


def test_out_of_network_takes_precedence_over_a_broken_copy(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="far", provider_id="PRV99999"),
            claim(claim_id="far", provider_id="PRV99999", units="ten"),
        ],
        lookups=[lookup(claim_id="far")],
        reversals=[reversal(claim_id="far")],
    )
    reasons = sorted(sorted(r) for (r,) in rows(con, "SELECT reasons FROM rejects"))
    assert reasons == [["bad_units", "out_of_network"], ["out_of_network"]]
    assert one(con, "SELECT resolution_status FROM fct_lookups") == "claim_out_of_network"
    assert one(con, "SELECT resolution_status FROM fct_reversals") == "claim_out_of_network"


def test_an_ambiguous_id_with_a_broken_copy_stays_ambiguous(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="dup", gross_amount=10.0),
            claim(claim_id="dup", gross_amount=11.0),
            claim(claim_id="dup", units="ten"),
        ],
        lookups=[lookup(claim_id="dup")],
        reversals=[reversal(claim_id="dup")],
    )
    reasons = sorted(sorted(r) for (r,) in rows(con, "SELECT reasons FROM rejects"))
    assert reasons == [["ambiguous_duplicate_id"], ["ambiguous_duplicate_id"], ["bad_units"]]
    assert one(con, "SELECT resolution_status FROM fct_lookups") == "claim_ambiguous_id"
    assert one(con, "SELECT resolution_status FROM fct_reversals") == "claim_ambiguous_id"
