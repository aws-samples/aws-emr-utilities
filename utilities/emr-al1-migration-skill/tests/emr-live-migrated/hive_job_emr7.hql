-- Hive 3.1 migrated job — adapted from Hive 2.3 patterns
-- Changes applied by migration tools:
--   1. Removed: SET hive.execution.engine=mr (Tez is default on EMR 7.x)
--   2. Changed: CREATE TABLE → CREATE EXTERNAL TABLE (prevents ACID default)
--   3. Reserved keywords already quoted in v2 (user, date)

-- NOTE: SET hive.execution.engine=mr REMOVED (Tez is default and preferred)

-- Create EXTERNAL table (prevents Hive 3 ACID default behavior)
CREATE EXTERNAL TABLE IF NOT EXISTS test_migration_events_emr7 (
    `user` STRING,
    `date` STRING,
    action STRING,
    amount DOUBLE
)
STORED AS TEXTFILE
LOCATION 's3://{{BUCKET}}/hive_data/events_emr7/';

-- Insert sample data
INSERT INTO test_migration_events_emr7 VALUES
    ('alice', '2024-01-01', 'purchase', 99.99),
    ('bob', '2024-01-01', 'purchase', 49.99),
    ('alice', '2024-01-02', 'refund', 25.00),
    ('charlie', '2024-01-02', 'purchase', 199.99),
    ('bob', '2024-01-03', 'purchase', 75.00);

-- Query with reserved keywords properly quoted
SELECT `user`, `date`, SUM(amount) as total
FROM test_migration_events_emr7
WHERE `date` >= '2024-01-01'
GROUP BY `user`, `date`
ORDER BY total DESC;

-- Cleanup
DROP TABLE IF EXISTS test_migration_events_emr7;
