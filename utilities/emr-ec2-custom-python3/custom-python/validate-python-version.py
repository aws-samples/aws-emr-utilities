import sys

from pyspark.sql import SparkSession

spark = (
    SparkSession.builder.appName("Python Spark SQL basic example")
    .config("spark.some.config.option", "some-value")
    .getOrCreate()
)

assert (sys.version_info.major, sys.version_info.minor) == (3, 11)
