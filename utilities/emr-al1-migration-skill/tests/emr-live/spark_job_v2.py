"""PySpark 2.4 job — uses deprecated APIs that need migration to 3.5.
   Uses Python 2 compatible print but with parentheses for EMR 5.33 Python 3 default."""
from pyspark.sql import SQLContext, SparkSession

# Works on both EMR 5.33 (Spark 2.4) and tests deprecated patterns
spark = SparkSession.builder.appName("migration-test-source").getOrCreate()
sc = spark.sparkContext
sqlContext = SQLContext(sc)

# Create sample data using deprecated API
data = [("Alice", 100), ("Bob", 200), ("Charlie", 150), ("Alice", 50)]
df = sqlContext.createDataFrame(data, ["name", "amount"])

# Use deprecated registerTempTable
df.registerTempTable("transactions")

# Query using sqlContext (deprecated — should use spark.sql)
result = sqlContext.sql("""
    SELECT name, SUM(amount) as total, COUNT(*) as cnt
    FROM transactions
    GROUP BY name
    ORDER BY total DESC
""")

# Use deprecated unionAll
combined = result.unionAll(result)

print("Row count: " + str(combined.count()))
print("Schema:")
combined.printSchema()
combined.show()

# Write output
combined.write.mode("overwrite").parquet("s3://{{BUCKET}}/output/spark_source/")
print("PySpark 2.4 job completed successfully on source cluster")
