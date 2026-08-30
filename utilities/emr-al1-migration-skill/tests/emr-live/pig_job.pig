-- Pig job that works on EMR 5.33 but will CRASH on EMR 7.x
-- Tests ORDER BY (multi-vertex Tez DAG — triggers OperatorKey.hashCode NPE on Java 17)

raw = LOAD 's3://{{BUCKET}}/input/pig_data.csv'
    USING PigStorage(',')
    AS (name:chararray, amount:double, category:chararray);

-- Filter (single vertex — would work even on 7.x)
purchases = FILTER raw BY amount > 0;

-- Group + aggregate (single vertex — would work even on 7.x)
by_name = GROUP purchases BY name;
totals = FOREACH by_name GENERATE
    group AS name,
    SUM(purchases.amount) AS total_spend,
    COUNT(purchases) AS num_purchases;

-- ORDER BY (multi-vertex — CRASHES on EMR 7.x / Java 17)
ranked = ORDER totals BY total_spend DESC;

-- STORE output
STORE ranked INTO 's3://{{BUCKET}}/output/pig_source/'
    USING PigStorage('\t');
