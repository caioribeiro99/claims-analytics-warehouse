-- title: Conversion by acquisition partner, on three bases
-- chart: bar
-- x: partner_code
-- y: recorded_conversion_pct, in_scope_conversion_pct
-- recorded : the funnel says the lookup converted (the business funnel metric)
-- resolved : the referenced claim exists in the claims source (referential health)
-- in scope : the claim is in fct_claims, the only basis valid next to claim dollars
-- The gaps between them are diagnostics, not rounding.
-- Pattern: boolean conditional aggregation over one grain.
SELECT partner_code,
       count(*)                                                                     AS lookups,
       round(100.0 * count(*) FILTER (WHERE has_claim_reference) / count(*), 2)     AS recorded_conversion_pct,
       round(100.0 * count(*) FILTER (WHERE claim_resolved) / count(*), 2)          AS resolved_conversion_pct,
       round(100.0 * count(*) FILTER (WHERE in_scope_conversion) / count(*), 2)     AS in_scope_conversion_pct,
       count(*) FILTER (WHERE has_claim_reference AND NOT in_scope_conversion)      AS unusable_conversions
FROM fct_lookups
GROUP BY partner_code
ORDER BY lookups DESC;
