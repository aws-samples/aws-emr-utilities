# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""Backend-specific session managers for EMR Serverless, EMR on EKS, and EMR on EC2.

Each backend handles:
- Resource lifecycle (provision, wait, get endpoint, release)
- Token refresh (returns token + expiry for the interceptor)
- Resource ID detection (which backend to use based on ID format)

Note that "session" is a Serverless/EC2 concept. Those services have a session
API (``StartSession`` / ``GetSession`` / ``GetSessionEndpoint`` /
``TerminateSession``). ``emr-containers`` does not: EMR on EKS has only the
``SPARK_CONNECT`` managed endpoint, and ``GetManagedEndpointSessionCredentials``
returns an already-active token against it, so there is no session to start or
poll. `BaseSessionManager.provision` covers both shapes.
"""

from __future__ import annotations

import abc
import datetime
import logging
import re
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger("emr_spark_connect.backends")


# ---------------------------------------------------------------------------
# Resource ID patterns for auto-detection
# ---------------------------------------------------------------------------
# EMR Serverless application IDs: 16 alphanumeric chars starting with "00"
# e.g., 00abcdef01234567
_EMRS_APP_ID_RE = re.compile(r"^00[a-z0-9]{14}$")
# EMR on EC2 cluster IDs: start with j- followed by alphanumeric chars
# e.g., j-3HABCDEF8RG0E, j-08396852YQHF5PD3YGSX
_EMR_EC2_CLUSTER_ID_RE = re.compile(r"^j-[A-Z0-9]{10,20}$")
# EMR on EKS virtual cluster IDs: 20-25 lowercase alphanumeric chars (not starting with "00")
_EMR_EKS_VC_ID_RE = re.compile(r"^[a-z0-9]{20,25}$")


def detect_backend(resource_id: str) -> str:
    """Detect the EMR backend type from the resource ID format.

    Returns:
        One of 'serverless', 'ec2', or 'eks'.

    Raises:
        ValueError: If the resource ID format is not recognized.
    """
    if _EMR_EC2_CLUSTER_ID_RE.match(resource_id):
        return "ec2"
    if _EMRS_APP_ID_RE.match(resource_id):
        return "serverless"
    if _EMR_EKS_VC_ID_RE.match(resource_id):
        return "eks"
    raise ValueError(
        f"Cannot detect EMR backend from resource_id '{resource_id}'. "
        "Expected EMR Serverless application ID (16 chars starting with '00'), "
        "EMR on EC2 cluster ID (j-XXXXXXXXXXXX), or "
        "EMR on EKS virtual cluster ID (20-25 alphanumeric chars)."
    )


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------
class BaseSessionManager(abc.ABC):
    """Abstract manager for the remote resource backing a Spark Connect session.

    The two backend families are shaped differently, and the abstraction here is
    deliberately neutral about which one you get.

    **EMR Serverless and EMR on EC2 are session-based.** ``StartSession`` creates
    a session resource, ``GetSessionEndpoint`` returns both its URL and a fresh
    auth token, and ``TerminateSession`` ends it. There is no endpoint resource
    with a lifetime of its own — the endpoint host is a property of the session —
    so the only thing that expires is the token, and refreshing means minting a
    new one against the same session.

    **EMR on EKS has no session API at all.** ``emr-containers`` exposes no
    ``StartSession``/``GetSession``/``TerminateSession``; the only resource is the
    ``SPARK_CONNECT`` *managed endpoint*, and ``GetManagedEndpointSessionCredentials``
    hands back an already-active token against it. Nothing needs to be started
    and no session state needs polling — see `EKSSessionManager`.

    So `provision` means "make the remote resource exist and be usable", which is
    ``StartSession`` for the session-based backends and endpoint reuse-or-create
    for EKS. `session_id` is correspondingly the session ID or the managed
    endpoint ID.
    """

    def __init__(
        self,
        resource_id: str,
        region: str,
        execution_role_arn: Optional[str] = None,
        session_name: str = "emr-spark-connect",
        idle_timeout_minutes: int = 60,
        max_wait_seconds: int = 900,
        spark_conf: Optional[Dict[str, str]] = None,
        endpoint_url: Optional[str] = None,
        boto3_session: Optional[boto3.Session] = None,
    ):
        self.resource_id = resource_id
        self.region = region
        self.execution_role_arn = execution_role_arn
        self.session_name = session_name
        self.idle_timeout_minutes = idle_timeout_minutes
        self.max_wait_seconds = max_wait_seconds
        self.spark_conf = spark_conf or {}
        self.endpoint_url = endpoint_url
        self._boto3_session = boto3_session or boto3.Session()
        self._session_id: Optional[str] = None
        self._client: Any = None
        # Expiry of the token currently on the wire, recorded by
        # get_endpoint_and_token. None until the first token is minted.
        self._token_expires_at: Optional[datetime.datetime] = None

    @property
    def session_id(self) -> Optional[str]:
        """Session ID (Serverless/EC2) or managed endpoint ID (EKS)."""
        return self._session_id

    @abc.abstractmethod
    def _create_client(self) -> Any:
        """Create the boto3 client for this backend."""

    @abc.abstractmethod
    def provision(self) -> str:
        """Make the remote resource exist and be usable. Returns its ID.

        ``StartSession`` for the session-based backends; reuse-or-create of the
        managed endpoint for EMR on EKS, which has no session API.
        """

    @abc.abstractmethod
    def wait_for_ready(self) -> None:
        """Wait until the remote resource can accept connections."""

    @abc.abstractmethod
    def get_endpoint_and_token(self) -> Tuple[str, str, datetime.datetime]:
        """Get endpoint URL, auth token, and token expiry time."""

    @abc.abstractmethod
    def terminate_session(self) -> None:
        """Release the remote resource."""

    # States in which a session-based backend can still accept work. Shared by
    # Serverless and EC2, which use the same vocabulary.
    _SESSION_READY_STATES: Tuple[str, ...] = ("READY", "IDLE", "STARTED", "RUNNING", "BUSY")

    def ensure_endpoint(self, wait: bool = True) -> bool:
        """Guarantee a usable *endpoint* exists, creating one if it expired.

        No-op for EMR Serverless and EMR on EC2: their endpoint is a property of
        the session rather than a resource of its own, so it cannot expire
        separately and there is nothing to ensure — a token is simply minted
        against the existing session. `EKSSessionManager` overrides this.

        Returns:
            True if a new endpoint was created, False if an existing one is
            being reused (always False for the session-based backends).
        """
        return False

    def is_endpoint_active(self) -> bool:
        """Whether the endpoint is live.

        Always True for the session-based backends — their endpoint lives exactly
        as long as the session, so it never expires out from under them.
        """
        return True

    # -- token liveness -----------------------------------------------------
    # Separate from endpoint/session liveness on purpose. On EMR on EKS the two
    # run on independent timers, so "it stopped working" has two distinct causes
    # and only these methods can tell them apart. See `EKSSessionManager`.

    @property
    def token_expires_at(self) -> Optional[datetime.datetime]:
        """When the token currently on the wire expires, or None if none yet.

        Recorded by `get_endpoint_and_token` each time a token is minted, so it
        tracks the token the interceptor is actually sending — not the token this
        session started with.
        """
        return self._token_expires_at

    @property
    def token_duration_seconds(self) -> Optional[int]:
        """Requested token lifetime, or None if the backend does not accept one.

        Only EMR on EKS does: ``durationInSeconds`` on
        ``GetManagedEndpointSessionCredentials``. Serverless and EC2 mint tokens
        with a service-chosen lifetime that the client cannot influence.
        """
        return None

    def token_seconds_remaining(self) -> Optional[float]:
        """Seconds until the current token expires; negative once it has.

        None if no token has been minted yet.
        """
        if self._token_expires_at is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        return (self._token_expires_at - now).total_seconds()

    def is_token_expired(self) -> bool:
        """Whether the token currently on the wire has lapsed.

        This is a *local* check against the recorded ``expiresAt`` — it makes no
        API call, so it cannot say anything about the endpoint behind the token.
        A True here on its own is rarely a problem: the interceptor remints
        before every call whose cached token is within five minutes of expiry.
        Its use is diagnostic — pairing it with `is_endpoint_active` separates a
        token that lapsed from an endpoint that timed out.
        """
        remaining = self.token_seconds_remaining()
        return remaining is not None and remaining <= 0

    # -- session liveness (session-based backends) --------------------------
    # These sit on the base class because the *question* is universal, but only
    # Serverless and EC2 have a session to ask about. EKS overrides them to
    # defer to the managed endpoint, which is its only resource.

    def session_state(self) -> str:
        """Current state of the remote session, or ``NOT_FOUND`` if it is gone.

        Subclasses implement this against their own ``GetSession`` shape. The
        returned value is the service's own state string, uppercased.
        """
        raise NotImplementedError

    def is_session_active(self) -> bool:
        """Whether the remote session can still accept work.

        False means the session has hit ``idleTimeoutMinutes``, failed, or been
        terminated. Unlike an expired EMR on EKS managed endpoint, a dead
        session-based session cannot be revived or reattached to — its ID is
        permanently spent — so the only recovery is to start a new one. That is
        what ``EMRSparkSession.recreate()`` does.
        """
        try:
            return self.session_state() in self._SESSION_READY_STATES
        except NotImplementedError:
            raise
        except Exception as e:
            # An API error here means we cannot confirm liveness. Report not-live
            # rather than raising: callers use this to decide whether to recreate,
            # and a failed probe should steer them to recreate, not crash.
            logger.debug(f"Could not determine session state: {e}")
            return False

    def refresh_token(self) -> Tuple[str, datetime.datetime]:
        """Refresh the auth token. Used by the interceptor.

        For Serverless and EC2 this is the whole story: mint a new token against
        the existing session and hand it back for the interceptor to inject. The
        endpoint host never changes, so the open gRPC channel stays valid.

        Returns:
            Tuple of (auth_token, expiry_datetime)
        """
        _, token, expires_at = self.get_endpoint_and_token()
        return token, expires_at

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def build_spark_connect_url(self, endpoint: str, token: str) -> str:
        """Build the sc:// URL from the endpoint and token."""
        host = endpoint
        for scheme in ("sc://", "https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme) :]
                break
        # Remove trailing slash if present
        host = host.rstrip("/")
        return f"sc://{host}:443/;use_ssl=true;x-aws-proxy-auth={token}"


# ---------------------------------------------------------------------------
# EMR Serverless backend
# ---------------------------------------------------------------------------
class ServerlessSessionManager(BaseSessionManager):
    """Session manager for EMR Serverless (application IDs like 00abcdef01234567).

    Session-based: ``StartSession`` creates the session and ``GetSessionEndpoint``
    returns its endpoint plus a fresh token.
    """

    def _create_client(self) -> Any:
        kwargs: Dict[str, Any] = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        return self._boto3_session.client("emr-serverless", **kwargs)

    def provision(self) -> str:
        start_kwargs: Dict[str, Any] = {
            "applicationId": self.resource_id,
            "executionRoleArn": self.execution_role_arn,
            "name": self.session_name,
            "idleTimeoutMinutes": self.idle_timeout_minutes,
        }
        if self.spark_conf:
            start_kwargs["configurationOverrides"] = {
                "runtimeConfiguration": [
                    {
                        "classification": "spark-defaults",
                        "properties": self.spark_conf,
                    }
                ]
            }
        resp = self.client.start_session(**start_kwargs)
        self._session_id = resp["sessionId"]
        logger.info(f"Started EMR Serverless session: {self._session_id}")
        return self._session_id

    def wait_for_ready(self) -> None:
        _poll_resource_state(
            poll_fn=lambda: self.client.get_session(
                applicationId=self.resource_id, sessionId=self._session_id
            )["session"]["state"],
            ready_states=("READY", "IDLE", "STARTED"),
            terminal_states=("FAILED", "TERMINATED", "STOPPED"),
            resource_id=self._session_id,
            max_wait=self.max_wait_seconds,
        )

    def get_endpoint_and_token(self) -> Tuple[str, str, datetime.datetime]:
        resp = self.client.get_session_endpoint(
            applicationId=self.resource_id, sessionId=self._session_id
        )
        endpoint = resp["endpoint"]
        token = resp["authToken"]
        expires_at = resp["authTokenExpiresAt"]
        if isinstance(expires_at, str):
            expires_at = datetime.datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        self._token_expires_at = expires_at
        return endpoint, token, expires_at

    def session_state(self) -> str:
        """State of the interactive session, or ``NOT_FOUND`` if it is gone."""
        if not self._session_id:
            return "NOT_FOUND"
        try:
            resp = self.client.get_session(
                applicationId=self.resource_id, sessionId=self._session_id
            )
        except self.client.exceptions.ResourceNotFoundException:
            return "NOT_FOUND"
        return str(resp["session"]["state"]).upper()

    def terminate_session(self) -> None:
        if self._session_id:
            try:
                self.client.terminate_session(
                    applicationId=self.resource_id, sessionId=self._session_id
                )
                logger.info(f"Terminated EMR Serverless session: {self._session_id}")
            except Exception as e:
                logger.error(f"Failed to terminate session {self._session_id}: {e}")


# ---------------------------------------------------------------------------
# EMR on EC2 backend
# ---------------------------------------------------------------------------
class EC2SessionManager(BaseSessionManager):
    """Session manager for EMR on EC2 (cluster IDs like j-XXXXXXXXXXXXX).

    Session-based: ``StartSession`` creates the session and ``GetSessionEndpoint``
    returns its endpoint plus a fresh token.
    """

    def _create_client(self) -> Any:
        kwargs: Dict[str, Any] = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        return self._boto3_session.client("emr", **kwargs)

    def provision(self) -> str:
        start_kwargs: Dict[str, Any] = {
            "ClusterId": self.resource_id,
            "Name": self.session_name,
        }
        if self.execution_role_arn:
            start_kwargs["ExecutionRoleArn"] = self.execution_role_arn
        if self.idle_timeout_minutes:
            start_kwargs["SessionIdleTimeoutInMinutes"] = self.idle_timeout_minutes
        if self.spark_conf:
            start_kwargs["EngineConfigurations"] = [
                {
                    "Classification": "spark-defaults",
                    "Properties": self.spark_conf,
                }
            ]
        resp = self.client.start_session(**start_kwargs)
        self._session_id = resp["Id"]
        logger.info(f"Started EMR on EC2 session: {self._session_id}")
        return self._session_id

    def wait_for_ready(self) -> None:
        _poll_resource_state(
            poll_fn=lambda: self.client.get_session(
                ClusterId=self.resource_id, SessionId=self._session_id
            )["Session"]["State"],
            ready_states=("IDLE", "STARTED", "READY"),
            terminal_states=("FAILED", "TERMINATED"),
            resource_id=self._session_id,
            max_wait=self.max_wait_seconds,
        )

    def get_endpoint_and_token(self) -> Tuple[str, str, datetime.datetime]:
        resp = self.client.get_session_endpoint(
            ClusterId=self.resource_id, SessionId=self._session_id
        )
        endpoint = resp["Endpoint"]
        token = resp["AuthToken"]
        expires_at = resp.get("AuthTokenExpirationTime") or resp.get("AuthTokenExpiresAt")
        if isinstance(expires_at, str):
            expires_at = datetime.datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        self._token_expires_at = expires_at
        return endpoint, token, expires_at

    def build_spark_connect_url(self, endpoint: str, token: str) -> str:
        """EMR on EC2 also requires the session ID in the authorization parameter."""
        host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        return (
            f"sc://{host}:443/;use_ssl=true;"
            f"x-aws-proxy-auth={token};"
            f"authorization={self._session_id}"
        )

    def session_state(self) -> str:
        """State of the interactive session, or ``NOT_FOUND`` if it is gone."""
        if not self._session_id:
            return "NOT_FOUND"
        try:
            resp = self.client.get_session(
                ClusterId=self.resource_id, SessionId=self._session_id
            )
        except self.client.exceptions.InvalidRequestException:
            # emr has no ResourceNotFoundException for sessions; a spent or
            # unknown session ID comes back as InvalidRequestException.
            return "NOT_FOUND"
        return str(resp["Session"]["State"]).upper()

    def terminate_session(self) -> None:
        if self._session_id:
            try:
                self.client.terminate_session(
                    ClusterId=self.resource_id, SessionId=self._session_id
                )
                logger.info(f"Terminated EMR on EC2 session: {self._session_id}")
            except Exception as e:
                logger.error(f"Failed to terminate session {self._session_id}: {e}")


# ---------------------------------------------------------------------------
# EMR on EKS backend
# ---------------------------------------------------------------------------
class EndpointExpiredError(RuntimeError):
    """Raised when an expired managed endpoint could **not** be replaced.

    Expiry itself is not an error: the managed endpoint backing Spark Connect
    on EMR on EKS is torn down once ``sessionIdleTimeoutInMinutes`` elapses,
    and the client simply creates another one. This is raised only when that
    automatic replacement fails, so the caller is genuinely stuck.
    """


class EKSSessionManager(BaseSessionManager):
    """Session manager for EMR on EKS (virtual cluster IDs).

    There is no session to start
    ----------------------------
    ``emr-containers`` has no session API. The only resource is the
    ``SPARK_CONNECT`` *managed endpoint* on a virtual cluster, and
    ``GetManagedEndpointSessionCredentials`` returns an already-active session
    token against it. So there is nothing to start and no session state to poll:
    the endpoint is the whole resource, and provisioning it is the whole job.

    That leaves two things with independent lifetimes:

    ===============  =============================================  =========================================
    Thing            Lifetime                                       Renewed by
    ===============  =============================================  =========================================
    Managed          ``sessionIdleTimeoutInMinutes`` on              ``CreateManagedEndpoint`` — i.e. a new
    endpoint         ``CreateManagedEndpoint``                       endpoint; it cannot be extended
    Session token    ``durationInSeconds`` on                       ``GetManagedEndpointSessionCredentials``
                     ``GetManagedEndpointSessionCredentials``
                     (default 15 min, max 12 h)
    ===============  =============================================  =========================================

    A token is only valid against the endpoint it was minted for, so it cannot
    outlive its endpoint. Which one expired decides what has to be rebuilt:

    * **Token expired, endpoint still ACTIVE** — the common case, and the cheap
      one. Mint a new token against the same endpoint; the host is unchanged so
      the gRPC channel keeps working. Same shape as the other backends.
    * **Endpoint expired** — the token is moot. Create a new endpoint (routine,
      not an error), then mint a token against it. The replacement has a new
      ``authProxyUrl``, so the channel must also be rebuilt.

    `ensure_endpoint` is the single place that reuse-or-replace decision is made,
    and `provision`, `get_endpoint_and_token` and `EMRSparkSession.reconnect()`
    all route through it, so a token is never minted against a dead endpoint.
    `refresh_token` handles the token and, when it finds the endpoint was replaced
    underneath it, signals that the caller must reconnect.
    """

    #: EMR on EKS release used when creating a Spark Connect managed endpoint.
    DEFAULT_RELEASE_LABEL = "emr-7.14.0-latest"
    #: Token lifetime requested from GetManagedEndpointSessionCredentials.
    #: API default is 15 minutes; 12 hours (43200s) is the documented maximum.
    DEFAULT_TOKEN_DURATION_SECONDS = 12 * 60 * 60
    #: Documented maximum for durationInSeconds; larger values are rejected.
    MAX_TOKEN_DURATION_SECONDS = 12 * 60 * 60
    #: Below this, tokens routinely expire in transit: the 30-60s budget has to
    #: cover the mint round trip, connection setup, and retry backoff before
    #: the auth proxy validates the token — losing that race means the RPC is
    #: rejected at the proxy and never reaches the Spark driver. 300s is the
    #: floor at which a token reliably outlives one dispatch.
    MIN_SANE_TOKEN_DURATION_SECONDS = 300
    #: An endpoint with a tiny idle timeout deletes itself between demo cells:
    #: retried-but-failing RPCs don't count as activity, so the whole driver
    #: deployment is reaped mid-session and every temp view with it.
    MIN_SANE_IDLE_TIMEOUT_MINUTES = 5

    # describe_managed_endpoint states
    _READY_STATES = ("ACTIVE",)
    _DEAD_STATES = ("TERMINATED", "TERMINATED_WITH_ERRORS", "TERMINATING")

    def __init__(
        self,
        *args,
        managed_endpoint_id: Optional[str] = None,
        release_label: Optional[str] = None,
        token_duration_seconds: Optional[int] = None,
        application_configuration: Optional[List[Dict[str, Any]]] = None,
        monitoring_configuration: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # Endpoint id supplied by the caller. We reuse it and never delete it —
        # deleting a caller-owned endpoint would be a surprising side effect.
        self._managed_endpoint_id = managed_endpoint_id
        self._owns_endpoint = False
        self._release_label = release_label or self.DEFAULT_RELEASE_LABEL
        if token_duration_seconds is not None:
            if token_duration_seconds > self.MAX_TOKEN_DURATION_SECONDS:
                raise ValueError(
                    f"token_duration_seconds={token_duration_seconds} exceeds the "
                    f"GetManagedEndpointSessionCredentials maximum of "
                    f"{self.MAX_TOKEN_DURATION_SECONDS} (12 hours)."
                )
            if token_duration_seconds < self.MIN_SANE_TOKEN_DURATION_SECONDS:
                warnings.warn(
                    f"token_duration_seconds={token_duration_seconds} is below "
                    f"{self.MIN_SANE_TOKEN_DURATION_SECONDS}s. Such tokens can "
                    "expire in transit — the auth proxy then rejects RPCs before "
                    "they reach the Spark driver — and every gRPC call will mint "
                    "a fresh token (an extra AWS API round trip per call). The "
                    "API default is 900s.",
                    stacklevel=3,
                )
        self._token_duration_seconds = (
            token_duration_seconds
            if token_duration_seconds is not None
            else self.DEFAULT_TOKEN_DURATION_SECONDS
        )
        if self.idle_timeout_minutes and (
            self.idle_timeout_minutes < self.MIN_SANE_IDLE_TIMEOUT_MINUTES
        ):
            warnings.warn(
                f"idle_timeout_minutes={self.idle_timeout_minutes} is below "
                f"{self.MIN_SANE_IDLE_TIMEOUT_MINUTES}. On EMR on EKS this is the "
                "managed endpoint's sessionIdleTimeoutInMinutes: the endpoint "
                "deletes its own driver deployment after that many idle minutes, "
                "and the replacement comes up on a new host with none of the old "
                "driver state.",
                stacklevel=3,
            )
        # Serializes token minting and endpoint replacement. Without this, the
        # interceptor's refresh and PySpark's background ReleaseExecute threads
        # can race ensure_endpoint into creating two replacement endpoints.
        self._refresh_lock = threading.RLock()
        self._application_configuration = application_configuration
        self._monitoring_configuration = monitoring_configuration
        # authProxyUrl of the endpoint currently in use. Tracked so we can tell
        # a token refresh (same host) from an endpoint swap (new host).
        self._endpoint_host: Optional[str] = None
        # Most recent DescribeManagedEndpoint result, set by describe_endpoint().
        self._last_endpoint: Optional[Dict[str, Any]] = None

    def _create_client(self) -> Any:
        kwargs: Dict[str, Any] = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        return self._boto3_session.client("emr-containers", **kwargs)

    # -- the managed endpoint (the only resource; no session to start) ------
    def provision(self) -> str:
        """Reuse a live managed endpoint if there is one, else create one.

        This is the entire provisioning step for EMR on EKS — there is no
        ``StartSession`` to call afterwards. ``wait_for_ready`` is invoked
        separately by the caller, matching the other backends, so this does not
        block on a newly created endpoint.
        """
        self.ensure_endpoint(wait=False)
        return self._session_id

    def ensure_endpoint(self, wait: bool = True) -> bool:
        """Guarantee a usable managed endpoint exists, creating one if needed.

        This concerns only the endpoint — it says nothing about the session token,
        which `get_endpoint_and_token` mints against it afterwards. It is the one
        place the reuse-or-replace decision is made:

        * An endpoint that is ``ACTIVE`` (or still ``CREATING``) is **reused**
          as-is — no new endpoint, no redundant ALB provisioning.
        * An endpoint that has expired, is terminating, or no longer exists is
          **replaced** with a new one. Expiry after
          ``sessionIdleTimeoutInMinutes`` is the normal end of an endpoint's
          life, so it is handled routinely rather than raised as an error.

        Replacement applies equally to an endpoint passed in as
        ``managed_endpoint_id``: once it has expired there is nothing to reuse,
        so the client stops referring to that id and manages its own from then
        on (otherwise it would keep describing the same dead id forever).

        Args:
            wait: Poll a newly created (or still-``CREATING``) endpoint until it
                reaches ``ACTIVE``.

        Returns:
            True if a new endpoint was created, False if an existing one is
            being reused. Callers use this to tell whether the ``authProxyUrl``
            may have changed under them.
        """
        # Serialized so two concurrent callers (e.g. the interceptor's refresh
        # and reconnect()) can't both see a dead endpoint and create two
        # replacements. RLock: get_endpoint_and_token calls this while holding it.
        with self._refresh_lock:
            endpoint_id = self._session_id or self._managed_endpoint_id
            if endpoint_id:
                self._session_id = endpoint_id
                state = self.endpoint_state()
                if state in self._READY_STATES or state == "CREATING":
                    logger.info(
                        f"Reusing EMR on EKS managed endpoint {endpoint_id} "
                        f"(state {state})"
                    )
                    if wait and state not in self._READY_STATES:
                        self.wait_for_ready()
                    return False
                logger.info(
                    f"EMR on EKS managed endpoint {endpoint_id} is no longer usable "
                    f"(state {state}) — creating a replacement"
                )
                # The caller's endpoint is gone; from here on we manage our own.
                self._managed_endpoint_id = None

            self._create_endpoint()
            if wait:
                self.wait_for_ready()
            if endpoint_id:
                logger.info(
                    f"Replaced EMR on EKS managed endpoint: "
                    f"{endpoint_id} -> {self._session_id}"
                )
            return True

    def _build_configuration_overrides(self) -> Dict[str, Any]:
        """Assemble ``configurationOverrides`` for CreateManagedEndpoint.

        Customer Spark configuration reaches the endpoint two ways, and both are
        honoured:

        * ``spark_conf={"spark.executor.memory": "4g"}`` — the common case, a
          flat dict promoted to a ``spark-defaults`` classification.
        * ``application_configuration=[{...}]`` — the full API shape, for
          anything a flat dict cannot express: other classifications
          (``spark-env``, ``spark-hive-site``, ``jeg-config``, ...) and nested
          ``configurations``.

        When both are given, ``spark_conf`` is merged into the caller's
        ``spark-defaults`` block if there is one (caller's explicit properties
        win on key conflicts) and appended as a new block otherwise. That keeps
        both from silently overwriting each other.
        """
        app_config: List[Dict[str, Any]] = [
            dict(block) for block in (self._application_configuration or [])
        ]

        if self.spark_conf:
            existing = next(
                (
                    b
                    for b in app_config
                    if b.get("classification") == "spark-defaults"
                ),
                None,
            )
            if existing is None:
                app_config.append(
                    {
                        "classification": "spark-defaults",
                        "properties": dict(self.spark_conf),
                    }
                )
            else:
                # Caller's explicitly-listed properties take precedence.
                merged = dict(self.spark_conf)
                merged.update(existing.get("properties") or {})
                existing["properties"] = merged

        overrides: Dict[str, Any] = {}
        if app_config:
            overrides["applicationConfiguration"] = app_config
        if self._monitoring_configuration:
            overrides["monitoringConfiguration"] = self._monitoring_configuration
        return overrides

    def _create_endpoint(self) -> str:
        create_kwargs: Dict[str, Any] = {
            "name": self.session_name,
            "virtualClusterId": self.resource_id,
            "type": "SPARK_CONNECT",
            "releaseLabel": self._release_label,
            "executionRoleArn": self.execution_role_arn,
        }
        # The endpoint is torn down this many idle minutes after creation.
        if self.idle_timeout_minutes:
            create_kwargs["sessionIdleTimeoutInMinutes"] = self.idle_timeout_minutes
        overrides = self._build_configuration_overrides()
        if overrides:
            create_kwargs["configurationOverrides"] = overrides
        # clientToken carries the idempotencyToken trait, so boto3 generates it.
        resp = self.client.create_managed_endpoint(**create_kwargs)
        self._session_id = resp["id"]
        self._owns_endpoint = True
        self._endpoint_host = None
        # Both describe the endpoint we just replaced — drop them.
        self._last_endpoint = None
        logger.info(
            f"Created EMR on EKS managed endpoint {self._session_id} "
            f"({self._release_label}, idle timeout "
            f"{self.idle_timeout_minutes} min)"
        )
        return self._session_id

    def describe_endpoint(self) -> Dict[str, Any]:
        """Return the ``endpoint`` struct from DescribeManagedEndpoint."""
        endpoint = self.client.describe_managed_endpoint(
            virtualClusterId=self.resource_id, id=self._session_id
        )["endpoint"]
        # Cached so the reuse path doesn't describe twice per token mint.
        self._last_endpoint = endpoint
        return endpoint

    def session_state(self) -> str:
        """The managed endpoint's state — EMR on EKS has no session resource.

        ``emr-containers`` exposes no ``GetSession``, so "is the session alive"
        can only mean "is the managed endpoint alive" here.
        """
        return self.endpoint_state()

    def is_session_active(self) -> bool:
        """Whether the managed endpoint is live.

        Deliberately identical to ``is_endpoint_active``. On EMR on EKS an
        expired endpoint is *replaced in place* by ``ensure_endpoint``, so
        ``EMRSparkSession.reconnect()`` remains the recovery path — recreating
        the whole session is neither needed nor correct.
        """
        return self.is_endpoint_active()

    def endpoint_state(self) -> str:
        """Current endpoint state, or ``NOT_FOUND`` if it no longer exists."""
        try:
            return self.describe_endpoint()["state"]
        except self.client.exceptions.ResourceNotFoundException:
            self._last_endpoint = None
            return "NOT_FOUND"

    def is_endpoint_active(self) -> bool:
        """Whether the managed endpoint is currently live.

        False means it has hit ``sessionIdleTimeoutInMinutes``; the next call
        needing it will create a replacement.
        """
        return self.endpoint_state() in self._READY_STATES

    @property
    def token_duration_seconds(self) -> Optional[int]:
        """The ``durationInSeconds`` requested for each token.

        Independent of ``sessionIdleTimeoutInMinutes``, so a token can easily
        outlive its endpoint or lapse long before it.
        """
        return self._token_duration_seconds

    def wait_for_ready(self) -> None:
        """Poll the endpoint to ACTIVE. There is no session state to wait on."""
        # Endpoint creation provisions a load balancer, so this is minutes, not
        # seconds. CREATING is the normal pre-ACTIVE state.
        _poll_resource_state(
            poll_fn=self.endpoint_state,
            ready_states=self._READY_STATES,
            terminal_states=self._DEAD_STATES + ("NOT_FOUND",),
            resource_id=self._session_id,
            max_wait=self.max_wait_seconds,
            resource_kind="Managed endpoint",
        )

    # -- the session token --------------------------------------------------
    def get_endpoint_and_token(self) -> Tuple[str, str, datetime.datetime]:
        """Return ``(authProxyUrl, token, expires_at)`` for a live endpoint.

        The endpoint is settled first via `ensure_endpoint` — reused if ``ACTIVE``,
        replaced if it hit ``sessionIdleTimeoutInMinutes`` — because a token is
        only valid against the endpoint it was minted for and must never be minted
        against a dead one. ``GetManagedEndpointSessionCredentials`` then returns an
        already-active token with its own independent ``durationInSeconds``; no
        session is started, and the token is usable immediately.

        ``DescribeManagedEndpoint`` can keep reporting ``ACTIVE`` for a window
        after the endpoint has actually hit its idle timeout, so the reuse
        decision above can be wrong. The credentials API validates against the
        endpoint's real state and rejects such an endpoint with
        ``ValidationException: Endpoint is in invalid state``. That rejection is
        treated exactly like an observed expiry: the endpoint is replaced and
        the mint retried once against the replacement.

        Because the credentials API does that validation anyway, an endpoint
        this manager has already connected to is minted against directly,
        without a preceding ``DescribeManagedEndpoint`` — halving the AWS calls
        on the common refresh path. The describe-first path runs on the first
        connection and whenever a direct mint is rejected.
        """
        with self._refresh_lock:
            # Optimistic path: the endpoint served us before, so mint straight
            # against it and let the credentials API be the liveness check.
            if self._endpoint_host and self._session_id:
                try:
                    return self._mint_token(self._endpoint_host)
                except self.client.exceptions.ValidationException as e:
                    if "state" not in str(e).lower():
                        raise
                    logger.info(
                        f"Managed endpoint {self._session_id} rejected a token "
                        f"mint ({e}) — falling back to describe-and-replace"
                    )
                    # Stale by definition now; force the full path to re-look.
                    self._last_endpoint = None

            # Reuses the current endpoint when it is still ACTIVE; creates a
            # replacement when it has timed out. Either way, describe_endpoint()
            # has run and cached the result, so no second describe is needed.
            self.ensure_endpoint(wait=True)

            host = self._resolve_endpoint_host()
            try:
                return self._mint_token(host)
            except self.client.exceptions.ValidationException as e:
                # Only a state complaint means "the endpoint is dead behind an
                # ACTIVE describe". Anything else (bad duration, bad role, ...)
                # would fail identically against a replacement, so re-raise.
                if "state" not in str(e).lower():
                    raise
                logger.info(
                    f"Managed endpoint {self._session_id} rejected a token mint "
                    f"({e}) — it expired behind an ACTIVE describe; creating a "
                    "replacement"
                )
                # Same handover as ensure_endpoint: the caller's endpoint is
                # gone, so from here on we manage our own.
                self._managed_endpoint_id = None
                self._create_endpoint()
                self.wait_for_ready()
                return self._mint_token(self._resolve_endpoint_host())

    def _resolve_endpoint_host(self) -> str:
        """Return the Spark Connect host of the current endpoint, recording it."""
        endpoint = self._last_endpoint or self.describe_endpoint()
        state = endpoint.get("state")
        if state not in self._READY_STATES:
            # ensure_endpoint just brought an endpoint to ACTIVE, so this means
            # the replacement itself is unusable — a real failure.
            raise EndpointExpiredError(
                f"Managed endpoint {self._session_id} is in state {state}, "
                "not ACTIVE, immediately after being ensured."
            )
        # authProxyUrl is the Spark Connect host; serverUrl is not.
        host = endpoint.get("authProxyUrl") or endpoint.get("serverUrl")
        if not host:
            raise EndpointExpiredError(
                f"Managed endpoint {self._session_id} exposed no authProxyUrl."
            )
        self._endpoint_host = host
        return host

    def _mint_token(self, host: str) -> Tuple[str, str, datetime.datetime]:
        """Mint a session token against the current endpoint."""
        resp = self.client.get_managed_endpoint_session_credentials(
            virtualClusterIdentifier=self.resource_id,
            endpointIdentifier=self._session_id,
            executionRoleArn=self.execution_role_arn,
            credentialType="TOKEN",
            durationInSeconds=self._token_duration_seconds,
        )
        # `credentials` and `endpointCredentials` are equivalent unions; the
        # docs' example reads credentials.token.
        creds = resp.get("credentials") or resp.get("endpointCredentials") or {}
        token = creds.get("token")
        if not token:
            raise RuntimeError(
                "GetManagedEndpointSessionCredentials returned no token for "
                f"endpoint {self._session_id}"
            )

        expires_at = resp.get("expiresAt")
        if expires_at is None:
            expires_at = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(seconds=self._token_duration_seconds)
        elif isinstance(expires_at, str):
            expires_at = datetime.datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        self._token_expires_at = expires_at
        return host, token, expires_at

    def refresh_token(self) -> Tuple[str, datetime.datetime]:
        """Mint a fresh token for the endpoint the open channel points at.

        The gRPC interceptor calls this when the *token* is near expiry. Which of
        the two actually expired decides the outcome:

        * **Only the token expired** (endpoint still ``ACTIVE``) — the common
          case. A new token is minted against the same endpoint and returned for
          the interceptor to inject. The host is unchanged, so the open channel
          keeps working. Indistinguishable from Serverless/EC2 behaviour.
        * **The endpoint expired too** — `get_endpoint_and_token` has already
          created the replacement, which is not an error and needs no caller
          involvement. But an interceptor can only rewrite a header, and the
          replacement listens on a *different* ``authProxyUrl``, so the open
          channel points at a host that no longer answers. That is the one case
          raised as `EndpointExpiredError` — the new endpoint is already up, and
          `EMRSparkSession.reconnect()` simply attaches to it.
        """
        previous_host = self._endpoint_host
        host, token, expires_at = self.get_endpoint_and_token()
        if previous_host and host != previous_host:
            raise EndpointExpiredError(
                f"Managed endpoint expired and was replaced with "
                f"{self._session_id} at a new host ({previous_host} -> {host}). "
                "The open Spark Connect channel cannot be redirected — call "
                "EMRSparkSession.reconnect() to attach to the new endpoint."
            )
        return token, expires_at

    def build_spark_connect_url(self, endpoint: str, token: str) -> str:
        """Build the ``sc://`` URL from ``authProxyUrl`` and a session token."""
        host = endpoint
        for scheme in ("sc://", "https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme) :]
                break
        host = host.rstrip("/")
        if ":" not in host:
            host = f"{host}:443"
        return f"sc://{host}/;use_ssl=true;x-aws-proxy-auth={token}"

    def terminate_session(self) -> None:
        """Delete the managed endpoint. There is no session to terminate."""
        # Only delete endpoints we created; a caller-supplied endpoint is theirs.
        if not self._session_id or not self._owns_endpoint:
            return
        try:
            self.client.delete_managed_endpoint(
                virtualClusterId=self.resource_id, id=self._session_id
            )
            logger.info(f"Deleted EMR on EKS managed endpoint: {self._session_id}")
        except Exception as e:
            logger.error(
                f"Failed to delete managed endpoint {self._session_id}: {e}"
            )


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------
def _poll_resource_state(
    poll_fn,
    ready_states: Tuple[str, ...],
    terminal_states: Tuple[str, ...],
    resource_id: str,
    max_wait: int,
    poll_interval: float = 3.0,
    resource_kind: str = "Session",
) -> None:
    """Poll until the remote resource reaches a ready or terminal state.

    Args:
        resource_kind: What is being polled, for log/error text — a session on
            Serverless/EC2, a managed endpoint on EMR on EKS (which has no
            session resource to poll).
    """
    start = time.time()
    last_state = None
    while True:
        elapsed = time.time() - start
        if elapsed >= max_wait:
            raise TimeoutError(
                f"{resource_kind} {resource_id} not ready after {max_wait}s "
                f"(last state: {last_state})"
            )
        state = poll_fn()
        if state != last_state:
            logger.info(f"{resource_kind} {resource_id}: {state}")
            last_state = state
        if state in ready_states:
            return
        if state in terminal_states:
            raise RuntimeError(
                f"{resource_kind} {resource_id} reached terminal state: {state}"
            )
        time.sleep(poll_interval)


def create_session_manager(
    resource_id: str,
    backend: Optional[str] = None,
    **kwargs,
) -> BaseSessionManager:
    """Factory to create the appropriate session manager.

    Args:
        resource_id: EMR resource ID (app ID, cluster ID, or virtual cluster ID)
        backend: Explicit backend ('serverless', 'ec2', 'eks') or None for auto-detect
        **kwargs: Passed to the session manager constructor

    Returns:
        An initialized (but not yet started) session manager
    """
    if backend is None:
        backend = detect_backend(resource_id)

    managers = {
        "serverless": ServerlessSessionManager,
        "ec2": EC2SessionManager,
        "eks": EKSSessionManager,
    }
    if backend not in managers:
        raise ValueError(
            f"Unknown backend '{backend}'. Must be one of: {list(managers.keys())}"
        )
    return managers[backend](resource_id=resource_id, **kwargs)
