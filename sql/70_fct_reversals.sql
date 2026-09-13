-- fct_reversals: one row per contract-valid reversal event. The source
-- reversal_id is not unique (a producer reused ids for different reversals),
-- so the grain key is a surrogate, and the record's lineage is kept alongside.
-- Reversals are never scope-filtered: the claim reference resolves to a status,
-- and only 'applied' reversals change claim economics (via fct_claims.is_reversed).
CREATE TABLE fct_reversals AS
WITH in_scope AS (
    SELECT claim_id FROM fct_claims
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
SELECT row_number() OVER (ORDER BY v.reversed_at, v.source_file, v.source_row) AS reversal_sk,
       v.reversal_id,
       v.claim_id,
       v.reversed_at,
       CASE WHEN f.claim_id IS NOT NULL THEN 'applied'
            WHEN a.claim_id IS NOT NULL THEN 'claim_ambiguous_id'
            WHEN o.claim_id IS NOT NULL THEN 'claim_out_of_network'
            WHEN r.claim_id IS NOT NULL THEN 'claim_rejected'
            ELSE 'claim_not_found'
       END AS resolution_status,
       v.source_file,
       v.source_row
FROM staging.reversals AS v
LEFT JOIN in_scope AS f                       ON f.claim_id = v.claim_id
LEFT JOIN staging.ambiguous_claim_ids AS a    ON a.claim_id = v.claim_id
LEFT JOIN out_of_network_ids AS o             ON o.claim_id = v.claim_id
LEFT JOIN rejected_ids AS r                   ON r.claim_id = v.claim_id;
