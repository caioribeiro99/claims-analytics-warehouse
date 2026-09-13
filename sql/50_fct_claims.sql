-- fct_claims: one row per in-scope claim; claim_id is a true primary key.
-- In scope = contract-valid + provider in network + claim id unambiguous
-- (for an exact redelivery, only the first delivery).
--
--   identity & dimensions   claim_id, provider_id, network, region, product_code, submitted_at
--   as submitted            gross_amount, units, service_fee
--   attribution             lookup_id, has_lookup, partner_code, channel, attribution_status
--   reversal                is_reversed, reversed_at
--   partner economics       partner_payout, retained_fee
--   reference cost          reference_unit_cost, reference_effective_date, reference_cost,
--                           reference_cost_status
--   reversal-aware          is_net_claim and net_* (see the final SELECT)
CREATE TABLE fct_claims AS
WITH in_scope AS (
    SELECT c.claim_id, c.provider_id, p.network, p.region, c.product_code,
           c.submitted_at, c.gross_amount, c.units, c.service_fee
    FROM staging.claims AS c
    JOIN dim_provider AS p USING (provider_id)
    WHERE c.claim_id NOT IN (SELECT claim_id FROM staging.ambiguous_claim_ids)
      AND c.record_seq NOT IN (SELECT record_seq FROM staging.redelivered_claims)
),

-- Claims carry no partner. Attribution comes only from the lookup that
-- produced the claim; the build asserts at most one valid lookup per in-scope
-- claim, so this join cannot fan out.
attribution AS (
    SELECT claim_id, lookup_id, partner_code, channel
    FROM staging.lookups
    WHERE claim_id IS NOT NULL
),

-- A claim is reversed when at least one contract-valid reversal references it.
-- Several reversals still reverse it exactly once; the earliest one dates it.
reversal AS (
    SELECT claim_id, min(reversed_at) AS reversed_at
    FROM staging.reversals
    GROUP BY claim_id
),

products_with_cost_history AS (
    SELECT DISTINCT product_code FROM ref_product_costs
),

enriched AS (
    SELECT c.*,
           a.lookup_id,
           a.lookup_id IS NOT NULL      AS has_lookup,
           a.partner_code,
           a.channel,
           t.payout_model,
           t.flat_payout_cents,
           t.revenue_share_pct,
           r.claim_id IS NOT NULL       AS is_reversed,
           r.reversed_at,
           h.product_code IS NOT NULL   AS product_has_cost_history,
           cost.unit_cost               AS reference_unit_cost,
           cost.effective_date          AS reference_effective_date
    FROM in_scope AS c
    LEFT JOIN attribution AS a ON a.claim_id = c.claim_id
    LEFT JOIN dim_partner AS t ON t.partner_code = a.partner_code
    LEFT JOIN reversal AS r ON r.claim_id = c.claim_id
    LEFT JOIN products_with_cost_history AS h ON h.product_code = c.product_code
    -- As-of join: the latest change-point already in effect when the claim was
    -- submitted. Joining on product_code alone would return one row per
    -- change-point (fan-out), and taking the newest price overall would leak
    -- future prices into past claims.
    ASOF LEFT JOIN ref_product_costs AS cost
        ON cost.product_code = c.product_code
       AND c.submitted_at >= cost.effective_date
),

priced AS (
    SELECT *,
           CASE WHEN NOT has_lookup       THEN 'unattributed'
                WHEN payout_model IS NULL THEN 'attributed_unknown_terms'
                ELSE 'attributed'
           END AS attribution_status,
           -- NULL = unknown (no lookup, or a partner without terms on file).
           -- A flat payout of 0 cents is a known zero.
           CAST(CASE payout_model
                    WHEN 'flat'          THEN flat_payout_cents * 0.01
                    WHEN 'revenue_share' THEN service_fee * revenue_share_pct * 0.01
                END AS DECIMAL(18, 6)) AS partner_payout,
           -- widened first: DECIMAL(12,3) x DECIMAL(12,5) would type as DECIMAL(18,8)
           -- and overflow for large but contract-valid quantities
           CAST(units AS DECIMAL(38, 3)) * reference_unit_cost AS reference_cost,
           CASE WHEN reference_unit_cost IS NOT NULL THEN 'matched'
                WHEN product_has_cost_history      THEN 'before_first_effective_date'
                ELSE 'no_cost_history'
           END AS reference_cost_status
    FROM enriched
)

SELECT claim_id, provider_id, network, region, product_code, submitted_at,
       gross_amount, units, service_fee,
       lookup_id, has_lookup, partner_code, channel, attribution_status,
       is_reversed, reversed_at,
       partner_payout,
       -- The closest observable margin proxy: the part of its fee the platform
       -- keeps. Not accounting margin (no operating costs in these sources), and
       -- negative when a flat payout exceeds a small fee.
       service_fee - partner_payout AS retained_fee,
       reference_unit_cost, reference_effective_date, reference_cost, reference_cost_status,

       -- Reversal-aware fields. A reversed claim contributes a KNOWN zero, even
       -- where the underlying value was unknown. Otherwise the submitted value
       -- passes through, NULL included. Summing a net_* column equals summing
       -- its source column over non-reversed claims, with no filter to forget.
       CASE WHEN is_reversed THEN 0 ELSE 1 END                              AS is_net_claim,
       CASE WHEN is_reversed THEN 0 ELSE gross_amount END                   AS net_gross_amount,
       CASE WHEN is_reversed THEN 0 ELSE units END                          AS net_units,
       CASE WHEN is_reversed THEN 0 ELSE service_fee END                    AS net_service_fee,
       CASE WHEN is_reversed THEN 0 ELSE partner_payout END                 AS net_partner_payout,
       CASE WHEN is_reversed THEN 0 ELSE service_fee - partner_payout END   AS net_retained_fee,
       CASE WHEN is_reversed THEN 0 ELSE reference_cost END                 AS net_reference_cost
FROM priced;
