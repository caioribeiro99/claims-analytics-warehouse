"""Build invariants.

Each query counts offending rows. Any non-zero result aborts the build before
the new warehouse replaces the previous one. These checks guard structure:
keys, grain, reconciliation and reference-data contracts. Business semantics
(payouts, reversals, as-of costing, conversion) are covered by the test suite.
"""

from __future__ import annotations


class BuildError(RuntimeError):
    """A delivery problem or a failed invariant. No warehouse is published."""


def _unique(table: str, key: str) -> str:
    return f"SELECT count(*) - count(DISTINCT ({key})) FROM {table}"


INVARIANTS: tuple[tuple[str, str], ...] = (
    ("dim_provider.provider_id is unique", _unique("dim_provider", "provider_id")),
    ("dim_partner.partner_code is unique", _unique("dim_partner", "partner_code")),
    ("dim_product.product_code is unique", _unique("dim_product", "product_code")),
    (
        "reference keys are never null",
        """SELECT (SELECT count(*) FROM dim_provider WHERE provider_id IS NULL)
                + (SELECT count(*) FROM dim_partner WHERE partner_code IS NULL)
                + (SELECT count(*) FROM dim_product WHERE product_code IS NULL)""",
    ),
    (
        "every partner has exactly one payout term (flat cents or revenue share)",
        """SELECT count(*) FROM staging.partners
           WHERE (flat_payout_cents IS NULL) = (revenue_share_pct IS NULL)""",
    ),
    (
        # read as text first: a typed read would round 150.7 cents to 151 without a word
        "partner terms are exact and in range (whole cents >= 0; share 0-100, at most 2 decimals)",
        """SELECT count(*) FROM staging.partners
           WHERE NOT regexp_full_match(coalesce(flat_payout_cents, '0'), '[0-9]{1,9}')
              OR NOT regexp_full_match(coalesce(revenue_share_pct, '0'), '[0-9]{1,3}([.][0-9]{1,2})?')
              OR TRY_CAST(revenue_share_pct AS DECIMAL(5, 2)) > 100""",
    ),
    (
        "cost publication rows are complete with exact unit costs (>= 0, at most 5 decimals)",
        """SELECT count(*) FROM staging.cost_publications
           WHERE product_code IS NULL OR unit_of_measure IS NULL
              OR effective_date IS NULL OR published_date IS NULL
              OR NOT regexp_full_match(coalesce(unit_cost, ''), '[0-9]{1,7}([.][0-9]{1,5})?')""",
    ),
    (
        "a cost publication lists each (product_code, effective_date) at most once",
        """SELECT count(*) FROM (
               SELECT 1 FROM staging.cost_publications
               GROUP BY product_code, effective_date, published_date
               HAVING count(*) > 1)""",
    ),
    (
        "ref_product_costs has one row per (product_code, effective_date)",
        _unique("ref_product_costs", "product_code, effective_date"),
    ),
    (
        "no in-scope claim has more than one valid lookup (attribution would fan out)",
        """SELECT count(*) FROM (
               SELECT l.claim_id FROM staging.lookups AS l
               JOIN fct_claims AS f ON f.claim_id = l.claim_id
               GROUP BY l.claim_id HAVING count(*) > 1)""",
    ),
    ("fct_claims.claim_id is unique", _unique("fct_claims", "claim_id")),
    (
        "fct_claims has exactly one row per in-scope claim",
        """SELECT abs((SELECT count(*) FROM fct_claims) - (
               SELECT count(*) FROM staging.claims AS c
               JOIN dim_provider AS p ON p.provider_id = c.provider_id
               WHERE c.claim_id NOT IN (SELECT claim_id FROM staging.ambiguous_claim_ids)
                 AND c.record_seq NOT IN (SELECT record_seq FROM staging.redelivered_claims)))""",
    ),
    ("fct_lookups.lookup_id is unique", _unique("fct_lookups", "lookup_id")),
    (
        "fct_lookups has exactly one row per contract-valid lookup",
        "SELECT abs((SELECT count(*) FROM fct_lookups) - (SELECT count(*) FROM staging.lookups))",
    ),
    ("fct_reversals.reversal_sk is unique", _unique("fct_reversals", "reversal_sk")),
    (
        "fct_reversals has exactly one row per contract-valid reversal",
        """SELECT abs((SELECT count(*) FROM fct_reversals)
                    - (SELECT count(*) FROM staging.reversals))""",
    ),
    (
        "every stream reconciles: source = curated + rejected",
        "SELECT count(*) FROM audit_reconciliation WHERE NOT balanced",
    ),
)


def assert_invariants(con) -> None:
    failures = []
    for description, sql in INVARIANTS:
        offending = con.execute(sql).fetchone()[0]
        if offending:
            failures.append(f"{description} (offending: {offending})")
    if failures:
        raise BuildError("invariant(s) failed:\n  - " + "\n  - ".join(failures))
