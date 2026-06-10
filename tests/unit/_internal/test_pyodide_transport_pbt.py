"""Property-based tests for PyfetchTransport header handling.

Invariant: across arbitrary request-header sets, the transport forwards every
header the browser does *not* manage and drops every header it does (the
``_SKIP_REQUEST_HEADERS`` set, case-insensitively). httpx always synthesizes a
``Host`` header from the URL, so the skip path is exercised on every example.
"""

from __future__ import annotations

import string

import httpx
from hypothesis import given
from hypothesis import strategies as st

from mixpanel_headless._internal.pyodide_transport import (
    _SKIP_REQUEST_HEADERS,
    PyfetchTransport,
)


class _RecordingXHR:
    """Minimal XHR fake that records forwarded request headers."""

    def __init__(self) -> None:
        """Initialize the canned response and header-capture slot."""
        self.status = 200
        self.responseText = ""
        self.request_headers: list[tuple[str, str]] = []

    def open(self, method: str, url: str, async_flag: bool) -> None:
        """No-op open (the property only inspects headers)."""

    def setRequestHeader(self, name: str, value: str) -> None:  # noqa: N802
        """Record a forwarded header."""
        self.request_headers.append((name, value))

    def send(self, body: object) -> None:
        """No-op send."""

    def getAllResponseHeaders(self) -> str | None:  # noqa: N802
        """Return no response headers."""
        return ""


# Lowercase-only names keep dict keys unambiguous under httpx's case-insensitive
# header model; alphanumeric values avoid httpx's surrounding-whitespace trim.
_HEADER_NAMES = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-", min_size=1, max_size=20
).filter(lambda name: name.lower() not in _SKIP_REQUEST_HEADERS)
_HEADER_VALUES = st.text(
    alphabet=string.ascii_letters + string.digits, min_size=1, max_size=30
)


class TestHeaderForwardingProperties:
    """Forwarding/skip invariants hold for arbitrary header sets."""

    @given(headers=st.dictionaries(_HEADER_NAMES, _HEADER_VALUES, max_size=8))
    def test_non_skiplist_headers_forwarded_and_host_dropped(
        self, headers: dict[str, str]
    ) -> None:
        """Every non-skip header is forwarded verbatim; auto-added Host is dropped.

        Args:
            headers: Arbitrary request headers (none in the skip-list).
        """
        xhr = _RecordingXHR()
        request = httpx.Request(
            "GET", "https://mixpanel.com/api/query", headers=headers
        )
        PyfetchTransport(xhr_factory=lambda: xhr).handle_request(request)

        forwarded = {name.lower(): value for name, value in xhr.request_headers}
        for name, value in headers.items():
            assert forwarded.get(name.lower()) == value
        # httpx always sets Host from the URL; the transport must drop it.
        assert "host" not in forwarded
