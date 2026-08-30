-- Presto query from EMR 5.x (needs Trino conversion)
SELECT
    json_extract(event_data, '$.user_id') as user_id,
    json_extract_scalar(event_data, '$.action') as action,
    CAST(current_timestamp AS VARCHAR) as processed_at,
    approx_distinct(session_id) as unique_sessions
FROM hive.default.events
WHERE dt = '2024-06-01'
GROUP BY 1, 2, 3;
