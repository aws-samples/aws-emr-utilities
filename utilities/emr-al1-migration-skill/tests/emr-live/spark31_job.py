"""PySpark 3.1 job (EMR 6.3.0) — tests upgrade to Spark 3.5 (EMR 7.1.0).
Uses patterns that may need adaptation for Spark 3.5.
"""
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType, DoubleType, IntegerType

spark = SparkSession.builder.appName("spark31-to-35-test").getOrCreate()

# Create sample data
schema = StructType([
    StructField("name", StringType(), True),
    StructField("department", StringType(), True),
    StructField("salary", DoubleType(), True),
    StructField("age", IntegerType(), True)
])

data = [
    ("Alice", "Engineering", 95000.0, 30),
    ("Bob", "Engineering", 105000.0, 35),
    ("Charlie", "Marketing", 78000.0, 28),
    ("Diana", "Marketing", 82000.0, 32),
    ("Eve", "Engineering", 110000.0, 40),
    ("Frank", "Sales", 65000.0, 25),
    ("Grace", "Sales", 72000.0, 29),
    ("Heidi", "Engineering", 98000.0, 33)
]

df = spark.createDataFrame(data, schema)

# Register view
df.createOrReplaceTempView("employees")

# Spark SQL with aggregations
result = spark.sql("""
    SELECT
        department,
        COUNT(*) as headcount,
        ROUND(AVG(salary), 2) as avg_salary,
        MAX(salary) as max_salary,
        MIN(age) as youngest
    FROM employees
    GROUP BY department
    ORDER BY avg_salary DESC
""")

print("=== Department Summary ===")
result.show()

# DataFrame API with window functions
from pyspark.sql.window import Window

w = Window.partitionBy("department").orderBy(F.desc("salary"))
ranked = df.withColumn("rank_in_dept", F.row_number().over(w))
top_per_dept = ranked.filter(F.col("rank_in_dept") == 1).drop("rank_in_dept")

print("=== Top Earner Per Department ===")
top_per_dept.show()

# Write output
result.write.mode("overwrite").parquet("s3://{{BUCKET}}/output/spark31_test/")
print("Spark 3.1 job completed — output written")
