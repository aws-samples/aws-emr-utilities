# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""Basic example: Connect to EMR Serverless and run SQL queries.

Prerequisites:
    pip install emr-spark-connect

    An EMR Serverless application with sessionEnabled=true (emr-7.13.0+):
    aws emr-serverless create-application \
        --type SPARK \
        --name my-spark-connect-app \
        --release-label emr-7.13.0 \
        --interactive-configuration '{"sessionEnabled": true}'
"""

from emr_spark_connect import EMRSparkSession

# Replace with your values
APPLICATION_ID = "00abcdef01234567"
EXECUTION_ROLE = "arn:aws:iam::123456789012:role/EMRServerlessRole"
REGION = "us-east-1"

# Create a session — auto-detects EMR Serverless from the application ID format
session = EMRSparkSession.create(
    resource_id=APPLICATION_ID,
    execution_role_arn=EXECUTION_ROLE,
    region=REGION,
)

try:
    # Run SQL
    print("Spark version:", session.spark.version)
    session.sql("SELECT 1 + 1 AS result").show()

    # DataFrame operations
    from pyspark.sql.functions import col

    df = session.createDataFrame(
        [(1, "Alice"), (2, "Bob"), (3, "Charlie")],
        ["id", "name"],
    )
    df.filter(col("id") > 1).show()

    # Read from S3
    # df = session.read.parquet("s3://my-bucket/my-data/")
    # df.groupBy("category").count().show()

finally:
    session.stop()
    print("Session terminated.")
