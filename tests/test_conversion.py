"""Lookups: three nested conversion bases and an explicit resolution status per reference."""

from tests.conftest import claim, lookup, one, row, rows


def test_every_kind_of_claim_reference_resolves_to_a_status(warehouse):
    con = warehouse(
        claims=[
            claim(claim_id="good"),
            claim(claim_id="far", provider_id="PRV99999"),
            claim(claim_id="dup"),
            claim(claim_id="dup", provider_id="PRV10002"),
            claim(claim_id="broken", units="ten"),
        ],
        lookups=[
            lookup(lookup_id="none", claim_id=None),
            lookup(lookup_id="to-good", claim_id="good"),
            lookup(lookup_id="to-far", claim_id="far"),
            lookup(lookup_id="to-dup", claim_id="dup"),
            lookup(lookup_id="to-broken", claim_id="broken"),
            lookup(lookup_id="to-nothing", claim_id="ghost"),
        ],
    )
    got = {
        lookup_id: rest
        for lookup_id, *rest in rows(
            con,
            """SELECT lookup_id, has_claim_reference, claim_resolved, in_scope_conversion,
                      resolution_status FROM fct_lookups""",
        )
    }
    assert got == {
        "none": [False, False, False, "no_claim"],
        "to-good": [True, True, True, "resolved"],
        "to-far": [True, True, False, "claim_out_of_network"],
        "to-dup": [True, True, False, "claim_ambiguous_id"],
        "to-broken": [True, True, False, "claim_rejected"],
        "to-nothing": [True, False, False, "claim_not_found"],
    }
    # the same six lookups give three different conversion rates
    assert row(
        con,
        """SELECT count(*) FILTER (WHERE has_claim_reference), count(*) FILTER (WHERE claim_resolved),
                  count(*) FILTER (WHERE in_scope_conversion) FROM fct_lookups""",
    ) == (5, 4, 1)


def test_minutes_to_claim_exists_only_for_in_scope_conversions(warehouse):
    con = warehouse(
        claims=[claim(submitted_at="2025-04-01T10:00:00")],
        lookups=[
            lookup(lookup_id="a", claim_id="c1", looked_up_at="2025-04-01T09:15:00"),
            lookup(lookup_id="b", claim_id=None),
        ],
    )
    assert rows(con, "SELECT lookup_id, minutes_to_claim FROM fct_lookups ORDER BY 1") == [
        ("a", 45),
        ("b", None),
    ]


def test_lookups_are_never_dropped_for_unknown_partners_products_or_channels(warehouse):
    con = warehouse(
        lookups=[lookup(partner_code="UNSIGNED", product_code="prd-0001", channel="kiosk")]
    )
    assert one(con, "SELECT count(*) FROM fct_lookups") == 1
