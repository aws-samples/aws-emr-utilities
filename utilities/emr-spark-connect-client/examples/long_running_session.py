# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""Example: Long-running session demonstrating automatic token refresh.

This example runs queries over a period longer than the 1-hour token TTL.
The token refresh interceptor handles this transparently — no code changes needed.
"""

import logging
import time

from emr_spark_connect import EMRSparkSession

# Enable logging to observe token refreshes
logging.basicConfig(level=logging.INFO)
logging.getLogger("emr_spark_connect").setLevel(logging.DEBUG)

APPLICATION_ID = "00abcdef01234567"
EXECUTION_ROLE = "arn:aws:iam::123456789012:role/EMRServerlessRole"
REGION = "us-east-1"

with EMRSparkSession.create(
    resource_id=APPLICATION_ID,
    execution_role_arn=EXECUTION_ROLE,
    region=REGION,
    idle_timeout_minutes=180,  # 3 hours
) as session:
    # Run queries periodically for > 1 hour
    # The token auto-refreshes transparently
    for i in range(90):  # Run for ~90 minutes
        result = session.sql(f"SELECT {i} AS iteration, current_timestamp() AS ts")
        result.show()
        print(f"Iteration {i} complete at minute ~{i}")
        time.sleep(60)  # Wait 1 minute between queries
