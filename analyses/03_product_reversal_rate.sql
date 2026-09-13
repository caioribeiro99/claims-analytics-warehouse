-- title: Reversal rate by product
-- chart: row
-- x: product_code
-- y: reversal_pct
-- Which products lose claims after the sale. Products with fewer than 100 claims are
-- left out so small denominators do not dominate the ranking.
-- Pattern: conditional aggregation, HAVING on volume, catalog dimension join.
SELECT f.product_code,
       p.product_name,
       p.category,
       count(*)                                                            AS claims,
       count(*) FILTER (WHERE f.is_reversed)                               AS reversed_claims,
       round(100.0 * count(*) FILTER (WHERE f.is_reversed) / count(*), 2)  AS reversal_pct,
       round(sum(f.gross_amount) FILTER (WHERE f.is_reversed), 2)          AS reversed_gross_amount
FROM fct_claims AS f
LEFT JOIN dim_product AS p ON p.product_code = f.product_code
GROUP BY ALL
HAVING count(*) >= 100
ORDER BY reversal_pct DESC, claims DESC
LIMIT 12;  -- a longer list gets folded into an "Other" bar by chart tools
