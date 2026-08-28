# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""Example: Connect to an EMR on EC2 cluster with Spark Connect.

Prerequisites:
    pip install emr-spark-connect

    An EMR cluster with sessions enabled (emr-spark-8.0.0+):
    aws emr create-cluster \
        --release-label emr-spark-8.0.0 \
        --applications Name=Spark \
        --session-enabled \
        --instance-groups '[
          {"InstanceCount":1,"InstanceGroupType":"MASTER","InstanceType":"m5.xlarge"},
          {"InstanceCount":2,"InstanceGroupType":"CORE","InstanceType":"m5.xlarge"}
        ]' \
        --service-role EMR_DefaultRole \
        --ec2-attributes InstanceProfile=EMR_EC2_DefaultRole
"""

from emr_spark_connect import EMRSparkSession

CLUSTER_ID = "j-3HXXXXXX8RG0E"
REGION = "us-west-2"

# EMR on EC2 — execution_role_arn is optional (for runtime role sessions)
session = EMRSparkSession.create(
    resource_id=CLUSTER_ID,
    region=REGION,
    session_name="my-analytics-session",
    idle_timeout_minutes=120,
)

try:
    session.sql("SHOW DATABASES").show()
    session.sql("SELECT current_timestamp() AS now").show()

    # DataFrame operations
    df = session.range(1000)
    df.selectExpr("id", "id * id as squared").show(5)

finally:
    session.stop()
