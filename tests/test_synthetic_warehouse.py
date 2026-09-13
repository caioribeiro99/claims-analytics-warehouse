"""End to end on the full synthetic dataset.

The generator records, from how it built each record, what a correct warehouse
must contain (`_expected.json`). These tests compare the warehouse against that
independent account. Expected values are derived, never hand-copied, so an
intentional generator change needs no test edits.
"""

from decimal import Decimal
from pathlib import Path

from tests.conftest import one, rows

ROOT = Path(__file__).resolve().parent.parent


def test_every_stream_reconciles_to_the_expected_counts(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    got = {stream: rest for stream, *rest in rows(con, "SELECT * FROM audit_reconciliation")}
    assert got == {
        stream: [c["source_records"], c["curated_records"], c["rejected_records"], True]
        for stream, c in expected["streams"].items()
    }


def test_every_rejected_record_carries_exactly_the_expected_reasons(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    for stream, expected_rejects in expected["rejects"].items():
        got = {
            f"{source_file}:{source_row}": sorted(reasons)
            for source_file, source_row, reasons in rows(
                con,
                "SELECT source_file, source_row, reasons FROM rejects WHERE stream = ?",
                [stream],
            )
        }
        assert got == expected_rejects, stream


def test_each_fact_has_the_generators_row_count(synthetic_build):
    # Keys and grain are build invariants (checks.py). This compares row counts with the
    # generator's account rather than with the model's own scope definition.
    con, expected = synthetic_build.con, synthetic_build.expected
    streams = expected["streams"]
    assert one(con, "SELECT count(*) FROM fct_claims") == streams["claims"]["curated_records"]
    assert one(con, "SELECT count(*) FROM fct_lookups") == streams["lookups"]["curated_records"]
    assert one(con, "SELECT count(*) FROM fct_reversals") == streams["reversals"]["curated_records"]
    assert (
        one(con, "SELECT count(*) FROM staging.ambiguous_claim_ids")
        == expected["claims"]["ambiguous_claim_ids"]
    )


def test_resolution_statuses_match_record_by_record(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    assert (
        dict(rows(con, "SELECT resolution_status, count(*) FROM fct_lookups GROUP BY 1"))
        == (expected["lookup_status"])
    )
    assert (
        dict(rows(con, "SELECT resolution_status, count(*) FROM fct_reversals GROUP BY 1"))
        == (expected["reversal_status"])
    )
    lookup_exceptions = {
        f"{source_file}:{source_row}": status
        for source_file, source_row, status in rows(
            con,
            """SELECT s.source_file, s.source_row, f.resolution_status
               FROM fct_lookups AS f JOIN staging.lookups AS s USING (lookup_id)
               WHERE f.resolution_status NOT IN ('no_claim', 'resolved')""",
        )
    }
    assert lookup_exceptions == expected["lookup_exceptions"]
    reversal_exceptions = {
        f"{source_file}:{source_row}": status
        for source_file, source_row, status in rows(
            con,
            """SELECT source_file, source_row, resolution_status FROM fct_reversals
               WHERE resolution_status <> 'applied'""",
        )
    }
    assert reversal_exceptions == expected["reversal_exceptions"]


def test_conversion_flags_are_nested_and_match(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    assert (
        one(
            con,
            """SELECT count(*) FROM fct_lookups
               WHERE (claim_resolved AND NOT has_claim_reference)
                  OR (in_scope_conversion AND NOT claim_resolved)""",
        )
        == 0
    )
    got = rows(
        con,
        """SELECT count(*), count(*) FILTER (WHERE has_claim_reference),
                  count(*) FILTER (WHERE claim_resolved), count(*) FILTER (WHERE in_scope_conversion)
           FROM fct_lookups""",
    )[0]
    c = expected["conversion"]
    assert got == (
        c["valid_lookups"],
        c["has_claim_reference"],
        c["claim_resolved"],
        c["in_scope_conversion"],
    )


def test_attribution_is_consistent_with_the_lookup_fact(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    inconsistent = one(
        con,
        """SELECT count(*) FROM fct_claims AS f
           LEFT JOIN fct_lookups AS l ON l.lookup_id = f.lookup_id
           WHERE f.has_lookup AND (l.claim_id IS DISTINCT FROM f.claim_id
                                   OR l.partner_code IS DISTINCT FROM f.partner_code
                                   OR l.channel IS DISTINCT FROM f.channel
                                   OR NOT l.in_scope_conversion)""",
    )
    assert inconsistent == 0
    assert one(con, "SELECT count(*) FROM fct_lookups WHERE in_scope_conversion") == one(
        con, "SELECT count(*) FROM fct_claims WHERE has_lookup"
    )
    statuses = dict(rows(con, "SELECT attribution_status, count(*) FROM fct_claims GROUP BY 1"))
    claims = expected["claims"]
    assert statuses == {
        "attributed": claims["attributed"],
        "attributed_unknown_terms": claims["attributed_unknown_terms"],
        "unattributed": claims["unattributed"],
    }


def test_reversal_semantics_hold_for_every_claim(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    assert (
        one(con, "SELECT count(*) FROM fct_claims WHERE is_reversed")
        == expected["claims"]["reversed"]
    )
    assert (
        one(
            con,
            """SELECT count(*) FROM fct_claims AS f
               WHERE f.reversed_at IS DISTINCT FROM (
                   SELECT min(r.reversed_at) FROM fct_reversals AS r
                   WHERE r.claim_id = f.claim_id AND r.resolution_status = 'applied')""",
        )
        == 0
    )
    assert (
        one(
            con,
            """SELECT count(*) FROM fct_claims
               WHERE is_reversed AND (is_net_claim <> 0 OR net_gross_amount <> 0 OR net_units <> 0
                     OR net_service_fee <> 0 OR net_partner_payout IS DISTINCT FROM 0
                     OR net_retained_fee IS DISTINCT FROM 0 OR net_reference_cost IS DISTINCT FROM 0)""",
        )
        == 0
    )


def test_null_means_unknown_and_zero_means_known_zero(synthetic_build):
    con = synthetic_build.con
    assert (
        one(
            con,
            "SELECT count(*) FROM fct_claims WHERE (partner_payout IS NULL) <> (attribution_status <> 'attributed')",
        )
        == 0
    )
    assert (
        one(
            con,
            "SELECT count(*) FROM fct_claims WHERE (reference_cost IS NULL) <> (reference_cost_status <> 'matched')",
        )
        == 0
    )
    zero_term_claims, zero_payouts = rows(
        con,
        """SELECT count(*), count(*) FILTER (WHERE f.partner_payout = 0)
           FROM fct_claims AS f JOIN dim_partner AS p ON p.partner_code = f.partner_code
           WHERE p.flat_payout_cents = 0""",
    )[0]
    assert zero_term_claims > 0 and zero_payouts == zero_term_claims
    # on a claim that was not reversed, net_* passes the submitted value through, NULL included
    passthrough_violations = one(
        con,
        """SELECT count(*) FROM fct_claims
           WHERE NOT is_reversed
             AND ((partner_payout IS NULL) <> (net_partner_payout IS NULL)
                  OR (retained_fee IS NULL) <> (net_retained_fee IS NULL)
                  OR (reference_cost IS NULL) <> (net_reference_cost IS NULL))""",
    )
    assert passthrough_violations == 0


def test_economics_match_to_the_last_decimal(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    names = [
        "net_gross_amount",
        "net_service_fee",
        "net_partner_payout",
        "net_retained_fee",
        "unknown_payout_net_service_fee",
        "net_reference_cost",
    ]
    got = rows(
        con,
        """SELECT sum(net_gross_amount), sum(net_service_fee), sum(net_partner_payout),
                  sum(net_retained_fee),
                  sum(net_service_fee) FILTER (WHERE attribution_status <> 'attributed'),
                  sum(net_reference_cost)
           FROM fct_claims""",
    )[0]
    for name, value in zip(names, got, strict=True):
        assert value == Decimal(expected["economics"][name]), name
    _, fee, payout, retained, unknown_payout_fee, _ = got
    assert fee == payout + retained + unknown_payout_fee


def test_reference_costs_are_as_of_with_no_future_leakage(synthetic_build):
    con, expected = synthetic_build.con, synthetic_build.expected
    change_points = [
        {
            "product_code": product_code,
            "effective_date": effective_date.isoformat(),
            "unit_cost": f"{unit_cost:.5f}",
            "was_restated": was_restated,
        }
        for product_code, effective_date, unit_cost, was_restated in rows(
            con,
            """SELECT product_code, effective_date, unit_cost, was_restated
               FROM ref_product_costs ORDER BY product_code, effective_date""",
        )
    ]
    assert change_points == sorted(
        expected["reference_costs"]["change_points"],
        key=lambda point: (point["product_code"], point["effective_date"]),
    )
    assert (
        dict(rows(con, "SELECT reference_cost_status, count(*) FROM fct_claims GROUP BY 1"))
        == (expected["claims"]["reference_cost_status"])
    )
    assert (
        one(con, "SELECT count(*) FROM fct_claims WHERE reference_effective_date > submitted_at")
        == 0
    )
    # no later change-point was already in effect when the claim was submitted
    assert (
        one(
            con,
            """SELECT count(*) FROM fct_claims AS f
               JOIN ref_product_costs AS r
                 ON r.product_code = f.product_code
                AND r.effective_date <= f.submitted_at
                AND r.effective_date > f.reference_effective_date""",
        )
        == 0
    )


def test_every_example_analysis_runs_and_returns_rows(synthetic_build):
    analyses = sorted((ROOT / "analyses").glob("*.sql"))
    assert len(analyses) >= 10
    for path in analyses:
        assert synthetic_build.con.sql(path.read_text()).fetchall(), path.name


def test_redeliveries_and_out_of_order_events_are_counted_not_repaired(synthetic_build):
    con, injected = synthetic_build.con, synthetic_build.expected["injected"]
    assert one(con, "SELECT count(*) FROM staging.redelivered_claims") == (
        injected["claims"]["exact_redelivery"]
        + injected["claims"]["exact_redelivery_out_of_network"]
    )
    reversed_before_claim = one(
        con,
        """SELECT count(*) FROM fct_reversals AS r
           JOIN fct_claims AS c ON c.claim_id = r.claim_id
           WHERE r.resolution_status = 'applied' AND r.reversed_at < c.submitted_at""",
    )
    assert reversed_before_claim == injected["reversals"]["dated_before_claim"]
    assert (
        one(
            con,
            "SELECT count(*) FROM fct_lookups AS l JOIN fct_claims AS c USING (claim_id) WHERE l.looked_up_at > c.submitted_at",
        )
        == injected["lookups"]["looked_up_after_claim"]
    )
