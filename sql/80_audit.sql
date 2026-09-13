-- Audit views, queryable from any client. The build also asserts that every
-- stream in audit_reconciliation is balanced.
CREATE VIEW audit_reconciliation AS
WITH counts AS (
    SELECT 'claims' AS stream,
           (SELECT count(*) FROM staging.claims_raw) AS source_records,
           (SELECT count(*) FROM fct_claims)         AS curated_records
    UNION ALL
    SELECT 'lookups',
           (SELECT count(*) FROM staging.lookups_raw),
           (SELECT count(*) FROM fct_lookups)
    UNION ALL
    SELECT 'reversals',
           (SELECT count(*) FROM staging.reversals_raw),
           (SELECT count(*) FROM fct_reversals)
),
rejected AS (
    SELECT stream, count(*) AS rejected_records FROM rejects GROUP BY stream
)
SELECT c.stream,
       c.source_records,
       c.curated_records,
       coalesce(r.rejected_records, 0) AS rejected_records,
       c.source_records = c.curated_records + coalesce(r.rejected_records, 0) AS balanced
FROM counts AS c
LEFT JOIN rejected AS r USING (stream)
ORDER BY c.stream;

-- Why records were excluded. One record can carry several reasons.
CREATE VIEW audit_reject_reasons AS
SELECT stream, reason, count(*) AS records
FROM (SELECT stream, unnest(reasons) AS reason FROM rejects)
GROUP BY stream, reason
ORDER BY stream, records DESC, reason;
