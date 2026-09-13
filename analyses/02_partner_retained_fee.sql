-- title: Retained fee by partner
-- chart: bar
-- x: partner_code
-- y: net_retained_fee
-- Retained fee is the margin proxy: service fee minus partner payout, net of reversals.
-- Payout is only KNOWN for attributed claims whose partner has terms on file. The FILTER
-- matters: in the other groups, reversed claims hold a known 0 and the rest hold NULL, so a
-- plain SUM would print 0.00 and pass an unknown off as "nothing paid, nothing kept".
-- Pattern: dimension join, FILTER to the population where a measure is defined, NULLIF.
SELECT coalesce(f.partner_code, '(unattributed)')                                    AS partner_code,
       f.attribution_status,
       p.payout_model,
       sum(f.is_net_claim)                                                           AS net_claims,
       round(sum(f.net_service_fee), 2)                                              AS net_service_fee,
       round(sum(f.net_partner_payout) FILTER (WHERE f.attribution_status = 'attributed'), 2)
                                                                                     AS net_partner_payout,
       round(sum(f.net_retained_fee) FILTER (WHERE f.attribution_status = 'attributed'), 2)
                                                                                     AS net_retained_fee,
       round(100 * sum(f.net_retained_fee) FILTER (WHERE f.attribution_status = 'attributed')
                 / NULLIF(sum(f.net_service_fee), 0), 1)                             AS retained_pct_of_fee,
       count(*) FILTER (WHERE f.retained_fee < 0)                                    AS claims_with_negative_retained_fee
FROM fct_claims AS f
LEFT JOIN dim_partner AS p ON p.partner_code = f.partner_code
GROUP BY ALL
ORDER BY net_retained_fee DESC NULLS LAST;
