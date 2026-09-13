-- title: Reference-cost coverage by month
-- chart: bar
-- x: month
-- y: matched, before_first_effective_date, no_cost_history
-- stacked: true
-- How many claims could be costed against the reference price in effect when they were
-- submitted. Uncosted claims keep a NULL cost and an explicit status; nothing is imputed.
-- Pattern: pivot by conditional aggregation over an explicit status column.
SELECT date_trunc('month', submitted_at)::DATE                                          AS month,
       count(*) FILTER (WHERE reference_cost_status = 'matched')                        AS matched,
       count(*) FILTER (WHERE reference_cost_status = 'before_first_effective_date')    AS before_first_effective_date,
       count(*) FILTER (WHERE reference_cost_status = 'no_cost_history')                AS no_cost_history,
       round(100.0 * count(*) FILTER (WHERE reference_cost_status = 'matched') / count(*), 2)
                                                                                        AS matched_pct
FROM fct_claims
GROUP BY 1
ORDER BY 1;
