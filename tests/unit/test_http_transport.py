"""§14.1 transport: the exact URL the GitHub transport builds for a caller-supplied path.

The marker lookup is the pipeline's only dedupe primitive, and it is the one read whose path
carries a query string. Re-encoding that query turned `+` into `%2B`, so GitHub's search API
answered HTTP 422 on every marker lookup of a real LIVE run and dedupe silently degraded. The
URL is therefore pinned character for character here.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import TracebackType
from urllib.parse import urlencode

import pytest

from pipeline import http_transport
from pipeline.http_transport import MissingCredentialError, UrllibGitHubTransport

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
    query = urlencode({"q": f'repo:victorciao/superset "{MARKER}"'})
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
