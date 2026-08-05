#!/usr/bin/python
"""PySpark 2.4 job — uses deprecated APIs that need migration to 3.5."""
from pyspark import SparkContext
from pyspark.sql import SQLContext

sc = SparkContext.getOrCreate()
sqlContext = SQLContext(sc)

# Create sample data using deprecated API
data = [("Alice", 100), ("Bob", 200), ("Charlie", 150), ("Alice", 50)]
df = sqlContext.createDataFrame(data, ["name", "amount"])

# Use deprecated registerTempTable
df.registerTempTable("transactions")

# Query using sqlContext
result = sqlContext.sql("""
    SELECT name, SUM(amount) as total, COUNT(*) as cnt
    FROM transactions
    GROUP BY name
    ORDER BY total DESC
""")

# Use deprecated unionAll
combined = result.unionAll(result)

print "Row count:", combined.count()
print "Schema:"
combined.printSchema()
combined.show()

# Write output
combined.write.mode("overwrite").parquet("s3://{{BUCKET}}/output/spark_source/")
print "PySpark 2.4 job completed successfully"
