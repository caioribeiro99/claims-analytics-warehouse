-- title: Reconciliation: source = curated + rejected
-- chart: table
-- Every source record is either in its curated fact or in rejects, never both
-- and never neither. The build refuses to publish a warehouse where this breaks.
SELECT stream,
       source_records,
       curated_records,
       rejected_records,
       balanced
FROM audit_reconciliation;
