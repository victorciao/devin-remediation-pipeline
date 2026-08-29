# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Credential-safe stdlib HTTP transports for Devin and GitHub."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pipeline.github_client import GitHubTransport
from pipeline.session_client import DevinTransport


class HttpTransportError(RuntimeError):
    """Raised for a sanitized HTTP transport failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MissingCredentialError(HttpTransportError):
    """Raised when the runtime credential environment variable is absent."""


class _JsonHttpTransport:
    """Shared bounded HTTP implementation with runtime-only credentials."""

    def __init__(
        self,
        *,
        base_url: str,
        credential_name: str,
        auth_scheme: str,
        service_name: str,
        timeout_s: float = 30.0,
        max_attempts: int = 4,
        max_wait_s: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential_name = credential_name
        self._auth_scheme = auth_scheme
        self._service_name = service_name
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._max_wait_s = max_wait_s
        self._last_headers: dict[str, str] = {}

    @property
    def last_headers(self) -> Mapping[str, str]:
        """Return non-sensitive headers from the latest response."""
        return dict(self._last_headers)

    def _credential(self) -> str:
        token = os.environ.get(self._credential_name)
        if not token:
            raise MissingCredentialError(
                f"{self._service_name} credential {self._credential_name} is not set"
            )
        return token

    @staticmethod
    def _mapping(value: object, service_name: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise HttpTransportError(f"{service_name} response was not a JSON object")
        return value

    @staticmethod
    def _retry_delay(headers: Mapping[str, str], *, clock: float) -> float | None:
        normalized = {key.casefold(): value for key, value in headers.items()}
        retry_after = normalized.get("retry-after")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                return None
        reset_at = normalized.get("x-ratelimit-reset")
        if reset_at is not None:
            try:
                return max(float(reset_at) - clock, 0.0)
            except ValueError:
                return None
        return None

    def _request(self, method: str, path: str, payload: Mapping[str, object] | None) -> object:
        token = self._credential()
        body = json.dumps(dict(payload)).encode("utf-8") if payload is not None else None
        url = f"{self._base_url}/{quote(path.lstrip('/'), safe='/?:=&')}"
        waited = 0.0
        for attempt in range(self._max_attempts):
            request = Request(url, data=body, method=method)
            request.add_header("Accept", "application/json")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", f"{self._auth_scheme} {token}")
            request.add_header("User-Agent", "devin-remediation-pipeline")
            try:
                with urlopen(request, timeout=self._timeout_s) as response:
                    self._last_headers = {
                        str(key): str(value) for key, value in response.headers.items()
                    }
                    raw = response.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HttpTransportError(f"{self._service_name} returned invalid JSON") from exc
            except HTTPError as exc:
                headers = {str(key): str(value) for key, value in exc.headers.items()}
                normalized_headers = {key.casefold(): value for key, value in headers.items()}
                retryable = exc.code == 429 or (
                    exc.code == 403 and normalized_headers.get("x-ratelimit-remaining") == "0"
                )
                delay = self._retry_delay(headers, clock=time.time())
                if retryable and attempt + 1 < self._max_attempts:
                    if delay is None:
                        delay = 1.0
                    delay = min(delay, self._max_wait_s - waited)
                    if delay >= 0:
                        if delay > 0:
                            time.sleep(delay)
                        waited += delay
                        continue
                raise HttpTransportError(
                    f"{self._service_name} request failed with HTTP {exc.code}",
                    status_code=exc.code,
                ) from None
            except (TimeoutError, URLError, OSError):
                raise HttpTransportError(f"{self._service_name} request failed") from None
        raise HttpTransportError(f"{self._service_name} request retry limit exceeded")


class UrllibDevinTransport(DevinTransport):
    """Concrete Devin API transport using DEVIN_API_KEY at call time."""

    def __init__(self, *, base_url: str = "https://api.devin.ai") -> None:
        self._http = _JsonHttpTransport(
            base_url=base_url,
            credential_name="DEVIN_API_KEY",
            auth_scheme="Bearer",
            service_name="Devin",
        )

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Create a Devin session."""
        return self._http._mapping(self._http._request("POST", path, payload), "Devin")

    def get(self, path: str) -> Mapping[str, object]:
        """Retrieve a Devin session."""
        return self._http._mapping(self._http._request("GET", path, None), "Devin")


class UrllibGitHubTransport(GitHubTransport):
    """Concrete GitHub API transport using GITHUB_PAT_REMEDIATION at call time."""

    def __init__(self, *, base_url: str = "https://api.github.com") -> None:
        self._http = _JsonHttpTransport(
            base_url=base_url,
            credential_name="GITHUB_PAT_REMEDIATION",
            auth_scheme="Bearer",
            service_name="GitHub",
        )

    @property
    def response_headers(self) -> Mapping[str, str]:
        """Return non-sensitive headers from the latest response."""
        return self._http.last_headers

    def get(self, path: str) -> object:
        """Read a GitHub API resource, including list responses."""
        value = self._http._request("GET", path, None)
        return value

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Create a GitHub resource."""
        return self._http._mapping(self._http._request("POST", path, payload), "GitHub")

    def patch(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Patch a GitHub resource."""
        return self._http._mapping(self._http._request("PATCH", path, payload), "GitHub")


__all__ = [
    "HttpTransportError",
    "MissingCredentialError",
    "UrllibDevinTransport",
    "UrllibGitHubTransport",
]
