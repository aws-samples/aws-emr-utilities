"""PySpark 3.5 job — migrated from Spark 2.4 deprecated APIs.
   Uses Python 3 print functions (EMR 7.x default)."""
from pyspark.sql import SparkSession

# Works on both EMR 5.33 (Spark 2.4) and tests deprecated patterns
spark = SparkSession.builder.appName("migration-test-source").getOrCreate()
sc = spark.sparkContext

# Create sample data using deprecated API
data = [("Alice", 100), ("Bob", 200), ("Charlie", 150), ("Alice", 50)]
df = spark.createDataFrame(data, ["name", "amount"])

# Use deprecated registerTempTable
df.createOrReplaceTempView("transactions")

# Query using sqlContext (deprecated — should use spark.sql)
result = spark.sql("""
    SELECT name, SUM(amount) as total, COUNT(*) as cnt
    FROM transactions
    GROUP BY name
    ORDER BY total DESC
""")

# Use deprecated unionAll
combined = result.union(result)

print("Row count: " + str(combined.count()))
print("Schema:")
combined.printSchema()
combined.show()

# Write output
combined.write.mode("overwrite").parquet("s3://{{BUCKET}}/output/spark_source/")
print("PySpark 2.4 job completed successfully on source cluster")
