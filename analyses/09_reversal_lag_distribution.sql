-- title: Reversal lag distribution
-- chart: bar
-- x: lag_bucket
-- y: reversed_claims
-- Time from submission to the (first) applied reversal. It tells you how long a month's
-- numbers stay provisional. A reversal dated before its claim is legal under the record
-- contracts but suspicious, so it gets its own bucket (and a row in analysis 11).
-- Pattern: join across grains (reversal events -> claims), CASE bucketing,
-- cumulative share with a window over grouped rows, percentiles.
WITH first_reversal AS (
    SELECT c.claim_id,
           date_diff('second', c.submitted_at, r.reversed_at) / 86400.0 AS lag_days
    FROM fct_reversals AS r
    JOIN fct_claims AS c
      ON c.claim_id = r.claim_id
     AND r.reversed_at = c.reversed_at   -- the reversal that dated the claim
    WHERE r.resolution_status = 'applied'
    QUALIFY row_number() OVER (PARTITION BY c.claim_id ORDER BY r.reversal_sk) = 1
),
bucketed AS (
    SELECT CASE WHEN lag_days < 0  THEN '0. before submission'
                WHEN lag_days < 1  THEN '1. under 1 day'
                WHEN lag_days < 2  THEN '2. 1-2 days'
                WHEN lag_days < 7  THEN '3. 2-7 days'
                WHEN lag_days < 14 THEN '4. 7-14 days'
                WHEN lag_days < 30 THEN '5. 14-30 days'
                ELSE                    '6. 30+ days'
           END AS lag_bucket,
           lag_days
    FROM first_reversal
)
SELECT lag_bucket,
       count(*)                                                                   AS reversed_claims,
       round(100.0 * sum(count(*)) OVER (ORDER BY lag_bucket) / sum(count(*)) OVER (), 1)
                                                                                  AS cumulative_pct,
       round(quantile_cont(lag_days, 0.5), 1)                                     AS median_lag_days_in_bucket
FROM bucketed
GROUP BY lag_bucket
ORDER BY lag_bucket;
