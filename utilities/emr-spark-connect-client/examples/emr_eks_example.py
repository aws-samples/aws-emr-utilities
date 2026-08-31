# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""Example: Connect to EMR on EKS and handle managed endpoint expiry.

EMR on EKS has no session API — emr-containers exposes no StartSession, and
GetManagedEndpointSessionCredentials returns an already-active token. The
SPARK_CONNECT managed endpoint is the entire remote resource, so session.session_id
here is a managed endpoint ID.

Prerequisites:
    An EMR on EKS virtual cluster in RUNNING state, a job execution role, and
    the AWS Load Balancer Controller on the EKS cluster — the SPARK_CONNECT
    managed endpoint is fronted by an ALB, which this machine must be able to
    reach.

The equivalent manual setup via the AWS CLI would be:

    aws emr-containers create-managed-endpoint \
        --type SPARK_CONNECT \
        --virtual-cluster-id <VC_ID> \
        --name spark-connect-demo \
        --execution-role-arn arn:aws:iam::123456789012:role/EMRonEKSExecutionRole \
        --release-label emr-7.14.0-latest \
        --session-idle-timeout-in-minutes 60

    aws emr-containers get-managed-endpoint-session-credentials \
        --virtual-cluster-identifier <VC_ID> \
        --endpoint-identifier <ENDPOINT_ID> \
        --execution-role-arn arn:aws:iam::123456789012:role/EMRonEKSExecutionRole \
        --credential-type TOKEN \
        --duration-in-seconds 43200

EMRSparkSession.create() does all of that, plus polling the endpoint to ACTIVE
and building the sc://{authProxyUrl}:443 connection.
"""

from emr_spark_connect import EMRSparkSession, EndpointExpiredError

# Replace with your values
VIRTUAL_CLUSTER_ID = "YOUR_VIRTUAL_CLUSTER_ID"
EXECUTION_ROLE = "arn:aws:iam::123456789012:role/EMRonEKSExecutionRole"

# One call does three steps:
#   1. create the endpoint (CreateManagedEndpoint) or reuse a live one
#   2. mint a token (GetManagedEndpointSessionCredentials)
#   3. connect to Spark Connect at sc://{authProxyUrl}:443
session = EMRSparkSession.create(
    resource_id=VIRTUAL_CLUSTER_ID,
    execution_role_arn=EXECUTION_ROLE,
    idle_timeout_minutes=10,
    token_duration_seconds=900,
    spark_conf={
        "spark.dynamicAllocation.minExecutors": "0",
        "spark.dynamicAllocation.enabled": "true",
        "spark.kubernetes.node.selector.topology.kubernetes.io/zone": "us-west-2a"
    },
    # Reuse an existing ACTIVE endpoint (skips the multi-minute ALB creation):
    # managed_endpoint_id="YOUR_ENDPOINT_ID",
)

try:
    print("Managed endpoint:", session.session_id)
    print("Spark version:", session.spark.version)

    session.sql("SELECT 1 + 1 AS result").show()

    # The endpoint and its tokens expire on separate timers. While the endpoint is
    # ACTIVE, token expiry is handled in place — a new token is minted against the
    # same endpoint and comes back ready to use, so you never see it. When the
    # endpoint itself times out, the client creates a replacement automatically —
    # but that replacement has a brand new Spark driver, so the open channel has to be rebuilt.
    try:
        session.sql("SELECT current_timestamp() AS now").show()
    except EndpointExpiredError:
        print("Endpoint expired — replacement created, reattaching")
        session.reconnect()
        # The replacement is a new Spark driver: temp views and cached
        # DataFrames from before are all gone
        session.sql("SELECT current_timestamp() AS now").show()

finally:
    # Stop the Spark session, keep the endpoint alive until idle timeout
    session.stop(terminate=False)
