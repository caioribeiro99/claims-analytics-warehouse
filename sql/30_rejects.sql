-- rejects: one row per excluded source record, carrying EVERY reason that
-- applies, its lineage (source_file, source_row) and its original payload.
--
-- Reconciliation identity, asserted by the build for each stream:
--     source records = curated records + rejected records
-- Reasons overlap (one record can break several rules), so per-reason counts
-- are diagnostics and intentionally do not add up to the stream total.
--
-- Claims have three exclusions on top of their record contract:
--   out_of_network          provider_id is not in dim_provider
--   ambiguous_duplicate_id  the id is carried by contract-valid copies that disagree
--   duplicate_redelivery    an identical, later copy of a claim already delivered
CREATE TABLE rejects AS
WITH claim_reasons AS (
    SELECT c.record_seq, c.source_file, c.source_row, c.raw,
           c.schema_reasons
           || CASE WHEN c.provider_id IS NOT NULL AND p.provider_id IS NULL
                   THEN ['out_of_network'] ELSE []::VARCHAR[] END
           || CASE WHEN len(c.schema_reasons) = 0 AND a.claim_id IS NOT NULL
                   THEN ['ambiguous_duplicate_id'] ELSE []::VARCHAR[] END
           || CASE WHEN d.record_seq IS NOT NULL
                   THEN ['duplicate_redelivery'] ELSE []::VARCHAR[] END AS reasons
    FROM staging.claims_raw AS c
    LEFT JOIN dim_provider AS p ON p.provider_id = c.provider_id
    LEFT JOIN staging.ambiguous_claim_ids AS a ON a.claim_id = c.claim_id
    LEFT JOIN staging.redelivered_claims AS d ON d.record_seq = c.record_seq
),
excluded AS (
    SELECT 'claims' AS stream, record_seq, source_file, source_row, reasons, raw
    FROM claim_reasons
    WHERE len(reasons) > 0
    UNION ALL
    SELECT 'lookups', record_seq, source_file, source_row, schema_reasons, raw
    FROM staging.lookups_raw
    WHERE len(schema_reasons) > 0
    UNION ALL
    SELECT 'reversals', record_seq, source_file, source_row, schema_reasons, raw
    FROM staging.reversals_raw
    WHERE len(schema_reasons) > 0
)
SELECT row_number() OVER (ORDER BY stream, record_seq) AS reject_sk,
       stream,
       source_file,
       source_row,
       reasons,
       raw
FROM excluded;
