# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""End-to-end test of EMRSparkSession with EMR Serverless."""

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("emr_spark_connect").setLevel(logging.DEBUG)

from emr_spark_connect import EMRSparkSession

APPLICATION_ID = "00abcdef01234567"
EXECUTION_ROLE = "arn:aws:iam::123456789012:role/EMRServerlessS3RuntimeRole"
REGION = "us-east-1"

print(f"Connecting to EMR Serverless application: {APPLICATION_ID}")
print(f"Execution role: {EXECUTION_ROLE}")
print(f"Region: {REGION}")
print()

session = EMRSparkSession.create(
    resource_id=APPLICATION_ID,
    execution_role_arn=EXECUTION_ROLE,
    region=REGION,
    idle_timeout_minutes=15,
)

try:
    print(f"\n{'='*60}")
    print(f"Connected! Session ID: {session.session_id}")
    print(f"Spark version: {session.spark.version}")
    print(f"{'='*60}\n")

    # Test 1: Simple SQL
    print("Test 1: Simple SQL")
    session.sql("SELECT 1 + 1 AS result").show()

    # Test 2: DataFrame operations
    print("Test 2: DataFrame operations")
    df = session.range(10)
    df.selectExpr("id", "id * id AS squared").show()

    # Test 3: SQL with multiple columns
    print("Test 3: Current timestamp")
    session.sql("SELECT current_timestamp() AS now, current_date() AS today").show(truncate=False)

    print("\n✅ All tests passed!")

finally:
    session.stop()
    print("Session terminated.")
