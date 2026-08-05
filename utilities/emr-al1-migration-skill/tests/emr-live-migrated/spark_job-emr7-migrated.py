#!/usr/bin/env python3
"""PySpark 3.5 job — migrated from Spark 2.4 deprecated APIs."""
from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("spark-job-emr7-migrated").getOrCreate()
sc = spark.sparkContext

# Create sample data using deprecated API
data = [("Alice", 100), ("Bob", 200), ("Charlie", 150), ("Alice", 50)]
df = spark.createDataFrame(data, ["name", "amount"])

# Use deprecated registerTempTable
df.createOrReplaceTempView("transactions")

# Query using sqlContext
result = spark.sql("""
    SELECT name, SUM(amount) as total, COUNT(*) as cnt
    FROM transactions
    GROUP BY name
    ORDER BY total DESC
""")

# Use deprecated unionAll
combined = result.union(result)

print("Row count:", combined.count())
print("Schema:")
combined.printSchema()
combined.show()

# Write output
combined.write.mode("overwrite").parquet("s3://{{BUCKET}}/output/spark_source/")
print("PySpark 2.4 job completed successfully")
