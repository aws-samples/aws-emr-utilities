# emr-spark-connect

Unified PySpark Spark Connect client for **EMR Serverless**, **EMR on EC2**, and **EMR on EKS** with automatic auth token refresh.

## What It Does

- **One interface for all EMR backends** — `EMRSparkSession.create(resource_id=...)` auto-detects EMR Serverless, EC2, or EKS from the resource ID
- **Manages the remote lifecycle** — starts a session (Serverless/EC2) or reuses/creates the `SPARK_CONNECT` managed endpoint (EKS, which has no session API), waits for ready, connects, releases on stop
- **Automatic token refresh** — gRPC interceptor transparently refreshes the 1-hour auth token before expiry; sessions survive up to 24 hours
- **Standard SparkSession** — `session.sql()`, `session.read`, `session.createDataFrame()` all work normally
- **Context manager** — `with EMRSparkSession.create(...) as session:` auto-cleans up

## Install

```bash
pip install .
```

Requires: Python 3.9+, `pyspark[connect]` matching your EMR release, `boto3>=1.43.72`.

## Quick Start

```python
from emr_spark_connect import EMRSparkSession

# EMR Serverless (16-char app ID starting with "00")
session = EMRSparkSession.create(
    resource_id="00abcdef01234567",
    execution_role_arn="arn:aws:iam::123456789012:role/MyRole",
    region="us-east-1",
)

# EMR on EC2 (cluster ID starting with "j-")
session = EMRSparkSession.create(
    resource_id="j-1K48XXXXXXHCB",
    region="us-east-1",
)

# EMR on EKS (20-25 char virtual cluster ID)
session = EMRSparkSession.create(
    resource_id="uds4tzurhrvs1mdia8h1qr647",
    execution_role_arn="arn:aws:iam::123456789012:role/EMRonEKSExecutionRole",
    region="us-east-1",
    idle_timeout_minutes=60,
    spark_conf={"spark.dynamicAllocation.enabled": "true"},
)

session.sql("SELECT 1 + 1 AS result").show()
session.stop()
```

## EMR on EKS: two layers, two timeouts

EMR Serverless and EMR on EC2 are **session-based**: `StartSession` creates a
session, and `GetSessionEndpoint` returns both its URL and a fresh token. There is
no endpoint resource with a lifetime of its own — only the token expires, so
refreshing means minting a new one against the existing session. Nothing else to
check, and the host never changes.

EMR on EKS has **no session API at all**. `emr-containers` exposes no
`StartSession`, `GetSession`, or `TerminateSession` — the only session-shaped
operation is `GetManagedEndpointSessionCredentials`, and it hands back a token that
is *already active*. So there is nothing to start and no session state to poll: the
`SPARK_CONNECT` managed endpoint is the whole remote resource, and the managed
endpoint ID is what `session.session_id` reports.

What EKS does have is **two independently-timed layers**:

| Layer | Lifetime | Renewed by |
|---|---|---|
| **Managed endpoint** — the ALB-fronted Spark Connect server, and the only resource | `sessionIdleTimeoutInMinutes` on `CreateManagedEndpoint` | Creating a *new* endpoint |
| **Session token** — `x-aws-proxy-auth`, minted against that endpoint | `durationInSeconds` on `GetManagedEndpointSessionCredentials` (default 15 min, max 12 h) | `GetManagedEndpointSessionCredentials` |

They're nested, not parallel: a token is only valid against the endpoint it was
minted for, so it can never outlive its endpoint. Which layer expired decides what
gets rebuilt:

| What expired | What the client does |
|---|---|
| **Token only** (endpoint `ACTIVE`) | Mints a new token against the same endpoint. Host unchanged, channel keeps working — identical to Serverless/EC2. |
| **Endpoint too** | Creates a new endpoint, then mints a token against it. Routine, not an error. |

Endpoint reuse is checked before every token mint, so a live endpoint is never
replaced and no redundant ALB gets provisioned. Replacement applies whether the
endpoint was created by the client or handed in via `managed_endpoint_id` — once
expired, there's nothing left to reuse.

The one thing that can't be automated is the **gRPC channel**. A replacement
endpoint listens on a different `authProxyUrl`, and an interceptor can only rewrite
a header, not redirect an open channel. So when the endpoint layer expires
mid-session, the client creates the replacement and then raises
`EndpointExpiredError` to tell you the channel needs rebuilding. `reconnect()`
attaches to the endpoint that is already up:

```python
from emr_spark_connect import EMRSparkSession, EndpointExpiredError

try:
    session.sql("SELECT 1").show()
except EndpointExpiredError:
    session.reconnect()          # attach to the replacement endpoint
    session.sql("SELECT 1").show()

session.is_endpoint_active()     # False once the endpoint has been torn down
```

If the endpoint was replaced, `reconnect()` gives you a fresh Spark driver, so temp
views and cached DataFrames from the previous endpoint are gone — re-register them
afterwards. Calling `reconnect()` while the endpoint is still `ACTIVE` reuses it
rather than provisioning another ALB.

EMR on EKS-specific `create()` arguments:

| Argument | Purpose |
|---|---|
| `managed_endpoint_id` | Reuse an existing `SPARK_CONNECT` endpoint instead of creating one (skips the multi-minute ALB provisioning). Not deleted on `stop()`. |
| `release_label` | EMR release for the endpoint (default `emr-7.14.0-latest`). |
| `token_duration_seconds` | Session token TTL (default 12h, the documented max). |
| `application_configuration` | Full `applicationConfiguration` list — for classifications and nested configs a flat `spark_conf` can't express. |
| `monitoring_configuration` | `monitoringConfiguration` dict (CloudWatch/S3 logging, persistent app UI). |

`spark_conf` and `application_configuration` can be combined; `spark_conf` merges
into a `spark-defaults` block, and explicitly-listed properties win on conflicts.

## API

```python
EMRSparkSession.create(
    resource_id,                # Required: app ID, cluster ID, or virtual cluster ID
    execution_role_arn=None,    # Required for Serverless/EKS, optional for EC2
    region=None,                # Defaults to boto3 session region
    backend=None,               # Force: 'serverless', 'ec2', 'eks' (auto-detected if None)
    session_name="emr-spark-connect",
    idle_timeout_minutes=60,
    max_wait_seconds=900,
    spark_conf=None,            # Dict of Spark config overrides
    endpoint_url=None,          # Custom AWS endpoint
    boto3_session=None,         # Pre-configured boto3.Session
)
```

## How Token Refresh Works

EMR auth tokens are short-lived. This client installs a gRPC interceptor on the Spark Connect channel that:

1. Checks token expiry before every outgoing gRPC call (with a 5-minute buffer)
2. Mints a fresh token when needed — `GetSessionEndpoint` on Serverless/EC2, `GetManagedEndpointSessionCredentials` on EKS
3. Injects the new token as the `x-aws-proxy-auth` header

The user's code never sees token expiry — queries just work across token boundaries.

This covers EMR Serverless and EMR on EC2 completely: they have only a token to
refresh, against a session whose endpoint host never moves. On EMR on EKS the minted
token is already active, so it fully covers the token layer within the life of one
managed endpoint; when the *endpoint* layer expires the interceptor raises
`EndpointExpiredError`, because the replacement endpoint has a new host and a header
swap cannot reach it. See
[EMR on EKS: two layers, two timeouts](#emr-on-eks-two-layers-two-timeouts).

## Prerequisites

| Backend | Release | Key requirement |
|---------|---------|-----------------|
| EMR Serverless | emr-7.13.0+ | `sessionEnabled: true` on the application |
| EMR on EC2 | emr-spark-8.0.0+ | `--session-enabled` at cluster creation |
| EMR on EKS | emr-7.14.0+ | Virtual cluster in `RUNNING`; AWS Load Balancer Controller on the EKS cluster (the endpoint is fronted by an ALB) and network reachability to it |

## License

MIT-0
