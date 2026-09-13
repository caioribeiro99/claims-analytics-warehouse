"""Reversal semantics: net fields zero exactly once, events stay observable."""

from decimal import Decimal

from tests.conftest import claim, lookup, one, reversal, row, rows


def test_a_reversal_zeroes_net_fields_and_keeps_the_submitted_values(warehouse):
    con = warehouse(
        claims=[claim(gross_amount=10.0, units=5, service_fee=2.0)],
        lookups=[lookup(claim_id="c1", partner_code="FLAT")],
        reversals=[reversal()],
    )
    submitted = row(
        con,
        "SELECT is_reversed, gross_amount, service_fee, partner_payout, retained_fee FROM fct_claims",
    )
    assert submitted == (True, Decimal("10"), Decimal("2"), Decimal("1"), Decimal("1"))
    net = row(
        con,
        """SELECT is_net_claim, net_gross_amount, net_units, net_service_fee,
                  net_partner_payout, net_retained_fee FROM fct_claims""",
    )
    assert net == (0, 0, 0, 0, 0, 0)


def test_several_reversals_reverse_a_claim_once_dated_by_the_earliest(warehouse):
    con = warehouse(
        claims=[claim()],
        reversals=[
            reversal(reversal_id="late", reversed_at="2025-04-09T00:00:00"),
            reversal(reversal_id="early", reversed_at="2025-04-03T00:00:00"),
        ],
    )
    assert one(con, "SELECT count(*) FROM fct_claims") == 1
    assert str(one(con, "SELECT reversed_at FROM fct_claims")) == "2025-04-03 00:00:00"
    assert rows(
        con, "SELECT reversal_id, resolution_status FROM fct_reversals ORDER BY reversal_sk"
    ) == [
        ("early", "applied"),
        ("late", "applied"),
    ]


def test_reused_reversal_ids_keep_both_events_under_a_surrogate_key(warehouse):
    con = warehouse(
        claims=[claim(claim_id="c1"), claim(claim_id="c2")],
        reversals=[
            reversal(reversal_id="same", claim_id="c1"),
            reversal(reversal_id="same", claim_id="c2", reversed_at="2025-04-06T00:00:00"),
        ],
    )
    assert rows(con, "SELECT reversal_sk, reversal_id, claim_id FROM fct_reversals ORDER BY 1") == [
        (1, "same", "c1"),
        (2, "same", "c2"),
    ]
    assert one(con, "SELECT count(*) FROM fct_claims WHERE is_reversed") == 2


def test_an_orphan_reversal_stays_visible_and_changes_nothing(warehouse):
    con = warehouse(claims=[claim()], reversals=[reversal(claim_id="never-existed")])
    assert one(con, "SELECT is_reversed FROM fct_claims") is False
    assert one(con, "SELECT resolution_status FROM fct_reversals") == "claim_not_found"


def test_a_reversal_of_a_rejected_claim_resolves_to_a_status(warehouse):
    con = warehouse(
        claims=[claim(claim_id="broken", units="ten")], reversals=[reversal(claim_id="broken")]
    )
    assert one(con, "SELECT resolution_status FROM fct_reversals") == "claim_rejected"


def test_reversed_claim_with_unknown_values_contributes_a_known_zero(warehouse):
    con = warehouse(claims=[claim(product_code="PRD-0002")], reversals=[reversal()])
    assert row(
        con,
        "SELECT partner_payout, reference_cost, net_partner_payout, net_retained_fee, net_reference_cost FROM fct_claims",
    ) == (None, None, 0, 0, 0)


def test_net_sums_equal_the_exclude_reversed_formulation(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="a", service_fee=2.0),
            claim(claim_id="b", service_fee=3.0),
            claim(claim_id="c", service_fee=5.0),  # unattributed: retained fee unknown
        ],
        lookups=[
            lookup(lookup_id="la", claim_id="a", partner_code="HALF"),
            lookup(lookup_id="lb", claim_id="b", partner_code="HALF"),
        ],
        reversals=[reversal(claim_id="b")],
    )
    net, filtered = row(
        con,
        "SELECT sum(net_retained_fee), sum(retained_fee) FILTER (WHERE NOT is_reversed) FROM fct_claims",
    )
    assert net == filtered == Decimal("1")
