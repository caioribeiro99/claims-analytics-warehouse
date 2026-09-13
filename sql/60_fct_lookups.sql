-- fct_lookups: one row per contract-valid lookup. Lookups carry no provider, so
-- they are never scope-filtered. An unusable claim reference becomes a
-- resolution_status; the row is never dropped.
--
-- Three nested conversion flags (each implies the one before):
--   has_claim_reference  the funnel recorded a conversion       -> funnel metric
--   claim_resolved       the referenced claim exists in source  -> referential health
--   in_scope_conversion  the referenced claim is in fct_claims  -> the only basis that
--                        may be combined with claim economics
CREATE TABLE fct_lookups AS
WITH in_scope AS (
    SELECT claim_id, submitted_at FROM fct_claims
),
source_claim_ids AS (
    SELECT DISTINCT claim_id FROM staging.claims_raw WHERE claim_id IS NOT NULL
),
out_of_network_ids AS (
    SELECT DISTINCT c.claim_id
    FROM staging.claims AS c
    LEFT JOIN dim_provider AS p ON p.provider_id = c.provider_id
    WHERE p.provider_id IS NULL
),
rejected_ids AS (
    SELECT DISTINCT claim_id
    FROM staging.claims_raw
    WHERE len(schema_reasons) > 0 AND claim_id IS NOT NULL
)
SELECT l.lookup_id,
       l.partner_code,
       l.channel,
       l.product_code,
       l.looked_up_at,
       l.claim_id,
       l.claim_id IS NOT NULL AS has_claim_reference,
       s.claim_id IS NOT NULL AS claim_resolved,
       f.claim_id IS NOT NULL AS in_scope_conversion,
       CASE WHEN l.claim_id IS NULL     THEN 'no_claim'
            WHEN f.claim_id IS NOT NULL THEN 'resolved'
            WHEN a.claim_id IS NOT NULL THEN 'claim_ambiguous_id'
            WHEN o.claim_id IS NOT NULL THEN 'claim_out_of_network'
            WHEN r.claim_id IS NOT NULL THEN 'claim_rejected'
            ELSE 'claim_not_found'
       END AS resolution_status,
       date_diff('minute', l.looked_up_at, f.submitted_at) AS minutes_to_claim
FROM staging.lookups AS l
LEFT JOIN in_scope AS f                       ON f.claim_id = l.claim_id
LEFT JOIN source_claim_ids AS s               ON s.claim_id = l.claim_id
LEFT JOIN staging.ambiguous_claim_ids AS a    ON a.claim_id = l.claim_id
LEFT JOIN out_of_network_ids AS o             ON o.claim_id = l.claim_id
LEFT JOIN rejected_ids AS r                   ON r.claim_id = l.claim_id;
