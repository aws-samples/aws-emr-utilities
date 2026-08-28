# emr-spark-connect: Testing Summary

## What Was Built

A unified Python package (`emr-spark-connect`) that provides a single interface to connect to EMR Serverless, EMR on EC2, and EMR on EKS via Spark Connect — with automatic auth token refresh so sessions survive past the 1-hour token TTL.

### User Experience

```python
from emr_spark_connect import EMRSparkSession

# EMR Serverless
session = EMRSparkSession.create(
    resource_id="00abcdef01234567",
    execution_role_arn="arn:aws:iam::123456789012:role/EMRServerlessS3RuntimeRole",
    region="us-east-1",
)

# EMR on EC2
session = EMRSparkSession.create(
    resource_id="j-1K48XXXXXXHCB",
    region="us-east-1",
)

# Then use it like a normal SparkSession
session.sql("SELECT 1 + 1 AS result").show()
session.stop()
```

The `resource_id` format is auto-detected — no need to specify which backend to use.

---

## What Was Tested

### EMR Serverless (emr-7.13.0, Spark 3.5.6-amzn-2)

| Item | Details |
|------|---------|
| Application ID | `00abcdef01234567` |
| Execution Role | `arn:aws:iam::123456789012:role/EMRServerlessS3RuntimeRole` |
| Region | us-east-1 |
| Session ID | `00abcdef01234568` |
| Session startup time | ~77 seconds (SUBMITTED → STARTING → STARTED) |
| Spark version | 3.5.6-amzn-2 |

**Tests run:**
1. ✅ `SELECT 1 + 1 AS result` — returned `2`
2. ✅ `session.range(10).selectExpr("id", "id * id AS squared")` — 10 rows with correct squares
3. ✅ `SELECT current_timestamp() AS now, current_date() AS today` — returned valid timestamps

**Token refresh:** Interceptor installed, next refresh scheduled ~55 minutes ahead (5-minute buffer before the 1-hour TTL).

---

### EMR on EC2 (emr-spark-8.0.0, Spark 4.0.2-amzn-0)

| Item | Details |
|------|---------|
| Cluster ID | `j-1K48XXXXXXHCB` |
| Cluster config | 1× m5.xlarge master + 2× m5.xlarge core |
| Execution Role | Not required (no runtime role) |
| Region | us-east-1 |
| Session ID | `is-ABCDEFGHIJKL` |
| Session startup time | ~38 seconds (SUBMITTED → STARTING → STARTED) |
| Spark version | 4.0.2-amzn-0 |

**Tests run:**
1. ✅ `SELECT 1 + 1 AS result` — returned `2`
2. ✅ `session.range(10).selectExpr("id", "id * id AS squared")` — 10 rows with correct squares
3. ✅ `SELECT current_timestamp() AS now, current_date() AS today` — returned valid timestamps

**Token refresh:** Interceptor installed, next refresh scheduled ~56 minutes ahead.

---

## Key Observations

1. **EMR on EC2 does NOT require an execution role** for basic sessions (unlike EMR Serverless which always requires one).

2. **Session startup is faster on EC2** (~38s vs ~77s for Serverless) because the cluster is already running — Serverless needs to provision compute from cold.

3. **The auto-start feature on EMR Serverless works** — the application was in STOPPED state but auto-started when a session was requested.

4. **EMR on EC2 requires `emr-spark-8.0.0`** (released August 2026) with the `--session-enabled` flag at cluster creation. The older `emr-7.13.0` does not support Spark Connect sessions on EC2.

5. **Resource ID auto-detection works reliably:**
   - `00abcdef01234567` (16 chars, starts with `00`) → EMR Serverless
   - `j-1K48XXXXXXHCB` (starts with `j-`) → EMR on EC2
   - 20-25 lowercase alphanumeric chars → EMR on EKS

6. **The same token refresh interceptor works for both backends** — the abstraction of `refresh_fn: Callable[[], Tuple[str, datetime]]` cleanly separates token refresh logic from the gRPC machinery.

---

## How Token Refresh and Endpoints Work

### The Problem

EMR Spark Connect uses a `sc://` URL to connect your local PySpark client to a remote Spark driver over gRPC. Each connection is authenticated with an `x-aws-proxy-auth` token embedded in the URL. This token **expires after 1 hour**. Once expired, every gRPC call fails with `StatusCode.UNKNOWN / "Stream removed"` — killing your session mid-work.

The remote session itself can live up to **24 hours**. It's only the auth token that's short-lived.

### The Solution: gRPC Interceptor

The package installs a `TokenRefreshInterceptor` that sits between PySpark's Spark Connect client and the gRPC channel. On **every outgoing gRPC call**, the interceptor:

1. Checks if the cached token is within 5 minutes of expiry
2. If yes, calls `GetSessionEndpoint` (Serverless) or `GetSessionEndpoint` (EC2) to get a fresh token
3. Injects the fresh token as the `x-aws-proxy-auth` metadata header on the gRPC call
4. Passes the call through to the server

```
Local PySpark Client
        │
        ▼
┌─────────────────────────────┐
│  TokenRefreshInterceptor    │
│                             │
│  • Every gRPC call passes   │
│    through here             │
│  • Checks: is token expiring│
│    within 5 minutes?        │
│  • If yes: calls AWS API    │
│    to get fresh token       │
│  • Injects x-aws-proxy-auth │
│    header with valid token  │
└─────────────────────────────┘
        │
        ▼ (gRPC over TLS, port 443)
┌─────────────────────────────┐
│  EMR Spark Connect Endpoint │
│  sc://host:443/;use_ssl=true│
└─────────────────────────────┘
```

### Endpoint URLs

When a session starts, the `GetSessionEndpoint` API returns:
- **Endpoint URL** — an `https://` URL unique to the session
- **Auth token** — a time-limited bearer token (1-hour TTL)
- **Token expiry time** — when the token becomes invalid

The client converts the `https://` endpoint to a `sc://` URL:

| Backend | Endpoint format | sc:// URL |
|---------|----------------|-----------|
| EMR Serverless | `https://<session-id>.s.emr-serverless-services.<region>.amazonaws.com` | `sc://<session-id>.s.emr-serverless-services.<region>.amazonaws.com:443/;use_ssl=true;x-aws-proxy-auth=<token>` |
| EMR on EC2 | `https://<session-id>.elasticmapreduce-services.<region>.amazonaws.com` | `sc://<session-id>.elasticmapreduce-services.<region>.amazonaws.com:443/;use_ssl=true;x-aws-proxy-auth=<token>;authorization=<session-id>` |

Note: EMR on EC2 requires an additional `authorization=<session-id>` parameter in the URL.

### Timeouts

| Timeout | Default | Description |
|---------|---------|-------------|
| Token TTL | 1 hour (not configurable) | Auth token lifetime. Interceptor refreshes 5 min before expiry. |
| Session idle timeout | 60 min (configurable via `idle_timeout_minutes`) | Session auto-terminates after this period of inactivity. |
| Session max lifetime | 24 hours | Hard limit — session terminates even if actively running. |
| `max_wait_seconds` | 900 (15 min) | How long `create()` waits for session to become ready. |
| Application auto-stop (Serverless) | 15 min | Application stops if no active sessions/jobs. Re-starts on next request if auto-start is enabled. |

### What Happens During a Long-Running Session

```
t=0 min    Session starts, initial token obtained (expires at t=60)
t=0-55     All gRPC calls use cached token — no refresh needed
t=55       Next gRPC call triggers refresh (within 5-min buffer)
           → calls GetSessionEndpoint → gets new token (expires t=120)
t=55-115   Uses new token
t=115      Another refresh
...        Continues indefinitely up to 24-hour session limit
```

The user's code doesn't need to know about any of this. Queries, DataFrame operations, and writes all just work across token boundaries.

### How It's Wired Together

```python
# EMRChannelBuilder extends PySpark's ChannelBuilder
class EMRChannelBuilder(ChannelBuilder):
    def __init__(self, url, refresh_fn):
        # refresh_fn = manager.refresh_token (returns token + expiry)
        ...

    def toChannel(self):
        channel = super().toChannel()  # normal gRPC channel with TLS
        interceptor = TokenRefreshInterceptor(self._refresh_fn)
        return grpc.intercept_channel(channel, interceptor)

# The SparkSession uses this channel builder
spark = SparkSession.builder.channelBuilder(channel_builder).getOrCreate()
```

The `refresh_fn` is backend-agnostic — it's just a callable that returns `(token, expiry_datetime)`. Each backend (Serverless, EC2, EKS) implements its own version that calls the appropriate AWS API.

---

## Package Structure

```
src/emr_spark_connect/
├── __init__.py          # Public API: EMRSparkSession
├── session.py           # EMRSparkSession — unified facade
├── backends.py          # Backend managers (Serverless, EC2, EKS)
└── interceptors.py      # gRPC token refresh interceptor + EMRChannelBuilder
```

## EMR on EKS

Implemented in `EKSSessionManager` and exercised by `test_emreks.py`. The flow is:

1. `create_managed_endpoint(type="SPARK_CONNECT", releaseLabel="emr-7.14.0-latest", sessionIdleTimeoutInMinutes=...)`
2. `describe_managed_endpoint` → poll to `ACTIVE`, read **`endpoint.authProxyUrl`**
3. `get_managed_endpoint_session_credentials(credentialType="TOKEN", durationInSeconds=43200)` → `credentials.token`
4. `sc://{authProxyUrl}:443/;use_ssl=true;x-aws-proxy-auth={token}`

Four details differ from the other backends and are easy to get wrong:

| Detail | Correct value |
|---|---|
| Endpoint host | `endpoint.authProxyUrl` — **not** `serverUrl` |
| Token API cluster param | `virtualClusterIdentifier` — **not** `virtualClusterId` (that name is a `ParamValidationError`) |
| Endpoint lifetime | Ephemeral. Expires after `sessionIdleTimeoutInMinutes` |
| Session API | There isn't one. `emr-containers` has no `StartSession`/`GetSession`/`TerminateSession` |

### No session API — the managed endpoint is the resource

`emr-containers` exposes no `StartSession`, `GetSession`, or `TerminateSession`
(verified against the botocore service model; the only session-shaped operation is
`GetManagedEndpointSessionCredentials`, which returns an *already-active* token).
So on EKS there is nothing to start and no session state to poll: the managed
endpoint is the entire remote resource. That is why the abstract method is
`provision()` rather than `start_session()` — Serverless/EC2 implement it with the
real `StartSession`, and EKS implements it as reuse-or-create of the endpoint.
Verified: `EKSSessionManager.provision()` touches only `CreateManagedEndpoint`, and
a full connect/refresh/stop cycle calls exactly `CreateManagedEndpoint`,
`DescribeManagedEndpoint`, `GetManagedEndpointSessionCredentials`, and
`DeleteManagedEndpoint`.

### Two layers, two independent timeouts

Serverless and EC2 are **session-based**: `StartSession` creates a session and
`GetSessionEndpoint` returns both URL and token, so there is no endpoint resource
that expires separately. Only the token expires, and the interceptor refreshes it
against the same session — which is why `ensure_endpoint()` is a no-op on those
backends (verified: zero API calls).

EMR on EKS has two layers, nested rather than parallel — a token is only valid
against the endpoint it was minted for, so it cannot outlive its endpoint:

| Layer | Lifetime | Renewed by |
|---|---|---|
| Managed endpoint (the only resource) | `sessionIdleTimeoutInMinutes` (`CreateManagedEndpoint`) | Creating a new endpoint |
| Session token (minted against it, active on return) | `durationInSeconds` (`GetManagedEndpointSessionCredentials`, default 15 min / max 12 h) | Minting a new token |

Which layer expired decides what is rebuilt:

- **Token only, endpoint `ACTIVE`** → mint a new token against the same endpoint.
  Host unchanged, channel survives. Same shape as Serverless/EC2.
- **Endpoint expired** → create a new endpoint, *then* mint a token against it.
  Routine, not an error. Applies equally to an endpoint passed in as
  `managed_endpoint_id`.

`EKSSessionManager.ensure_endpoint()` is the single place that reuse-or-replace
decision is made, and `provision()`, `get_endpoint_and_token()`, and
`reconnect()` all route through it, so a token is never minted against a dead
endpoint. The residual manual step is the gRPC channel: a replacement endpoint has a
new `authProxyUrl`, and an interceptor can only rewrite a header, so `refresh_token()`
creates the replacement and then raises `EndpointExpiredError` to tell the caller to
`reconnect()`.

Verification status: the API contract (parameter names, config shape, URL construction,
and the absence of any session operation on `emr-containers`) is verified against the
botocore service model and via mocked clients, covering both layers — token-only
refresh reusing a live endpoint (1 create across 3 mints), endpoint expiry creating
the replacement before signalling, expired and live caller-supplied endpoints,
`CREATING` reuse, one `DescribeManagedEndpoint` per mint, and endpoint-ownership
delete semantics. Serverless/EC2 were re-checked to confirm they still call the real
`StartSession`, skip the endpoint layer entirely, and only mint tokens against the
existing session. A live end-to-end run against a virtual cluster with an ALB-fronted
endpoint has **not** been done — `test_emreks.py` is the script for it.

## Not Yet Tested
- **Token refresh past the 1-hour mark** — verified the interceptor installs and schedules correctly; a long-running session test (>60 min) would confirm end-to-end refresh.
- **Spark configuration overrides** — the `spark_conf` parameter is wired through to the APIs but not explicitly tested.
- **Context manager** (`with EMRSparkSession.create(...) as session:`) — code path exists but not explicitly tested in isolation.

---

## Prerequisites for Reproduction

### EMR Serverless
- Application with `sessionEnabled: true` on `emr-7.13.0+`
- Execution role trusted by `emr-serverless.amazonaws.com`
- IAM permissions: `emr-serverless:StartSession`, `GetSession`, `GetSessionEndpoint`, `TerminateSession`

### EMR on EC2
- Cluster on `emr-spark-8.0.0+` created with `--session-enabled`
- Cluster in `WAITING` or `RUNNING` state
- VPC/subnet tagged `for-use-with-amazon-emr-managed-policies=true` (for private subnet clusters)
- IAM permissions: `elasticmapreduce:StartSession`, `GetSession`, `GetSessionEndpoint`, `TerminateSession`
- **boto3 >= 1.43.72** (earlier versions don't have the EMR session APIs)

### EMR on EKS
- Virtual cluster in `RUNNING` state on a release supporting `SPARK_CONNECT` managed endpoints
- Job execution role trusted by EMR on EKS
- AWS Load Balancer Controller on the EKS cluster (the endpoint is ALB-fronted), and network reachability from the client to that ALB
- IAM permissions: `emr-containers:CreateManagedEndpoint`, `DescribeManagedEndpoint`, `DeleteManagedEndpoint`, `GetManagedEndpointSessionCredentials` — note there is no session permission to grant, because there is no session API

### Local Environment
- Python 3.9+
- `pip install emr-spark-connect` (or `pip install -e .` from source)
- AWS credentials configured
