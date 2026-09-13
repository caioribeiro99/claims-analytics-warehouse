-- Reference extracts: each one is a single current snapshot (the README covers
-- effective-dating them at scale). Their contracts are asserted by the build in
-- claims_warehouse/checks.py. A broken reference row fails the whole build
-- instead of being quarantined, because a wrong partner term would silently
-- misprice every claim it touches.

-- One row per in-network provider. This table is also the scope gate: a claim
-- whose provider_id is not listed here is out of network.
CREATE TABLE dim_provider AS
SELECT provider_id, network, region
FROM read_csv(__PROVIDERS__, header = true,
              columns = {'provider_id': 'VARCHAR', 'network': 'VARCHAR', 'region': 'VARCHAR'});

-- Partner terms exactly as delivered. The numbers stay text here so the build can
-- check their exact shape: a typed read would round 150.7 cents to 151 silently.
CREATE TABLE staging.partners AS
SELECT *
FROM read_csv(__PARTNERS__, header = true,
              columns = {'partner_code': 'VARCHAR', 'partner_name': 'VARCHAR',
                         'flat_payout_cents': 'VARCHAR', 'revenue_share_pct': 'VARCHAR'});

-- One row per partner with its payout term: exactly one of a flat amount per
-- claim or a percentage share of the service fee. flat_payout_cents = 0 is a
-- real commercial term (the partner is paid nothing), not a missing value.
CREATE TABLE dim_partner AS
SELECT partner_code,
       partner_name,
       TRY_CAST(flat_payout_cents AS INTEGER)       AS flat_payout_cents,
       TRY_CAST(revenue_share_pct AS DECIMAL(5, 2)) AS revenue_share_pct,
       CASE WHEN flat_payout_cents IS NOT NULL THEN 'flat' ELSE 'revenue_share' END AS payout_model
FROM staging.partners;

-- One row per catalog product.
CREATE TABLE dim_product AS
SELECT product_code, product_name, category, unit_of_measure
FROM read_csv(__PRODUCTS__, header = true,
              columns = {'product_code': 'VARCHAR', 'product_name': 'VARCHAR',
                         'category': 'VARCHAR', 'unit_of_measure': 'VARCHAR'});
