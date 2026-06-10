"""Tests for the Pyodide synchronous-XHR httpx transport.

``PyfetchTransport`` runs only under Emscripten, where it backs httpx with a
synchronous ``XMLHttpRequest`` reachable through Pyodide's ``js`` FFI. The
JS boundary is injected (``xhr_factory``) so the request/response/error
logic is fully exercised under CPython with an in-memory fake; the real
``js``-backed factory is proven separately by ``just test-pyodide-node``.

Ported from the live-verified sandbox shim in mixpanel-lab
(``renderer/sandbox/pyBootstrap.ts``).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from mixpanel_headless._internal.pyodide_transport import (
    PyfetchTransport,
    _PyodideXHR,
    _XHRLike,
)


class _FakeXHR:
    """In-memory stand-in for a synchronous ``js.XMLHttpRequest``.

    Mirrors the camelCase surface the transport drives, records the outgoing
    request for assertions, and replays a preloaded response. Set
    ``send_error`` to simulate a network/CORS failure on ``send``.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        response_text: str | None = "",
        raw_headers: str | None = "",
        send_error: Exception | None = None,
    ) -> None:
        """Preload the canned response and request-capture slots.

        Args:
            status: HTTP status to report back via ``status``.
            response_text: Body text returned via ``responseText``.
            raw_headers: CRLF-joined header block returned by
                ``getAllResponseHeaders``.
            send_error: When set, ``send`` raises it (network-failure sim).
        """
        self.status = status
        self.responseText = response_text
        self._raw_headers = raw_headers
        self._send_error = send_error
        self.opened: tuple[str, str, bool] | None = None
        self.request_headers: list[tuple[str, str]] = []
        self.sent_body: object = "<<unsent>>"

    def open(self, method: str, url: str, async_flag: bool) -> None:
        """Record the (method, url, async flag) triple from ``xhr.open``."""
        self.opened = (method, url, async_flag)

    def setRequestHeader(self, name: str, value: str) -> None:  # noqa: N802
        """Record a forwarded request header (JS camelCase surface)."""
        self.request_headers.append((name, value))

    def send(self, body: object) -> None:
        """Capture the sent body, or raise the preloaded network error."""
        if self._send_error is not None:
            raise self._send_error
        self.sent_body = body

    def getAllResponseHeaders(self) -> str | None:  # noqa: N802
        """Return the canned raw response-header block."""
        return self._raw_headers


def _factory(xhr: _FakeXHR) -> Callable[[], _XHRLike]:
    """Build a single-shot ``xhr_factory`` returning ``xhr``.

    Args:
        xhr: The fake to hand back on every call (structurally an ``_XHRLike``).

    Returns:
        A zero-arg callable suitable for ``PyfetchTransport(xhr_factory=...)``.
    """
    return lambda: xhr


class TestRequestForwarding:
    """The transport drives a synchronous XHR and forwards headers/body."""

    def test_open_is_synchronous(self) -> None:
        """``xhr.open`` is called with the async flag set to ``False``."""
        xhr = _FakeXHR()
        PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(
            httpx.Request("GET", "https://mixpanel.com/api/query")
        )
        assert xhr.opened == ("GET", "https://mixpanel.com/api/query", False)

    def test_forwards_user_headers(self) -> None:
        """Caller headers (e.g. Authorization) are forwarded verbatim."""
        xhr = _FakeXHR()
        req = httpx.Request(
            "GET",
            "https://mixpanel.com/api/query",
            headers={"Authorization": "Bearer tok", "X-Trace": "abc"},
        )
        PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(req)
        forwarded = {name.lower(): value for name, value in xhr.request_headers}
        assert forwarded["authorization"] == "Bearer tok"
        assert forwarded["x-trace"] == "abc"

    def test_skips_browser_managed_request_headers(self) -> None:
        """``host``/``content-length``/etc. are dropped — the browser owns them."""
        xhr = _FakeXHR()
        req = httpx.Request(
            "POST",
            "https://mixpanel.com/api/query",
            headers={
                "Host": "evil.example",
                "Content-Length": "5",
                "Connection": "keep-alive",
                "Accept-Encoding": "gzip",
                "Transfer-Encoding": "chunked",
                "Authorization": "Bearer tok",
            },
            content=b"hello",
        )
        PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(req)
        forwarded = {name.lower() for name, _ in xhr.request_headers}
        assert "authorization" in forwarded
        assert forwarded.isdisjoint(
            {
                "host",
                "content-length",
                "connection",
                "accept-encoding",
                "transfer-encoding",
            }
        )

    def test_sends_body_when_present(self) -> None:
        """A non-empty request body is passed to ``xhr.send``."""
        xhr = _FakeXHR()
        req = httpx.Request(
            "POST", "https://mixpanel.com/api/query", content=b"payload"
        )
        PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(req)
        assert xhr.sent_body == b"payload"

    def test_sends_none_when_body_empty(self) -> None:
        """An empty body sends ``None`` (equivalent to ``xhr.send()``)."""
        xhr = _FakeXHR()
        req = httpx.Request("GET", "https://mixpanel.com/api/query")
        PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(req)
        assert xhr.sent_body is None


class TestResponseConstruction:
    """The transport synthesizes a faithful ``httpx.Response``."""

    def test_status_and_body(self) -> None:
        """Status code and utf-8-decoded body cross back into httpx."""
        xhr = _FakeXHR(status=201, response_text='{"ok": true}')
        resp = PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(
            httpx.Request("GET", "https://mixpanel.com/api/query")
        )
        assert resp.status_code == 201
        assert resp.json() == {"ok": True}

    def test_response_headers_parsed(self) -> None:
        """Headers from ``getAllResponseHeaders`` are split into pairs."""
        xhr = _FakeXHR(
            response_text="x",
            raw_headers="Content-Type: application/json\r\nX-Trace: abc\r\n",
        )
        resp = PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(
            httpx.Request("GET", "https://mixpanel.com/api/query")
        )
        assert resp.headers["content-type"] == "application/json"
        assert resp.headers["x-trace"] == "abc"

    def test_skips_content_encoding_and_length_response_headers(self) -> None:
        """``content-encoding``/``content-length`` are stripped (browser pre-decoded)."""
        xhr = _FakeXHR(
            response_text="abc",
            raw_headers=(
                "Content-Type: text/plain\r\n"
                "Content-Encoding: gzip\r\n"
                "Content-Length: 999\r\n"
            ),
        )
        resp = PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(
            httpx.Request("GET", "https://mixpanel.com/api/query")
        )
        assert "content-encoding" not in resp.headers
        # httpx recomputes content-length from the actual bytes (3), never 999.
        assert resp.headers.get("content-length") != "999"
        assert resp.content == b"abc"

    def test_empty_response_text_is_empty_bytes(self) -> None:
        """A ``None`` ``responseText`` becomes empty content, not a crash."""
        xhr = _FakeXHR(status=204, response_text=None)
        resp = PyfetchTransport(xhr_factory=_factory(xhr)).handle_request(
            httpx.Request("GET", "https://mixpanel.com/api/query")
        )
        assert resp.status_code == 204
        assert resp.content == b""


class TestErrorMapping:
    """Network/transport failures surface as ``httpx.TransportError``."""

    def test_send_failure_becomes_transport_error(self) -> None:
        """A raising ``xhr.send`` (network/CORS) maps to ``httpx.ConnectError``."""
        xhr = _FakeXHR(send_error=RuntimeError("NetworkError: failed to fetch"))
        transport = PyfetchTransport(xhr_factory=_factory(xhr))
        with pytest.raises(httpx.TransportError) as exc_info:
            transport.handle_request(
                httpx.Request("GET", "https://mixpanel.com/api/query")
            )
        assert isinstance(exc_info.value, httpx.ConnectError)
        assert "NetworkError" in str(exc_info.value)

    def test_status_zero_becomes_transport_error(self) -> None:
        """Sync XHR status 0 (silent network/CORS failure) maps to a transport error."""
        xhr = _FakeXHR(status=0, response_text="")
        transport = PyfetchTransport(xhr_factory=_factory(xhr))
        with pytest.raises(httpx.TransportError):
            transport.handle_request(
                httpx.Request("GET", "https://mixpanel.com/api/query")
            )


class TestViaHttpxClient:
    """End-to-end: httpx itself drives the transport (the real call path)."""

    def test_get_round_trip(self) -> None:
        """``httpx.Client(transport=...)`` returns the synthesized response."""
        xhr = _FakeXHR(status=200, response_text='[{"id": 1}]')
        transport = PyfetchTransport(xhr_factory=_factory(xhr))
        with httpx.Client(transport=transport) as client:
            resp = client.get(
                "https://mixpanel.com/api/query",
                headers={"Authorization": "Bearer tok"},
            )
        assert resp.status_code == 200
        assert resp.json() == [{"id": 1}]
        # httpx synthesized a Host header; the transport must have dropped it.
        assert {n.lower() for n, _ in xhr.request_headers}.isdisjoint({"host"})

    def test_post_body_round_trip(self) -> None:
        """A JSON POST body reaches ``xhr.send`` and the response decodes."""
        xhr = _FakeXHR(status=200, response_text='{"created": true}')
        transport = PyfetchTransport(xhr_factory=_factory(xhr))
        with httpx.Client(transport=transport) as client:
            resp = client.post(
                "https://mixpanel.com/oauth/mcp/register/",
                json={"redirect_uris": ["http://localhost/cb"]},
            )
        assert resp.json() == {"created": True}
        assert isinstance(xhr.sent_body, bytes)
        assert b"redirect_uris" in xhr.sent_body


class TestPyodideXHRAdapter:
    """The real-path adapter delegates to the JS proxy and converts the body."""

    def test_send_converts_body_via_to_js(self) -> None:
        """``send`` routes a non-None body through the injected ``to_js``."""
        seen: list[object] = []

        def _to_js(body: object) -> object:
            """Record the body and return a JS-ish marker string."""
            seen.append(body)
            return f"js:{body!r}"

        raw = _FakeXHR()
        adapter = _PyodideXHR(raw, _to_js)
        adapter.send(b"abc")
        assert seen == [b"abc"]
        assert raw.sent_body == "js:b'abc'"

    def test_send_passes_none_through_untouched(self) -> None:
        """A ``None`` body is sent as-is, never handed to ``to_js``."""
        called: list[object] = []

        def _to_js(body: object) -> object:
            """Record any call (must not happen for a None body)."""
            called.append(body)
            return body

        raw = _FakeXHR()
        adapter = _PyodideXHR(raw, _to_js)
        adapter.send(None)
        assert called == []
        assert raw.sent_body is None

    def test_attributes_and_methods_delegate(self) -> None:
        """status/responseText/open/headers all proxy to the wrapped object."""
        raw = _FakeXHR(status=200, response_text="body", raw_headers="A: b\r\n")
        adapter = _PyodideXHR(raw, lambda b: b)
        adapter.open("GET", "https://x/y", False)
        adapter.setRequestHeader("Authorization", "Bearer t")
        assert raw.opened == ("GET", "https://x/y", False)
        assert raw.request_headers == [("Authorization", "Bearer t")]
        assert adapter.status == 200
        assert adapter.responseText == "body"
        assert adapter.getAllResponseHeaders() == "A: b\r\n"
