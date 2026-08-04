-- Sample Hive 2.3 script from EMR 5.x
SET hive.execution.engine=mr;
SET hive.create.as.acid=false;

-- Create a summary table (will fail on Hive 3 — no EXTERNAL keyword)
CREATE TABLE daily_summary AS
SELECT
    date,
    user,
    COUNT(*) as event_count,
    SUM(amount) as total_amount
FROM events
WHERE date >= '2024-01-01'
GROUP BY date, user;

-- Insert with implicit type conversion (Hive 3 rejects)
INSERT OVERWRITE TABLE metrics
SELECT
    date,
    user,
    event_count,
    total_amount / '100' as normalized_amount
FROM daily_summary;

-- CTAS with EXTERNAL (broken in Hive 3)
CREATE EXTERNAL TABLE export_data
STORED AS PARQUET
LOCATION 's3://bucket/export/'
AS SELECT * FROM daily_summary WHERE date = '2024-06-01';
