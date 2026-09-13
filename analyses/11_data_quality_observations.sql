-- title: Data-quality observations (reported, never repaired)
-- chart: table
-- Conditions that are legal under the record contracts but worth watching. None of them
-- blocks the build or changes a row; they are surfaced for the producer to act on.
SELECT 'lookups with a channel label other than api/web' AS observation,
       count(*) AS records
FROM fct_lookups
WHERE channel NOT IN ('api', 'web')
UNION ALL
SELECT 'lookups whose product_code is not in the catalog', count(*)
FROM fct_lookups AS l
LEFT JOIN dim_product AS p ON p.product_code = l.product_code
WHERE p.product_code IS NULL
UNION ALL
SELECT 'reversal ids shared by more than one reversal', count(*)
FROM (SELECT reversal_id FROM fct_reversals GROUP BY reversal_id HAVING count(*) > 1)
UNION ALL
SELECT 'claims with more than one applied reversal', count(*)
FROM (SELECT claim_id FROM fct_reversals WHERE resolution_status = 'applied'
      GROUP BY claim_id HAVING count(*) > 1)
UNION ALL
SELECT 'claims attributed to a partner with no terms on file', count(*)
FROM fct_claims
WHERE attribution_status = 'attributed_unknown_terms'
UNION ALL
SELECT 'claims whose partner payout exceeds the service fee', count(*)
FROM fct_claims
WHERE retained_fee < 0
UNION ALL
SELECT 'applied reversals dated before their claim was submitted', count(*)
FROM fct_reversals AS r
JOIN fct_claims AS c ON c.claim_id = r.claim_id
WHERE r.resolution_status = 'applied' AND r.reversed_at < c.submitted_at
UNION ALL
SELECT 'converted lookups timestamped after their claim', count(*)
FROM fct_lookups AS l
JOIN fct_claims AS c ON c.claim_id = l.claim_id
WHERE l.looked_up_at > c.submitted_at   -- compare timestamps: date_diff counts minute boundaries
UNION ALL
SELECT 'claims delivered more than once (later identical copies rejected)', count(DISTINCT claim_id)
FROM staging.redelivered_claims
UNION ALL
SELECT 'claim ids quarantined as ambiguous', count(*)
FROM staging.ambiguous_claim_ids
UNION ALL
SELECT 'distinct out-of-network provider ids seen on valid claims', count(DISTINCT c.provider_id)
FROM staging.claims AS c
LEFT JOIN dim_provider AS p ON p.provider_id = c.provider_id
WHERE p.provider_id IS NULL;
