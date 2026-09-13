"""Effective-dated reference costs: as-of matching, no future leakage, restatements, fan-out."""

from datetime import date
from decimal import Decimal

from tests.conftest import claim, cost, one, reversal, row, rows


def test_as_of_join_uses_the_price_in_effect_at_submission(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="before", submitted_at="2025-02-20T10:00:00", units=2),
            claim(claim_id="old", submitted_at="2025-03-10T23:59:59", units=2),
            claim(claim_id="boundary", submitted_at="2025-03-11T00:00:00", units=2),
            claim(claim_id="new", submitted_at="2025-06-01T10:00:00", units=2),
        ],
        costs=[
            cost(unit_cost="1.00000", effective="2025-03-01"),
            cost(unit_cost="2.00000", effective="2025-03-11", published="2025-03-17"),
        ],
    )
    got = {
        claim_id: rest
        for claim_id, *rest in rows(
            con,
            """SELECT claim_id, reference_cost_status, reference_unit_cost,
                      reference_effective_date, reference_cost FROM fct_claims""",
        )
    }
    assert got == {
        "before": ["before_first_effective_date", None, None, None],
        "old": ["matched", Decimal("1"), date(2025, 3, 1), Decimal("2")],
        "boundary": [
            "matched",
            Decimal("2"),
            date(2025, 3, 11),
            Decimal("4"),
        ],  # effective at 00:00
        "new": ["matched", Decimal("2"), date(2025, 3, 11), Decimal("4")],
    }


def test_a_price_that_takes_effect_later_never_leaks_into_earlier_claims(warehouse):
    con = warehouse(
        claims=[claim(submitted_at="2025-04-01T10:00:00")],
        costs=[
            cost(unit_cost="1.00000", effective="2025-01-01", published="2025-03-31"),
            cost(unit_cost="9.00000", effective="2025-11-05", published="2025-03-31"),  # announced
        ],
    )
    assert row(con, "SELECT reference_unit_cost, reference_effective_date FROM fct_claims") == (
        Decimal("1"),
        date(2025, 1, 1),
    )


def test_a_restated_change_point_takes_the_latest_published_value(warehouse):
    con = warehouse(
        claims=[claim(units=1)],
        costs=[
            cost(unit_cost="1.20000", effective="2025-03-01", published="2025-03-03"),
            cost(unit_cost="1.00000", effective="2025-03-01", published="2025-03-10"),
            cost(unit_cost="1.00000", effective="2025-03-01", published="2025-03-17"),
        ],
    )
    assert row(
        con,
        "SELECT unit_cost, was_restated, publication_rows, first_published_date FROM ref_product_costs",
    ) == (Decimal("1"), True, 3, date(2025, 3, 3))
    assert one(con, "SELECT reference_unit_cost FROM fct_claims") == Decimal("1")


def test_a_product_without_cost_history_keeps_null_cost_and_says_why(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="c1", product_code="PRD-0002"),
            claim(claim_id="c2", product_code="PRD-0002"),
        ],
        costs=[cost(product_code="PRD-0001")],
        reversals=[reversal(claim_id="c2")],
    )
    assert rows(
        con,
        "SELECT claim_id, reference_cost_status, reference_cost, net_reference_cost FROM fct_claims ORDER BY 1",
    ) == [
        ("c1", "no_cost_history", None, None),  # unknown: never imputed
        ("c2", "no_cost_history", None, 0),  # reversed: a known zero
    ]


def test_a_naive_equality_join_fans_out_where_the_as_of_join_does_not(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="c1", submitted_at="2025-06-01T00:00:00"),
            claim(claim_id="c2", submitted_at="2025-06-02T00:00:00"),
        ],
        costs=[
            cost(effective="2025-01-01"),
            cost(effective="2025-02-01"),
            cost(effective="2025-03-01"),
        ],
    )
    naive = one(con, "SELECT count(*) FROM fct_claims JOIN ref_product_costs USING (product_code)")
    as_of = one(
        con,
        """SELECT count(*) FROM fct_claims AS f
           ASOF LEFT JOIN ref_product_costs AS r
             ON r.product_code = f.product_code AND f.submitted_at >= r.effective_date""",
    )
    assert (naive, as_of) == (6, 2)
    # and the model itself picked the one change-point in effect for each claim
    assert rows(con, "SELECT claim_id, reference_effective_date FROM fct_claims ORDER BY 1") == [
        ("c1", date(2025, 3, 1)),
        ("c2", date(2025, 3, 1)),
    ]


def test_the_as_of_join_equals_a_validity_interval_range_join(warehouse):
    moments = [
        "2025-01-01T00:00:00",
        "2025-02-15T08:00:00",
        "2025-03-01T00:00:00",
        "2025-04-20T12:00:00",
    ]
    con = warehouse(
        claims=[claim(claim_id=f"c{i}", submitted_at=moment) for i, moment in enumerate(moments)],
        costs=[
            cost(unit_cost="1.00000", effective="2025-01-10"),
            cost(unit_cost="2.00000", effective="2025-03-01"),
            cost(unit_cost="3.00000", effective="2025-04-01"),
        ],
    )
    differences = one(
        con,
        """SELECT count(*) FROM fct_claims AS f
           LEFT JOIN ref_product_costs AS r
             ON r.product_code = f.product_code
            AND f.submitted_at >= r.effective_date
            AND (r.next_effective_date IS NULL OR f.submitted_at < r.next_effective_date)
           WHERE f.reference_effective_date IS DISTINCT FROM r.effective_date""",
    )
    assert differences == 0
