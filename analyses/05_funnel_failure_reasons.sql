-- title: Where the funnel loses lookups and conversions
-- chart: row
-- x: detail
-- y: records
-- Two kinds of loss, kept apart: lookups rejected at ingestion (contract violations),
-- and recorded conversions whose claim never became an in-scope claim.
-- A rejected lookup can carry several reasons, so ingestion shares are shares of rejected
-- lookups and may add up to more than 100%.
-- Pattern: UNION ALL of two diagnostics into one tidy result, explicit denominators per stage.
WITH losses AS (
    SELECT 'lookup rejected at ingestion'                            AS stage,
           reason                                                    AS detail,
           records,
           (SELECT count(*) FROM rejects WHERE stream = 'lookups')   AS stage_records
    FROM audit_reject_reasons
    WHERE stream = 'lookups'
    UNION ALL
    SELECT 'conversion not usable',
           resolution_status,
           count(*),
           sum(count(*)) OVER ()
    FROM fct_lookups
    WHERE has_claim_reference AND NOT in_scope_conversion
    GROUP BY resolution_status
)
SELECT stage,
       detail,
       records,
       stage_records,
       round(100.0 * records / stage_records, 1) AS pct_of_stage_records
FROM losses
ORDER BY stage, records DESC;
