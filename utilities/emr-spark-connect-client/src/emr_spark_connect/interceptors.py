# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""gRPC interceptor for automatic EMR auth token refresh.

Intercepts every outgoing gRPC call, checks token expiry, refreshes via
the backend-specific token refresh callable, and injects the fresh token
as x-aws-proxy-auth metadata.
"""

import datetime
import logging
import threading
import time
from collections import namedtuple
from typing import Callable, Optional, Tuple

import grpc

# The URL-parsing builder. In PySpark 4.x ChannelBuilder became an abstract base
# whose __init__ takes channelOptions, and the sc:// parsing moved to
# DefaultChannelBuilder (exported from .core, not the client package). On 3.x
# ChannelBuilder itself is the concrete URL-parsing class.
try:
    from pyspark.sql.connect.client.core import DefaultChannelBuilder as _ChannelBuilder
except ImportError:  # pyspark < 4
    from pyspark.sql.connect.client import ChannelBuilder as _ChannelBuilder

logger = logging.getLogger("emr_spark_connect.interceptors")

# namedtuple to create new ClientCallDetails with updated metadata
_ClientCallDetails = namedtuple(
    "_ClientCallDetails",
    ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"),
)


class _ClientCallDetails(_ClientCallDetails, grpc.ClientCallDetails):
    pass


class TokenRefreshInterceptor(
    grpc.UnaryUnaryClientInterceptor,
    grpc.UnaryStreamClientInterceptor,
    grpc.StreamUnaryClientInterceptor,
    grpc.StreamStreamClientInterceptor,
):
    """Intercepts gRPC calls to inject a fresh EMR auth token.

    Works with any EMR backend (Serverless, on EKS, on EC2) — the caller
    provides a `refresh_fn` that returns (token, expires_at).
    """

    # Refresh this many seconds before actual expiry — shrunk adaptively for
    # tokens whose whole lifetime is shorter than this (see _refresh_token).
    MAX_EARLY_REFRESH_SECONDS = 5 * 60
    # Never shrink the early-refresh margin below this: the token must survive
    # proxy-side validation after transit, so a few seconds of slack is required.
    MIN_EARLY_REFRESH_SECONDS = 5.0
    # Revalidate through refresh_fn at least this often, even while the token
    # is still valid. The refresh path is the only place a dead EMR on EKS
    # endpoint is detected (the credentials API rejects it and the manager
    # raises EndpointExpiredError); with a long-lived token and no cap, the
    # first call after an endpoint idles out instead hits a dead host and
    # grinds through gRPC's UNAVAILABLE retry policy — 15 attempts with
    # backoff capped at 60s, i.e. ~11 minutes — before failing.
    REVALIDATE_INTERVAL_SECONDS = 60.0

    def __init__(self, refresh_fn: Callable[[], Tuple[str, datetime.datetime]]):
        """
        Args:
            refresh_fn: Callable that returns (auth_token, expiry_datetime).
                        The expiry_datetime must be timezone-aware (UTC).
        """
        self._refresh_fn = refresh_fn
        self._cached_token: Optional[str] = None
        self._cache_expiration_time = datetime.datetime.min.replace(
            tzinfo=datetime.timezone.utc
        )
        # Monotonic timestamp of the last successful refresh, for the
        # revalidation cap. -inf so the very first call always refreshes.
        self._last_refresh_monotonic = float("-inf")
        # PySpark's reattachable execute releases results from a background
        # thread pool, so several RPCs — hence several interceptions — can be
        # in flight at once. The lock keeps them from each minting a token
        # (and, worse, racing the manager into replacing endpoints twice).
        self._lock = threading.Lock()

    def _refresh_token(self) -> str:
        """Mint a fresh token and return it. Caller must hold the lock."""
        token, expires_at = self._refresh_fn()
        self._cached_token = token
        if isinstance(expires_at, str):
            expires_at = datetime.datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        # A fixed 5-minute margin would put any token shorter than 300s
        # permanently inside the refresh window, degrading to a mint per RPC
        # (two AWS calls each). Scale the margin to the observed lifetime so
        # short-lived tokens still get reused for most of their life.
        now = datetime.datetime.now(datetime.timezone.utc)
        lifetime = (expires_at - now).total_seconds()
        early = min(self.MAX_EARLY_REFRESH_SECONDS,
                    max(self.MIN_EARLY_REFRESH_SECONDS, lifetime / 3))
        self._cache_expiration_time = expires_at - datetime.timedelta(seconds=early)
        self._last_refresh_monotonic = time.monotonic()
        logger.debug(f"Token refreshed. Next refresh at {self._cache_expiration_time}")
        return token

    def _cache_is_fresh(self) -> bool:
        if time.monotonic() - self._last_refresh_monotonic > self.REVALIDATE_INTERVAL_SECONDS:
            return False
        return self._cache_expiration_time >= datetime.datetime.now(datetime.timezone.utc)

    def _current_token(self) -> Optional[str]:
        if self._cache_is_fresh():
            return self._cached_token
        with self._lock:
            # Re-check: another thread may have refreshed while we waited.
            if self._cache_is_fresh():
                return self._cached_token
            return self._refresh_token()

    def _with_metadata(self, client_call_details):
        metadata = dict(client_call_details.metadata or {})
        metadata["x-aws-proxy-auth"] = self._current_token()
        return _ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=list(metadata.items()),
            credentials=client_call_details.credentials,
            wait_for_ready=client_call_details.wait_for_ready,
            compression=client_call_details.compression,
        )

    def intercept_unary_unary(self, continuation, client_call_details, request):
        return continuation(self._with_metadata(client_call_details), request)

    def intercept_unary_stream(self, continuation, client_call_details, request):
        return continuation(self._with_metadata(client_call_details), request)

    def intercept_stream_unary(
        self, continuation, client_call_details, request_iterator
    ):
        return continuation(
            self._with_metadata(client_call_details), request_iterator
        )

    def intercept_stream_stream(
        self, continuation, client_call_details, request_iterator
    ):
        return continuation(
            self._with_metadata(client_call_details), request_iterator
        )


class EMRChannelBuilder(_ChannelBuilder):
    """Extends PySpark's URL-parsing ChannelBuilder to add the token-refresh interceptor."""

    def __init__(self, url: str, refresh_fn: Callable[[], Tuple[str, datetime.datetime]]):
        """
        Args:
            url: Spark Connect URL (sc://host:443/;use_ssl=true;...)
            refresh_fn: Callable that returns (auth_token, expiry_datetime)
        """
        super().__init__(url)
        self._refresh_fn = refresh_fn

    def toChannel(self) -> grpc.Channel:
        channel = super().toChannel()
        interceptor = TokenRefreshInterceptor(self._refresh_fn)
        return grpc.intercept_channel(channel, interceptor)
