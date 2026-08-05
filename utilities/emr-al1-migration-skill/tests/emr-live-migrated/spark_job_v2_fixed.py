"""PySpark 3.5 job — migrated from Spark 2.4 deprecated APIs."""
from pyspark.sql import SparkSession

# Migrated: SparkSession instead of SQLContext
spark = SparkSession.builder.appName("migration-test-target").getOrCreate()
sc = spark.sparkContext

# Create sample data
data = [("Alice", 100), ("Bob", 200), ("Charlie", 150), ("Alice", 50)]
df = spark.createDataFrame(data, ["name", "amount"])

# Migrated: createOrReplaceTempView instead of registerTempTable
df.createOrReplaceTempView("transactions")

# Migrated: spark.sql instead of sqlContext.sql
result = spark.sql("""
    SELECT name, SUM(amount) as total, COUNT(*) as cnt
    FROM transactions
    GROUP BY name
    ORDER BY total DESC
""")

# Migrated: union instead of unionAll
combined = result.union(result)

print("Row count: " + str(combined.count()))
print("Schema:")
combined.printSchema()
combined.show()

# Write output (migrated: s3:// instead of s3n://)
combined.write.mode("overwrite").parquet("s3://{{BUCKET}}/output/spark_target/")
print("PySpark 3.5 migrated job completed successfully on target cluster")
