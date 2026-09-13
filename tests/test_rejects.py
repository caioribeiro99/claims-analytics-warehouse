"""Excluded records keep every reason, their lineage and payload; nothing is salvaged."""

from tests.conftest import DROP, claim, lookup, one, reversal, rows


def test_rejects_carry_every_reason_lineage_and_the_original_payload(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="ok"),
            claim(claim_id="two-faults", gross_amount="ten", units=True),
            claim(claim_id="bad-date", submitted_at="2025-02-30T10:00:00"),
        ],
        lookups=[lookup(lookup_id="ok"), lookup(lookup_id="no-partner", partner_code=DROP)],
        reversals=[reversal(claim_id="ok"), reversal(reversal_id="no-claim", claim_id=DROP)],
    )
    got = {
        (stream, source_row): sorted(reasons)
        for stream, source_row, reasons in rows(
            con, "SELECT stream, source_row, reasons FROM rejects"
        )
    }
    assert got == {
        ("claims", 2): ["bad_gross_amount", "bad_units"],
        ("claims", 3): ["bad_submitted_at"],
        ("lookups", 2): ["missing_partner_code"],
        ("reversals", 2): ["missing_claim_id"],
    }
    assert one(con, "SELECT DISTINCT source_file FROM rejects WHERE stream = 'claims'") == (
        "claims-1.json"
    )
    payload = one(con, "SELECT raw FROM rejects WHERE stream = 'claims' AND source_row = 2")
    assert '"gross_amount":"ten"' in payload and '"units":true' in payload


def test_nothing_is_salvaged_into_the_curated_layer(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="ok"),
            claim(claim_id="sub-cent", gross_amount=19.999),
            claim(claim_id="word", units="ten"),
            claim(claim_id="text-number", service_fee="2.00"),
        ]
    )
    assert rows(con, "SELECT claim_id FROM fct_claims") == [("ok",)]


def test_every_stream_reconciles(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="ok"),
            claim(claim_id="bad", units=0),
            claim(provider_id="PRV99999"),
        ],
        lookups=[lookup(lookup_id="a"), lookup(lookup_id="b", looked_up_at="")],
        reversals=[reversal(claim_id="ok"), reversal(reversal_id="r2", reversed_at=None)],
    )
    assert rows(con, "SELECT * FROM audit_reconciliation") == [
        ("claims", 3, 1, 2, True),
        ("lookups", 2, 1, 1, True),
        ("reversals", 2, 1, 1, True),
    ]


def test_reason_counts_overlap_so_they_do_not_sum_to_rejected_records(warehouse):
    con = warehouse(
        claims=[claim(claim_id="ok"), claim(claim_id="x", gross_amount="ten", units="two")]
    )
    assert one(con, "SELECT sum(records) FROM audit_reject_reasons WHERE stream = 'claims'") == 2
    assert (
        one(con, "SELECT rejected_records FROM audit_reconciliation WHERE stream = 'claims'") == 1
    )
