# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""End-to-end test of EMRSparkSession with EMR on EKS.

Follows the three steps of the EMR on EKS Spark Connect setup, but driven
through the client instead of the AWS CLI:

  Step 1  Create a SPARK_CONNECT managed endpoint on the virtual cluster
          (CreateManagedEndpoint), then wait for state ACTIVE and read
          endpoint.authProxyUrl.
  Step 2  Mint a session token (GetManagedEndpointSessionCredentials,
          credentialType=TOKEN, durationInSeconds=43200).
  Step 3  Connect with sc://{authProxyUrl}:443/;use_ssl=true;x-aws-proxy-auth=...
          and run queries.

EMRSparkSession.create() does all three. Note there is no fourth step starting a
session: emr-containers has no StartSession/GetSession API, and Step 2's token comes
back already active, so the managed endpoint is the whole remote resource — which is
why session.session_id below is a managed endpoint ID.

Unlike Serverless/EC2 — session-based backends whose token is refreshed in place —
EMR on EKS has two layers on independent timers: the managed endpoint
(sessionIdleTimeoutInMinutes) and the session token (durationInSeconds). While the
endpoint is ACTIVE it is reused and only the token is reminted. Once the endpoint
times out, a NEW endpoint is created before a new token can be minted, and it comes
up on a new authProxyUrl host — so the gRPC channel must be rebuilt too.
session.reconnect() does that; Test 5 exercises it.

Prerequisites:
    - Virtual cluster in RUNNING state on an EMR release that supports
      SPARK_CONNECT managed endpoints.
    - Job execution role trusted by EMR on EKS with access to your data.
    - AWS Load Balancer Controller on the EKS cluster (the endpoint is fronted
      by an ALB), and network reachability from this machine to that ALB.
    - IAM: emr-containers:CreateManagedEndpoint, DescribeManagedEndpoint,
      DeleteManagedEndpoint, GetManagedEndpointSessionCredentials.

Usage:
    python test_emreks.py

    # Reuse an already-ACTIVE endpoint to skip the multi-minute create:
    EP_ID=xxxxxxxxxxxxx python test_emreks.py
"""

import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("emr_spark_connect").setLevel(logging.DEBUG)

from emr_spark_connect import EMRSparkSession

VIRTUAL_CLUSTER_ID = os.environ.get("VC_ID", "xxxxxxxxxxxx")
EXECUTION_ROLE = os.environ.get(
    "ROLE_ARN", "arn:aws:iam::ACCOUNTID:role/emr-on-eks-execution-role"
)
REGION = os.environ.get("AWS_REGION", "us-west-2")
RELEASE_LABEL = os.environ.get("RELEASE_LABEL", "emr-7.14.0-latest")
# Endpoint teardown timer
IDLE_TIMEOUT_MINUTES = int(os.environ.get("IDLE_TIMEOUT_MINUTES", "60"))
# session token timer
TOKEN_TIMEOUT_SECONDS = int(os.environ.get("TOKEN_TIMEOUT_SECONDS", "43200"))
# optional: how long to wait for the endpoint to become ACTIVE before giving up
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "1800"))
# Optional: reuse an existing ACTIVE endpoint instead of creating one.
MANAGED_ENDPOINT_ID = os.environ.get("EP_ID") or None
# Optional control-plane endpoint override (e.g. a beta endpoint). Leave
# EMR_ENDPOINT_URL unset to use botocore's default resolution.
ENDPOINT_URL = os.environ.get("EMR_ENDPOINT_URL") or None

print(f"Connecting to EMR on EKS virtual cluster: {VIRTUAL_CLUSTER_ID}")
print(f"Execution role: {EXECUTION_ROLE}")
print(f"Region: {REGION}")
print(f"Release label: {RELEASE_LABEL}")
print(f"Idle timeout: {IDLE_TIMEOUT_MINUTES} min")
print(f"Control plane: {ENDPOINT_URL or '<prod (botocore default)>'}")
if MANAGED_ENDPOINT_ID:
    print(f"Reusing managed endpoint: {MANAGED_ENDPOINT_ID}")
else:
    print("Creating a new SPARK_CONNECT endpoint")
print()

# Steps 1-3. Endpoint creation provisions an ALB, so allow well over the 900s
# default that suffices for Serverless/EC2 sessions.
session = EMRSparkSession.create(
    resource_id=VIRTUAL_CLUSTER_ID,
    execution_role_arn=EXECUTION_ROLE,
    region=REGION,
    session_name="spark-connect-demo",
    idle_timeout_minutes=IDLE_TIMEOUT_MINUTES,
    token_duration_seconds=TOKEN_TIMEOUT_SECONDS,
    max_wait_seconds=MAX_WAIT_SECONDS,
    release_label=RELEASE_LABEL,
    spark_conf={
        "spark.dynamicAllocation.enabled": "true",
        "spark.dynamicAllocation.minExecutors": "0",
    },
    endpoint_url=ENDPOINT_URL,
    managed_endpoint_id=MANAGED_ENDPOINT_ID,
)

try:
    print(f"\n{'='*60}")
    print(f"Connected! Managed endpoint ID: {session.session_id}")
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

    # Test 4: Confirm the Spark configs from Step 1 reached the driver
    print("Test 4: Spark config overrides applied")
    for key in ("spark.dynamicAllocation.enabled", "spark.dynamicAllocation.minExecutors"):
        print(f"  {key} = {session.conf.get(key, '<unset>')}")

    # Test 5: reconnect(). The endpoint layer is reused while ACTIVE and replaced
    # once it has hit sessionIdleTimeoutInMinutes, so on a healthy session this
    # rebuilds the channel against the SAME endpoint — no second ALB.
    if os.environ.get("TEST_RECONNECT") == "1":
        print("\nTest 5: reconnect()")
        old_endpoint = session.session_id
        active_before = session.is_endpoint_active()
        print(f"  endpoint active before: {active_before}")
        session.reconnect()
        print(f"  endpoint: {old_endpoint} -> {session.session_id}")
        if active_before:
            assert session.session_id == old_endpoint, (
                "a live endpoint must be reused, not replaced"
            )
        else:
            assert session.session_id != old_endpoint, (
                "an expired endpoint must be replaced"
            )
        session.sql("SELECT 'reconnected' AS status").show()
    else:
        print("\nTest 5: skipped (set TEST_RECONNECT=1 to exercise reconnect())")

    print("\n✅ All tests passed!")

finally:
    # Stop the Spark session but keep the endpoint alive for reuse; pass
    # terminate=True to delete the managed endpoint as well.
    session.stop(terminate=False)
    print("Session stopped; endpoint left running for reuse.")
