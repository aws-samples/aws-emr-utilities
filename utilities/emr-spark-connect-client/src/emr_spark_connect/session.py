# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""Unified EMRSparkSession — the main entry point for the package.

Usage:
    from emr_spark_connect import EMRSparkSession

    session = EMRSparkSession.create(
        resource_id="00abcdef01234567",  # EMR Serverless app ID
        execution_role_arn="arn:aws:iam::123456789012:role/MyRole",
    )
    session.sql("SELECT 1 + 1 AS result").show()
    session.stop()
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import boto3
from pyspark.sql import SparkSession
from pyspark.sql.connect.session import SparkSession as ConnectSparkSession

from .backends import BaseSessionManager, EndpointExpiredError, create_session_manager
from .interceptors import EMRChannelBuilder

logger = logging.getLogger("emr_spark_connect")


def _discard_channel(spark: Optional[SparkSession], remote_gone: bool) -> None:
    """Close a stale Spark Connect channel without waiting on a dead host.

    ``stop()`` on PySpark 4.x first sends a ``ReleaseSession`` RPC to the remote
    driver. When that driver is gone — an idled-out session, an expired managed
    endpoint — the RPC gets ``UNAVAILABLE``, which the default retry policy
    treats as retryable: 15 attempts with exponential backoff capped at 60s,
    so ``stop()`` blocks for over ten minutes before giving up. That looks
    exactly like a hang in :meth:`EMRSparkSession.recreate`.

    PySpark 3.5's ``stop()`` has no such RPC, which is why this only bites the
    4.x line (EMR on EC2). ``release_session_on_close`` is likewise 4.x-only,
    so it is set defensively rather than assumed to exist.

    Args:
        spark: The SparkSession to close. None is a no-op.
        remote_gone: True if the remote driver is known to be unreachable, in
            which case the release RPC is skipped as pointless. When False the
            RPC is left in place so a live remote session is released cleanly.
    """
    if spark is None:
        return
    if remote_gone:
        try:
            spark.release_session_on_close = False
        except Exception as e:  # pragma: no cover - attribute is 4.x-only
            logger.debug(f"Could not disable ReleaseSession on close: {e}")
    try:
        spark.stop()
    except Exception as e:
        logger.debug(f"Ignoring error stopping stale SparkSession: {e}")


def _connect(manager: BaseSessionManager) -> SparkSession:
    """Build a SparkSession against a ready endpoint, with token auto-refresh.

    Assumes the manager's session/endpoint is already in a ready state.
    """
    endpoint, token, _expires_at = manager.get_endpoint_and_token()
    spark_connect_url = manager.build_spark_connect_url(endpoint, token)
    logger.info(f"Connecting to Spark Connect at {endpoint}")

    channel_builder = EMRChannelBuilder(
        url=spark_connect_url,
        refresh_fn=manager.refresh_token,
    )
    # create() rather than getOrCreate(): getOrCreate would hand back the
    # cached session bound to a previous (now stale) channel on reconnect.
    spark = ConnectSparkSession.builder.channelBuilder(channel_builder).create()
    logger.info("SparkSession created with automatic token refresh")
    return spark


class EMRSparkSession:
    """Unified Spark Connect session for EMR Serverless, EMR on EKS, and EMR on EC2.

    Manages the full lifecycle:
    - Detects backend from resource_id format (or use explicit backend= param)
    - Starts an interactive session
    - Waits for session readiness
    - Creates a PySpark SparkSession with automatic token refresh
    - Provides session termination on stop()

    The returned object delegates attribute access to the underlying SparkSession,
    so you can use it exactly like a regular SparkSession:
        session.sql("...").show()
        session.read.parquet("s3://...")
        session.createDataFrame(...)
    """

    def __init__(
        self,
        spark: SparkSession,
        manager: BaseSessionManager,
    ):
        self._spark = spark
        self._manager = manager

    @classmethod
    def create(
        cls,
        resource_id: str,
        execution_role_arn: Optional[str] = None,
        region: Optional[str] = None,
        backend: Optional[str] = None,
        session_name: str = "emr-spark-connect",
        idle_timeout_minutes: int = 60,
        max_wait_seconds: int = 900,
        spark_conf: Optional[Dict[str, str]] = None,
        endpoint_url: Optional[str] = None,
        boto3_session: Optional[boto3.Session] = None,
        **kwargs,
    ) -> "EMRSparkSession":
        """Create a SparkSession connected to an EMR backend with automatic token refresh.

        Args:
            resource_id: The EMR resource to connect to:
                - EMR Serverless application ID (16 chars starting with '00',
                  e.g. '00abcdef01234567')
                - EMR on EC2 cluster ID (e.g. 'j-XXXXXXXXXXXXX')
                - EMR on EKS virtual cluster ID (20-25 alphanumeric chars)
            execution_role_arn: IAM role ARN for the session. Required for EMR Serverless
                and EMR on EKS. Optional for EMR on EC2 (runtime role sessions).
            region: AWS region. If None, uses the default from boto3 session/environment.
            backend: Explicit backend type ('serverless', 'ec2', 'eks'). If None,
                auto-detected from resource_id format.
            session_name: Human-readable session name.
            idle_timeout_minutes: Auto-terminate session after this many idle minutes.
            max_wait_seconds: Maximum time to wait for session to become ready.
            spark_conf: Optional Spark configuration overrides (dict of key→value),
                applied as a `spark-defaults` classification.
            endpoint_url: Custom AWS service endpoint URL (for testing/beta).
            boto3_session: Optional pre-configured boto3.Session to use.
            **kwargs: Additional backend-specific arguments. For EMR on EKS:
                - managed_endpoint_id: reuse an existing SPARK_CONNECT managed
                  endpoint instead of creating one.
                - release_label: EMR release for the endpoint
                  (default 'emr-7.14.0-latest').
                - token_duration_seconds: session token TTL (default 12h, the max).
                - application_configuration: full `applicationConfiguration` list,
                  for classifications and nested configs `spark_conf` can't express.
                - monitoring_configuration: `monitoringConfiguration` dict
                  (CloudWatch/S3 logging, persistent app UI).

        Returns:
            EMRSparkSession wrapping a fully-connected PySpark SparkSession.

        Raises:
            ValueError: If resource_id format is unrecognized and backend is not specified.
            TimeoutError: If session does not become ready within max_wait_seconds.
            RuntimeError: If session reaches a terminal/failed state.

        Example:
            >>> from emr_spark_connect import EMRSparkSession
            >>> session = EMRSparkSession.create(
            ...     resource_id="00abcdef01234567",
            ...     execution_role_arn="arn:aws:iam::123456789012:role/SparkRole",
            ...     region="us-east-1",
            ... )
            >>> session.sql("SELECT 1 + 1 AS result").show()
            +------+
            |result|
            +------+
            |     2|
            +------+
            >>> session.stop()
        """
        # Resolve region
        if region is None:
            sess = boto3_session or boto3.Session()
            region = sess.region_name or "us-east-1"

        # Create the appropriate backend session manager
        manager = create_session_manager(
            resource_id=resource_id,
            backend=backend,
            region=region,
            execution_role_arn=execution_role_arn,
            session_name=session_name,
            idle_timeout_minutes=idle_timeout_minutes,
            max_wait_seconds=max_wait_seconds,
            spark_conf=spark_conf,
            endpoint_url=endpoint_url,
            boto3_session=boto3_session,
            **kwargs,
        )

        # Provision the remote resource and wait for it to be ready. That is
        # StartSession on Serverless/EC2; on EMR on EKS there is no session API,
        # so it reuses or creates the SPARK_CONNECT managed endpoint instead.
        manager.provision()
        manager.wait_for_ready()

        spark = _connect(manager)
        return cls(spark=spark, manager=manager)

    @property
    def spark(self) -> SparkSession:
        """Access the underlying PySpark SparkSession directly."""
        return self._spark

    @property
    def session_id(self) -> Optional[str]:
        """The EMR session ID, or on EMR on EKS the managed endpoint ID.

        EMR on EKS has no session resource — ``emr-containers`` exposes no
        ``StartSession`` — so the managed endpoint is the identity of the remote
        Spark driver there.
        """
        return self._manager.session_id

    @property
    def resource_id(self) -> str:
        """The EMR resource ID (application ID, cluster ID, or virtual cluster ID)."""
        return self._manager.resource_id

    def reconnect(self) -> "EMRSparkSession":
        """Rebuild the Spark Connect channel, replacing the endpoint if expired.

        This exists for EMR on EKS, the one backend whose endpoint is a resource
        in its own right with its own timer. The endpoint side is handled
        routinely — a still-``ACTIVE`` endpoint is reused, and one that hit
        ``sessionIdleTimeoutInMinutes`` is replaced automatically.

        What cannot be automated away is the channel: a replacement endpoint
        listens on a new ``authProxyUrl``, so the open gRPC channel points at a
        dead host and a token refresh cannot redirect it. This method drops that
        channel and builds a new SparkSession against whichever endpoint is live.

        If the endpoint was replaced, the new one is a new Spark driver — temp
        views and cached DataFrames from before are gone and need re-registering.

        Returns:
            self, so you can chain: ``session.reconnect().sql("SELECT 1").show()``
        """
        # Drop the stale channel first; its target host may no longer answer.
        # An expired endpoint's ALB is gone, so skip the release RPC there.
        _discard_channel(self._spark, remote_gone=not self._manager.is_endpoint_active())
        self._spark = None

        # EKS: reuse the endpoint if live, create one if expired.
        # Serverless/EC2: no separate endpoint resource, so this is a no-op and
        # the existing session is reused as-is.
        self._manager.ensure_endpoint(wait=True)

        self._spark = _connect(self._manager)
        return self

    def is_endpoint_active(self) -> bool:
        """Whether the remote endpoint is currently live.

        Meaningful only on EMR on EKS, where False means the managed endpoint
        has expired and the next call that needs it will create a replacement.
        On Serverless and EC2 the endpoint belongs to the session and cannot
        expire separately, so they always report True.
        """
        return self._manager.is_endpoint_active()

    @property
    def token_expires_at(self):
        """When the auth token currently on the wire expires (UTC), or None.

        None before the first token is minted. This tracks the token the gRPC
        interceptor is actually sending, so it moves forward on every refresh.
        """
        return self._manager.token_expires_at

    @property
    def token_duration_seconds(self) -> Optional[int]:
        """The token lifetime this session requests, or None if not settable.

        Only EMR on EKS accepts one — ``token_duration_seconds`` on
        :meth:`create`, passed through as ``durationInSeconds`` to
        ``GetManagedEndpointSessionCredentials``. The service honours it exactly,
        up to the documented 12 h maximum (43200s); larger values are rejected.
        Serverless and EC2 report None: their token lifetime is service-chosen.
        """
        return self._manager.token_duration_seconds

    def token_seconds_remaining(self) -> Optional[float]:
        """Seconds until the current token expires; negative once it has."""
        return self._manager.token_seconds_remaining()

    def is_token_expired(self) -> bool:
        """Whether the token currently on the wire has lapsed.

        A local check against the recorded ``expiresAt`` — no API call, and it
        says nothing about the endpoint behind the token. True on its own is
        usually benign: the interceptor remints before any call whose token is
        within five minutes of expiry.

        On EMR on EKS the endpoint and the token expire on independent clocks,
        so pair this with :meth:`is_endpoint_active` to tell the two apart. A
        token is only ever valid against the endpoint it was minted for, so
        check the endpoint first — once it has expired, the token is moot.
        """
        return self._manager.is_token_expired()

    def session_state(self) -> str:
        """The remote session's state as the service reports it.

        ``NOT_FOUND`` if the session no longer exists. On EMR on EKS, which has
        no session resource, this reports the managed endpoint's state.
        """
        return self._manager.session_state()

    def is_active(self) -> bool:
        """Whether the remote session can still accept work.

        Use this before reusing a session that has been idle — a session that
        hit ``idle_timeout_minutes`` is gone, and the next query against it
        fails with a gRPC or botocore error rather than anything catchable as
        ``EndpointExpiredError``.

        Recovery differs by backend, because the underlying resource differs:

        * **EMR Serverless / EMR on EC2** — the session *is* the endpoint. A
          spent session ID cannot be revived, so call :meth:`recreate`.
        * **EMR on EKS** — the managed endpoint is replaceable underneath a
          live client, so call :meth:`reconnect`. This method is a synonym for
          :meth:`is_endpoint_active` there.

        Note this reports on the *remote* session only. It stays True after a
        local ``stop()`` with ``terminate=False``, since the remote session is
        indeed still alive; use :attr:`spark` being None to test the local side.
        """
        return self._manager.is_session_active()

    def recreate(self) -> "EMRSparkSession":
        """Start a fresh remote session and reattach, reusing the original settings.

        For EMR Serverless and EMR on EC2, where an idled-out session cannot be
        revived. The new session inherits every argument the original was
        created with, so a caller does not have to restate them.

        The result is a **new remote session with a new ID**. Temp views, cached
        DataFrames and any other driver-side state from the old session are gone.
        On EMR on EC2 the session ID is embedded in the Spark Connect URL, so the
        channel is necessarily rebuilt too.

        Mutates and returns ``self``, so an existing variable stays valid:

            if not session.is_active():
                session.recreate()

        Raises:
            NotImplementedError: On EMR on EKS. Its managed endpoint is replaced
                in place by ``reconnect()``; recreating the session is neither
                needed nor correct.
        """
        from .backends import EKSSessionManager

        if isinstance(self._manager, EKSSessionManager):
            raise NotImplementedError(
                "recreate() does not apply to EMR on EKS: an expired managed "
                "endpoint is replaced in place — call reconnect() instead."
            )

        old_session_id = self.session_id

        # Drop the local channel first; its session is spent and the host may
        # not answer. Deliberately not terminate_session() — the remote session
        # is already gone, and calling it on a live one would be destructive.
        # remote_gone is decided by asking the service, not assumed: recreate()
        # on a still-live session should release it cleanly rather than leak it.
        _discard_channel(self._spark, remote_gone=not self._manager.is_session_active())
        self._spark = None

        # Re-provision through the same manager, which still holds every
        # create() argument; provision() overwrites _session_id with the new one.
        self._manager.provision()
        try:
            self._manager.wait_for_ready()
            self._spark = _connect(self._manager)
        except BaseException:
            # The replacement was started but never became usable. It is billing
            # and idle-timing out on its own, so say which ID to chase — silently
            # dropping the reference is how clusters fill up with orphans.
            logger.error(
                f"Recreate failed after starting session {self.session_id}; it is "
                "still running remotely. Call stop() to terminate it, or reuse it "
                "once the cluster has capacity."
            )
            raise

        logger.info(f"Recreated session: {old_session_id} -> {self.session_id}")
        return self

    def stop(self, terminate: bool = True) -> None:
        """Stop the SparkSession and optionally release the remote resource.

        Args:
            terminate: If True (default), releases the remote resource —
                ``TerminateSession`` on Serverless/EC2, or deleting the managed
                endpoint on EMR on EKS (only if this client created it; an
                endpoint passed in as ``managed_endpoint_id`` is left alone).
                Set to False to leave it running for reuse.
        """
        if self._spark is not None:
            # A session that already idled out cannot answer the release RPC;
            # without this, stop() blocks on retries for over ten minutes.
            _discard_channel(
                self._spark, remote_gone=not self._manager.is_session_active()
            )
            self._spark = None

        if terminate:
            self._manager.terminate_session()

    def __enter__(self) -> "EMRSparkSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def __getattr__(self, name):
        """Delegate attribute access to the underlying SparkSession.

        This allows using EMRSparkSession exactly like a regular SparkSession:
            session.sql("...")
            session.read.parquet("...")
            session.createDataFrame(...)
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._spark, name)

    def __repr__(self) -> str:
        backend = type(self._manager).__name__.replace("SessionManager", "")
        return (
            f"EMRSparkSession(backend={backend}, "
            f"resource_id={self.resource_id}, "
            f"session_id={self.session_id})"
        )
