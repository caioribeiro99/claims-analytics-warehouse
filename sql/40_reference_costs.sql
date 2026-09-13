-- Reference unit costs arrive as weekly publications. Each publication restates
-- the whole current price list, so the same (product_code, effective_date)
-- appears in many files, and sometimes a later publication corrects a price
-- that was already published.
--
--   staging.cost_publications  every published row and the file it came from;
--                              unit_cost stays text so the build can assert its
--                              exact shape before anything is published
--   ref_product_costs          one row per price change-point; when a change-point
--                              was restated, the latest published value wins
--
-- How a claim picks its change-point (as of its submission time) lives in
-- 50_fct_claims.sql.
CREATE TABLE staging.cost_publications AS
SELECT product_code,
       unit_of_measure,
       unit_cost,
       effective_date,
       published_date,
       parse_filename(filename) AS source_file
FROM read_csv(__COST_PUBLICATIONS__, header = true, filename = true,
              columns = {'product_code': 'VARCHAR', 'unit_of_measure': 'VARCHAR',
                         'unit_cost': 'VARCHAR', 'effective_date': 'DATE',
                         'published_date': 'DATE'});

CREATE TABLE ref_product_costs AS
WITH typed AS (
    SELECT product_code,
           effective_date,
           published_date,
           unit_of_measure,
           TRY_CAST(unit_cost AS DECIMAL(12, 5)) AS unit_cost
    FROM staging.cost_publications
),
change_points AS (
    SELECT product_code,
           effective_date,
           arg_max(unit_cost, published_date)       AS unit_cost,
           arg_max(unit_of_measure, published_date) AS unit_of_measure,
           min(published_date)                      AS first_published_date,
           max(published_date)                      AS last_published_date,
           count(*)                                 AS publication_rows,
           count(DISTINCT unit_cost) > 1            AS was_restated
    FROM typed
    GROUP BY product_code, effective_date
)
SELECT product_code,
       effective_date,
       -- exclusive upper bound of the validity interval (NULL = still current);
       -- lets the as-of join also be written as a range join
       lead(effective_date) OVER (PARTITION BY product_code ORDER BY effective_date)
           AS next_effective_date,
       unit_cost,
       unit_of_measure,
       first_published_date,
       last_published_date,
       publication_rows,
       was_restated
FROM change_points;
