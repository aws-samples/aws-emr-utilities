# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""Example: Using EMRSparkSession as a context manager.

The context manager automatically stops the SparkSession and terminates
the EMR session on exit — even if an exception occurs.
"""

from emr_spark_connect import EMRSparkSession
from pyspark.sql import functions as F

APPLICATION_ID = "00abcdef01234567"
EXECUTION_ROLE = "arn:aws:iam::123456789012:role/EMRServerlessRole"

with EMRSparkSession.create(
    resource_id=APPLICATION_ID,
    execution_role_arn=EXECUTION_ROLE,
    region="us-east-1",
    spark_conf={
        "spark.executor.memory": "4g",
        "spark.executor.cores": "2",
        "spark.dynamicAllocation.enabled": "true",
    },
) as session:
    # All standard PySpark operations work
    df = session.read.parquet("s3://my-bucket/events/")

    # Filter, aggregate, write
    result = (
        df.filter(F.col("event_date") >= "2024-01-01")
        .groupBy("event_type")
        .agg(
            F.count("*").alias("event_count"),
            F.countDistinct("user_id").alias("unique_users"),
        )
        .orderBy(F.desc("event_count"))
    )

    result.show(20)
    result.write.mode("overwrite").parquet("s3://my-bucket/reports/event_summary/")

# Session is automatically cleaned up here
print("Done! Session terminated.")
