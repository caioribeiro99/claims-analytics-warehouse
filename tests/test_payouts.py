"""Partner attribution and payout: flat, known-zero, revenue share, and unknown terms."""

from decimal import Decimal

import pytest

from claims_warehouse.checks import BuildError
from tests.conftest import PARTNERS, claim, lookup, row, rows


def test_payout_for_each_attribution_case(warehouse):
    fees = {"flat": 2.0, "share": 4.0, "zero": 6.0, "unattributed": 8.0, "no-terms": 3.0}
    con = warehouse(
        claims=[claim(claim_id=claim_id, service_fee=fee) for claim_id, fee in fees.items()],
        lookups=[
            lookup(lookup_id="l1", claim_id="flat", partner_code="FLAT"),
            lookup(lookup_id="l2", claim_id="share", partner_code="HALF"),
            lookup(lookup_id="l3", claim_id="zero", partner_code="ZERO"),
            lookup(lookup_id="l4", claim_id="no-terms", partner_code="UNSIGNED"),
        ],
    )
    got = {
        claim_id: rest
        for claim_id, *rest in rows(
            con, "SELECT claim_id, attribution_status, partner_payout, retained_fee FROM fct_claims"
        )
    }
    assert got == {
        "flat": ["attributed", Decimal("1"), Decimal("1")],  # 100 cents flat
        "share": ["attributed", Decimal("2"), Decimal("2")],  # 50% of 4.00
        "zero": ["attributed", Decimal("0"), Decimal("6")],  # 0 cents is a known zero
        "unattributed": ["unattributed", None, None],  # no lookup: payout unknown
        "no-terms": ["attributed_unknown_terms", None, None],  # partner not in dim_partner
    }


def test_a_flat_payout_above_the_fee_gives_a_negative_retained_fee(warehouse):
    con = warehouse(
        claims=[claim(service_fee=0)], lookups=[lookup(claim_id="c1", partner_code="FLAT")]
    )
    assert row(con, "SELECT service_fee, partner_payout, retained_fee FROM fct_claims") == (
        Decimal("0"),
        Decimal("1"),
        Decimal("-1"),
    )


def test_revenue_share_is_exact_decimal_arithmetic(warehouse):
    con = warehouse(
        claims=[claim(service_fee=2.35)],
        lookups=[lookup(claim_id="c1", partner_code="ODD")],
        partners=[*PARTNERS, ("ODD", "Odd share", "", "35.25")],
    )
    assert row(con, "SELECT partner_payout, retained_fee FROM fct_claims") == (
        Decimal("0.828375"),
        Decimal("1.521625"),
    )


def test_attribution_comes_only_from_the_lookup(warehouse):
    con = warehouse(
        claims=[claim()],
        lookups=[lookup(lookup_id="l9", claim_id="c1", partner_code="HALF", channel="api")],
    )
    assert row(con, "SELECT lookup_id, has_lookup, partner_code, channel FROM fct_claims") == (
        "l9",
        True,
        "HALF",
        "api",
    )


@pytest.mark.parametrize("terms", [("10", "5"), ("", "")])
def test_a_partner_without_exactly_one_payout_term_fails_the_build(warehouse, terms):
    with pytest.raises(BuildError, match="exactly one payout term"):
        warehouse(claims=[claim()], partners=[("BROKEN", "Broken terms", *terms)])
