-- Contract-valid subsets of raw staging. Everything downstream builds on these.
CREATE TABLE staging.claims AS
SELECT * FROM staging.claims_raw WHERE len(schema_reasons) = 0;

CREATE TABLE staging.lookups AS
SELECT * FROM staging.lookups_raw WHERE len(schema_reasons) = 0;

CREATE TABLE staging.reversals AS
SELECT * FROM staging.reversals_raw WHERE len(schema_reasons) = 0;

-- Claim identity. The producer's contract says a claim id identifies one claim.
--
-- Copies of an id that DISAGREE on any field, from any provider in or out of
-- network, make the id ambiguous. Keeping one copy would silently delete a
-- different real claim, and inventing a composite key would invent a business
-- rule, so every copy is quarantined.
CREATE TABLE staging.ambiguous_claim_ids AS
SELECT claim_id
FROM staging.claims
GROUP BY claim_id
HAVING count(DISTINCT (provider_id, product_code, gross_amount, units, service_fee, submitted_at)) > 1;

-- Copies that are IDENTICAL after typing are one claim delivered more than once.
-- The first delivery (lowest record_seq) is kept; later copies are rejected as
-- duplicate_redelivery. Dropping an exact copy loses nothing and invents nothing.
CREATE TABLE staging.redelivered_claims AS
SELECT record_seq, claim_id
FROM staging.claims
WHERE claim_id NOT IN (SELECT claim_id FROM staging.ambiguous_claim_ids)
QUALIFY row_number() OVER (PARTITION BY claim_id ORDER BY record_seq) > 1;
