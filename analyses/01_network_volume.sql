-- title: Claims by network
-- chart: bar
-- x: network
-- y: net_claims
-- How transaction volume and value split across provider networks, net of reversals.
-- Pattern: GROUP BY with FILTER, share of total via a window over an aggregate.
SELECT network,
       count(*)                                                          AS claims,
       sum(is_net_claim)                                                 AS net_claims,
       round(100.0 * count(*) FILTER (WHERE is_reversed) / count(*), 2)  AS reversal_pct,
       round(sum(net_gross_amount), 2)                                   AS net_gross_amount,
       round(100.0 * sum(is_net_claim) / sum(sum(is_net_claim)) OVER (), 2)
                                                                         AS share_of_net_claims_pct
FROM fct_claims
GROUP BY network
ORDER BY net_claims DESC;
