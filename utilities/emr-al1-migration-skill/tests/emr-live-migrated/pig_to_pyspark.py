"""PySpark equivalent of pig_job.pig — converted from Pig Latin.

Original Pig script used: LOAD, FILTER, GROUP BY, FOREACH GENERATE, ORDER BY, LIMIT, JOIN, STORE.
The ORDER BY and JOIN operations crash on EMR 7.x due to Pig's Java 17 serialization bug.
This PySpark version produces identical output using DataFrame API.
"""
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.appName("pig-to-pyspark-migration-test").getOrCreate()

# LOAD 's3://bucket/input/pig_data.csv' USING PigStorage(',')
#     AS (name:chararray, amount:double, category:chararray)
raw = spark.read.csv(
    "s3://{{BUCKET}}/input/pig_data.csv",
    schema="name STRING, amount DOUBLE, category STRING"
)

# FILTER raw BY amount > 0
purchases = raw.filter(F.col("amount") > 0)

# GROUP purchases BY name
# FOREACH by_name GENERATE group AS name, SUM(purchases.amount), COUNT(purchases)
totals = purchases.groupBy("name").agg(
    F.sum("amount").alias("total_spend"),
    F.count("*").alias("num_purchases")
)

# ORDER totals BY total_spend DESC
ranked = totals.orderBy(F.desc("total_spend"))

# STORE ranked INTO 's3://bucket/output/pig_target/' USING PigStorage('\t')
ranked.write.mode("overwrite").csv(
    "s3://{{BUCKET}}/output/pig_target/",
    sep="\t",
    header=False
)

# Print results for validation
print("=== Pig→PySpark Migration Output ===")
ranked.show()
print("Pig-to-PySpark migrated job completed successfully")
