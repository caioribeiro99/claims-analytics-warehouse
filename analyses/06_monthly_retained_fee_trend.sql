-- title: Monthly retained fee trend
-- chart: line
-- x: month
-- y: net_retained_fee
-- Net of reversals as known today. Reversals arrive days or weeks after the claim, so recent
-- months are provisional: claim counts and service fee can only fall as reversals arrive,
-- while retained fee usually falls but rises when a claim with a negative retained fee is reversed.
-- Payout and retained fee are defined only where the payout is known; the service fee whose
-- payout is unknown gets its own column, so fee = payout + retained + unknown-payout fee.
-- Pattern: DATE_TRUNC, FILTER, running total and month-over-month change with window functions.
WITH monthly AS (
    SELECT date_trunc('month', submitted_at)::DATE                                     AS month,
           sum(is_net_claim)                                                           AS net_claims,
           sum(net_service_fee)                                                        AS net_service_fee,
           sum(net_partner_payout) FILTER (WHERE attribution_status = 'attributed')    AS net_partner_payout,
           sum(net_retained_fee) FILTER (WHERE attribution_status = 'attributed')      AS net_retained_fee,
           sum(net_service_fee) FILTER (WHERE attribution_status <> 'attributed')      AS net_fee_unknown_payout
    FROM fct_claims
    GROUP BY 1
)
SELECT month,
       net_claims,
       round(net_service_fee, 2)                                        AS net_service_fee,
       round(net_partner_payout, 2)                                     AS net_partner_payout,
       round(net_retained_fee, 2)                                       AS net_retained_fee,
       round(net_fee_unknown_payout, 2)                                 AS net_fee_unknown_payout,
       round(sum(net_retained_fee) OVER (ORDER BY month), 2)            AS cumulative_retained_fee,
       round(100 * (net_retained_fee / NULLIF(lag(net_retained_fee) OVER (ORDER BY month), 0) - 1), 1)
                                                                        AS mom_change_pct
FROM monthly
ORDER BY month;
