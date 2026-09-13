-- Raw typed staging. Every source record lands here exactly once: typed where
-- the value could be typed, with every contract violation it triggered (an
-- empty list means clean), its lineage (file and row) and its original payload.
-- claims_warehouse/build.py fills these tables from the JSON event files.
CREATE SCHEMA staging;

CREATE TABLE staging.claims_raw (
    record_seq     BIGINT    NOT NULL,  -- load order across all files of the stream
    source_file    VARCHAR   NOT NULL,
    source_row     INTEGER   NOT NULL,  -- 1-based position inside source_file
    claim_id       VARCHAR,
    provider_id    VARCHAR,
    product_code   VARCHAR,
    gross_amount   DECIMAL(12, 2),
    units          DECIMAL(12, 3),
    service_fee    DECIMAL(10, 2),
    submitted_at   TIMESTAMP,
    schema_reasons VARCHAR[] NOT NULL,
    raw            VARCHAR   NOT NULL
);

CREATE TABLE staging.lookups_raw (
    record_seq     BIGINT    NOT NULL,
    source_file    VARCHAR   NOT NULL,
    source_row     INTEGER   NOT NULL,
    lookup_id      VARCHAR,
    claim_id       VARCHAR,
    product_code   VARCHAR,
    partner_code   VARCHAR,
    channel        VARCHAR,
    looked_up_at   TIMESTAMP,
    schema_reasons VARCHAR[] NOT NULL,
    raw            VARCHAR   NOT NULL
);

CREATE TABLE staging.reversals_raw (
    record_seq     BIGINT    NOT NULL,
    source_file    VARCHAR   NOT NULL,
    source_row     INTEGER   NOT NULL,
    reversal_id    VARCHAR,
    claim_id       VARCHAR,
    reversed_at    TIMESTAMP,
    schema_reasons VARCHAR[] NOT NULL,
    raw            VARCHAR   NOT NULL
);
