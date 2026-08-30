#!/usr/bin/python
"""Sample PySpark application from EMR 5.x with Spark 2.4 APIs."""

from pyspark import SparkContext
from pyspark.sql import SQLContext
from pyspark.mllib.feature import HashingTF
from pyspark.mllib.clustering import KMeans

# Initialize contexts (deprecated in Spark 3.x)
sc = SparkContext.getOrCreate()
sqlContext = SQLContext(sc)

# Read data using s3n scheme
df = sqlContext.read.parquet('s3n://my-data-bucket/events/')
users = sqlContext.read.json('s3n://my-data-bucket/users/')

# Register temp table (deprecated API)
df.registerTempTable('events')
users.registerTempTable('users')

# Query
result = sqlContext.sql("""
    SELECT e.user_id, u.name, COUNT(*) as cnt
    FROM events e JOIN users u ON e.user_id = u.id
    GROUP BY e.user_id, u.name
""")

# Union (deprecated name)
combined = result.unionAll(users)

# Python 2 style
print combined.count()
print "Processing complete"

try:
    x = 1 / 0
except ZeroDivisionError, e:
    print "Error:", e

# Save output
combined.write.parquet('s3n://my-output-bucket/results/')
