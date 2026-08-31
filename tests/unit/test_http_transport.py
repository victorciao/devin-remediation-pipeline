"""§14.1 transport: the exact URL the GitHub transport builds for a caller-supplied path.

The marker lookup is the pipeline's only dedupe primitive, and it is the one read whose path
carries a query string. Re-encoding that query turned `+` into `%2B`, so GitHub's search API
answered HTTP 422 on every marker lookup of a real LIVE run and dedupe silently degraded. The
URL is therefore pinned character for character here.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from email.message import Message
from io import BytesIO
from types import TracebackType
from urllib.error import HTTPError
from urllib.parse import urlencode

import pytest

from pipeline import http_transport
from pipeline.http_transport import (
    HttpTransportError,
    MissingCredentialError,
    UrllibGitHubTransport,
)

CREDENTIAL = "GITHUB_PAT_REMEDIATION"
MARKER = "<!-- devin-remediation-id: codeql-0 -->"


class FakeResponse:
    """The subset of `http.client.HTTPResponse` the transport consumes."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers: Mapping[str, str] = {"X-OAuth-Scopes": "repo"}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        """Return the canned JSON body."""
        return self._body


@pytest.fixture()
def requested_urls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Capture the URL of every request without opening a socket."""
    urls: list[str] = []

    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        urls.append(str(getattr(request, "full_url", "")))
        return FakeResponse(b'{"total_count": 0}')

    monkeypatch.setenv(CREDENTIAL, "placeholder-github-token")
    monkeypatch.setattr(http_transport, "urlopen", fake_urlopen)
    yield urls


def test_an_urlencoded_query_reaches_github_verbatim(requested_urls: list[str]) -> None:
    """§14.1 — a `+`-encoded search query is appended as given, never re-encoded.

    `urlencode` writes the space in the search qualifier as `+`; percent-encoding that `+`
    into `%2B` makes GitHub read a literal plus sign inside the query and reject the search.
    """
    query = urlencode({"q": f'repo:victorciao/superset is:issue in:body "{MARKER}"'})
    transport = UrllibGitHubTransport()

    transport.get(f"/search/issues?{query}")

    assert requested_urls == [f"https://api.github.com/search/issues?{query}"]
    assert "+" in requested_urls[0]
    assert "%2B" not in requested_urls[0]


def test_a_path_without_a_query_keeps_its_slashes(requested_urls: list[str]) -> None:
    """§14.1 — a plain resource path is joined to the base URL unchanged."""
    transport = UrllibGitHubTransport()

    transport.get("/repos/victorciao/superset/pulls/2")

    assert requested_urls == ["https://api.github.com/repos/victorciao/superset/pulls/2"]


def test_a_path_segment_needing_escaping_is_still_escaped(requested_urls: list[str]) -> None:
    """§14.1 — only the query is verbatim: the path segment is still `quote`d."""
    transport = UrllibGitHubTransport()

    transport.get("/repos/victorciao/superset/git/ref/heads/devin/fix a thing")

    assert requested_urls == [
        "https://api.github.com/repos/victorciao/superset/git/ref/heads/devin/fix%20a%20thing"
    ]


def test_an_already_encoded_path_segment_is_not_double_encoded(requested_urls: list[str]) -> None:
    """§14.1 — `%` is in the path's safe set, so `%3A` survives as itself."""
    transport = UrllibGitHubTransport()

    transport.get("/repos/victorciao/superset/labels/needs%3Ahuman")

    assert requested_urls == [
        "https://api.github.com/repos/victorciao/superset/labels/needs%3Ahuman"
    ]


def test_the_credential_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """§16 — the token is never captured at construction, so absence fails loudly."""
    monkeypatch.delenv(CREDENTIAL, raising=False)
    transport = UrllibGitHubTransport()

    with pytest.raises(MissingCredentialError):
        transport.get("/repos/victorciao/superset")


def http_error(
    *,
    body: bytes,
    code: int = 403,
    headers: Mapping[str, str] | None = None,
) -> HTTPError:
    """Build one HTTP error with a readable response body."""
    response_headers = Message()
    for key, value in (headers or {}).items():
        response_headers[key] = value
    return HTTPError(
        "https://api.github.com/search/issues",
        code,
        "request failed",
        response_headers,
        BytesIO(body),
    )


def test_a_secondary_rate_limit_with_retry_after_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secondary-limit 403 responses retry even with remaining quota."""
    responses: list[object] = [
        http_error(
            body=b'{"message":"secondary rate limit"}',
            headers={"x-ratelimit-remaining": "12", "retry-after": "2"},
        ),
        FakeResponse(b'{"ok": true}'),
    ]
    sleeps: list[float] = []

    monkeypatch.setenv(CREDENTIAL, "placeholder-github-token")

    def urlopen(*_args: object, **_kwargs: object) -> object:
        response = responses.pop(0)
        if isinstance(response, HTTPError):
            raise response
        return response

    monkeypatch.setattr(http_transport, "urlopen", urlopen)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert UrllibGitHubTransport().get("/user") == {"ok": True}
    assert sleeps == [2.0]


def test_a_rate_limit_message_without_headers_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JSON rate-limit message is sufficient to retry a 403."""
    responses: list[object] = [
        http_error(body=b'{"message":"API rate limit exceeded"}'),
        FakeResponse(b'{"ok": true}'),
    ]
    sleeps: list[float] = []

    monkeypatch.setenv(CREDENTIAL, "placeholder-github-token")

    def urlopen(*_args: object, **_kwargs: object) -> object:
        response = responses.pop(0)
        if isinstance(response, HTTPError):
            raise response
        return response

    monkeypatch.setattr(http_transport, "urlopen", urlopen)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert UrllibGitHubTransport().get("/user") == {"ok": True}
    assert sleeps == [1.0]


def test_an_unrelated_forbidden_response_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission failure remains terminal and preserves its status."""
    calls = 0

    def forbidden(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise http_error(body=b'{"message":"permission denied"}')

    monkeypatch.setenv(CREDENTIAL, "placeholder-github-token")
    monkeypatch.setattr(http_transport, "urlopen", forbidden)

    with pytest.raises(HttpTransportError, match="permission denied") as raised:
        UrllibGitHubTransport().get("/user")
    assert raised.value.status_code == 403
    assert calls == 1


def test_a_non_json_http_error_body_does_not_raise_a_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed error body still becomes a sanitized transport error."""
    monkeypatch.setenv(CREDENTIAL, "placeholder-github-token")

    def malformed(*_args: object, **_kwargs: object) -> object:
        raise http_error(
            body=b"<html>rate limit</html>",
            headers={"x-ratelimit-remaining": "1"},
        )

    monkeypatch.setattr(http_transport, "urlopen", malformed)

    with pytest.raises(HttpTransportError) as raised:
        UrllibGitHubTransport().get("/user")
    assert raised.value.status_code == 403
