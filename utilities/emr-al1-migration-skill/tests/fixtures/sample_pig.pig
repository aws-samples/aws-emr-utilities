-- Sample Pig script that will CRASH on EMR 7.x (Java 17 serialization bug)
events = LOAD 's3n://my-bucket/events/' USING PigStorage(',')
    AS (user_id:chararray, action:chararray, ts:chararray, amount:double);

-- Filter active users
active = FILTER events BY action == 'purchase';

-- Group and aggregate
by_user = GROUP active BY user_id;
totals = FOREACH by_user GENERATE
    group AS user_id,
    COUNT(active) AS purchase_count,
    SUM(active.amount) AS total_spend;

-- ORDER BY triggers the Java 17 serialization crash
ranked = ORDER totals BY total_spend DESC;
top_users = LIMIT ranked 100;

-- JOIN also crashes
user_info = LOAD 's3n://my-bucket/users/' USING PigStorage('\t')
    AS (user_id:chararray, name:chararray, email:chararray);
enriched = JOIN top_users BY user_id, user_info BY user_id;

STORE enriched INTO 's3n://my-bucket/output/top_purchasers/';
