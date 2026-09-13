-- title: Retained fee per lookup by partner
-- chart: bar
-- x: partner_code
-- y: retained_fee_per_lookup
-- Volume is not value: a partner that sends many lookups may convert few of them.
-- Combining two grains safely: aggregate lookups and claims separately up to the partner
-- grain, then join the aggregates. Summing after a row-level join of the two facts would
-- only be correct because attribution happens to be 1:1; aggregating first never relies on it.
WITH lookups AS (
    SELECT partner_code,
           count(*)                                    AS lookups,
           count(*) FILTER (WHERE in_scope_conversion) AS in_scope_conversions
    FROM fct_lookups
    GROUP BY partner_code
),
claims AS (
    SELECT partner_code,
           sum(is_net_claim) AS net_claims,
           -- defined only where the payout is known (a partner without terms stays NULL)
           sum(net_retained_fee) FILTER (WHERE attribution_status = 'attributed') AS net_retained_fee
    FROM fct_claims
    WHERE has_lookup
    GROUP BY partner_code
)
SELECT l.partner_code,
       l.lookups,
       l.in_scope_conversions,
       c.net_claims,
       round(c.net_retained_fee, 2)                            AS net_retained_fee,
       round(c.net_retained_fee / NULLIF(l.lookups, 0), 4)     AS retained_fee_per_lookup,
       round(c.net_retained_fee / NULLIF(c.net_claims, 0), 4)  AS retained_fee_per_net_claim
FROM lookups AS l
LEFT JOIN claims AS c ON c.partner_code = l.partner_code
ORDER BY retained_fee_per_lookup DESC NULLS LAST;
