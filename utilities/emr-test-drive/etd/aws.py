"""AWS session helpers with credential auto-refresh.

A test drive can easily run longer than a credential lifetime: several EMR
Serverless jobs, each a few minutes, plus polling. Sessions backed by static
keys (for example those written by an internal credential helper) go stale
mid-run and every subsequent API call fails with ExpiredTokenException, losing
the whole run.

`AutoRefreshClient` proxies a boto3 client, and on an expiry error runs an
optional refresh command, rebuilds the underlying client from a fresh session,
and retries the call once.

Configure the refresh command in the config as `run.credential_refresh_command`,
or via the ETD_CREDENTIAL_REFRESH_CMD environment variable. If neither is set
the client simply surfaces the error — which is the right behaviour for SSO or
`credential_process` profiles, where boto3 already refreshes on its own.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from typing import Any, Callable

EXPIRY_CODES = {
    "ExpiredToken", "ExpiredTokenException", "RequestExpired",
    "InvalidClientTokenId", "TokenRefreshRequired", "UnrecognizedClientException",
}
# Attributes that must reach the real client untouched rather than being
# treated as callable API operations.
PASSTHROUGH = {"exceptions", "meta", "waiter_names", "can_paginate"}


def _is_expiry(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    if code in EXPIRY_CODES:
        return True
    text = str(exc)
    return "ExpiredToken" in text or "security token included in the request is expired" in text


class CredentialRefresher:
    """Runs the configured refresh command, at most once per interval."""

    def __init__(self, command: str | None = None, min_interval_s: float = 20.0) -> None:
        self.command = command or os.environ.get("ETD_CREDENTIAL_REFRESH_CMD") or ""
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def refresh(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if time.time() - self._last < self.min_interval_s:
                return True          # another thread just refreshed
            print(f"  [creds] refreshing: {self.command}")
            proc = subprocess.run(shlex.split(self.command), capture_output=True, text=True)
            self._last = time.time()
            if proc.returncode != 0:
                print(f"  [creds] refresh FAILED rc={proc.returncode}: "
                      f"{(proc.stderr or proc.stdout).strip()[:300]}")
                return False
            print("  [creds] refreshed")
            return True


class AutoRefreshClient:
    """Attribute-proxy around a boto3 client that retries once after refreshing."""

    def __init__(self, make_client: Callable[[], Any], refresher: CredentialRefresher) -> None:
        self._make = make_client
        self._refresher = refresher
        self._client = make_client()
        self._lock = threading.Lock()

    def _rebuild(self) -> None:
        with self._lock:
            self._client = self._make()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._client, name)
        if name in PASSTHROUGH or not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return getattr(self._client, name)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if not _is_expiry(exc):
                    raise
                print(f"  [creds] {name} hit expired credentials — refreshing and retrying")
                self._refresher.refresh()
                self._rebuild()
                return getattr(self._client, name)(*args, **kwargs)

        return call


def make_session(profile: str | None = None):
    import boto3
    return boto3.Session(profile_name=profile) if profile else boto3.Session()


class SessionFactory:
    """Builds auto-refreshing clients from a profile plus a refresh command."""

    def __init__(self, profile: str | None, region: str, refresh_command: str | None = None) -> None:
        self.profile = profile
        self.region = region
        self.refresher = CredentialRefresher(refresh_command)

    def client(self, service: str, region_name: str | None = None) -> AutoRefreshClient:
        region = region_name or self.region

        def make():
            return make_session(self.profile).client(service, region_name=region)

        return AutoRefreshClient(make, self.refresher)
