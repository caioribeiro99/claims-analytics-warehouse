-- title: As-of join versus naive joins to reference costs
-- chart: table
-- Why effective-dated reference data needs an as-of join. Over claims whose product has a
-- cost history, three ways of attaching a unit cost:
--   equality on product_code   one row per change-point: the join fans out
--   newest price overall       one row per claim, but often a price that took effect later
--   as-of (fct_claims)         latest change-point with effective_date <= submitted_at
-- Pattern: as-of join, validity-interval range join, anti-leakage checks.
WITH claims AS (
    SELECT claim_id, product_code, submitted_at
    FROM fct_claims
    WHERE reference_cost_status <> 'no_cost_history'
),
newest AS (
    SELECT product_code, max(effective_date) AS effective_date
    FROM ref_product_costs
    GROUP BY product_code
),
as_of AS (
    SELECT c.claim_id, c.submitted_at, r.effective_date
    FROM claims AS c
    ASOF LEFT JOIN ref_product_costs AS r
        ON r.product_code = c.product_code
       AND c.submitted_at >= r.effective_date
),
range_join AS (
    SELECT c.claim_id, r.effective_date
    FROM claims AS c
    LEFT JOIN ref_product_costs AS r
        ON r.product_code = c.product_code
       AND c.submitted_at >= r.effective_date
       AND (r.next_effective_date IS NULL OR c.submitted_at < r.next_effective_date)
)
SELECT (SELECT count(*) FROM claims)                                          AS claims,
       (SELECT count(*) FROM claims JOIN ref_product_costs USING (product_code))
                                                                              AS equality_join_rows,
       (SELECT count(*) FROM claims AS c JOIN newest AS n USING (product_code)
        WHERE n.effective_date > c.submitted_at)                              AS newest_price_from_the_future,
       (SELECT count(*) FROM as_of)                                           AS as_of_rows,
       (SELECT count(*) FROM as_of WHERE effective_date > submitted_at)       AS as_of_future_prices,
       (SELECT count(*) FROM as_of WHERE effective_date IS NULL)              AS as_of_before_first_price,
       (SELECT count(*) FROM as_of AS a JOIN range_join AS r USING (claim_id)
        WHERE a.effective_date IS DISTINCT FROM r.effective_date)             AS as_of_vs_range_join_differences;
