-- Hive 2.3 job — uses patterns that need migration to Hive 3.1
-- Fixed: 'user' quoted since it's reserved even on 2.3.7

SET hive.execution.engine=mr;

-- Create table (no EXTERNAL — problem on Hive 3)
CREATE TABLE IF NOT EXISTS test_migration_events (
    `user` STRING,
    `date` STRING,
    action STRING,
    amount DOUBLE
)
STORED AS TEXTFILE;

-- Insert sample data
INSERT INTO test_migration_events VALUES
    ('alice', '2024-01-01', 'purchase', 99.99),
    ('bob', '2024-01-01', 'purchase', 49.99),
    ('alice', '2024-01-02', 'refund', 25.00),
    ('charlie', '2024-01-02', 'purchase', 199.99),
    ('bob', '2024-01-03', 'purchase', 75.00);

-- Query
SELECT `user`, `date`, SUM(amount) as total
FROM test_migration_events
WHERE `date` >= '2024-01-01'
GROUP BY `user`, `date`
ORDER BY total DESC;

-- Cleanup
DROP TABLE IF EXISTS test_migration_events;
