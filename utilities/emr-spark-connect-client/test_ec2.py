# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""End-to-end test of EMRSparkSession with EMR on EC2."""

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("emr_spark_connect").setLevel(logging.DEBUG)

from emr_spark_connect import EMRSparkSession

CLUSTER_ID = "j-1K48XXXXXXHCB"
REGION = "us-east-1"

print(f"Connecting to EMR on EC2 cluster: {CLUSTER_ID}")
print(f"Region: {REGION}")
print()

session = EMRSparkSession.create(
    resource_id=CLUSTER_ID,
    region=REGION,
    session_name="spark-connect-client-test",
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

    # Test 3: Current timestamp
    print("Test 3: Current timestamp")
    session.sql("SELECT current_timestamp() AS now, current_date() AS today").show(truncate=False)

    print("\n✅ All tests passed!")

finally:
    session.stop()
    print("Session terminated.")
