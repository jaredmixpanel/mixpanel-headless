"""Synchronous-XHR httpx transport for the Pyodide (Emscripten) runtime.

Under Pyodide there is no socket layer, so httpx's default transport cannot
reach the network. The browser/worker does have a synchronous
``XMLHttpRequest``, which blocks until the response is fully available —
exactly the shape a synchronous ``httpx.Client.request()`` needs. This module
adapts that XHR into an :class:`httpx.BaseTransport`.

It is a faithful port of the live-verified shim shipped in the mixpanel-lab
sandbox worker (``renderer/sandbox/pyBootstrap.ts``), which has been proven
against the real Mixpanel API from a sandboxed ``app://``/``null`` origin
(carries ``Authorization`` + JSON body, echoes the origin). The differences
from the shim are deliberate and additive:

* the JS boundary is **injected** (``xhr_factory``) so the request/response
  logic is unit-testable under CPython without Pyodide; and
* network failures are mapped into the :class:`httpx.TransportError` family
  rather than leaking raw ``JsException``\\s to callers.

The class is named ``PyfetchTransport`` for the Pyodide-fetch role it fills
in the client; the underlying mechanism is synchronous ``XMLHttpRequest``
(true ``pyfetch`` is async and cannot satisfy a sync httpx call).

Example:
    ```python
    import httpx
    from mixpanel_headless._internal.pyodide_transport import PyfetchTransport

    client = httpx.Client(transport=PyfetchTransport())  # under Pyodide
    response = client.get("https://mixpanel.com/api/query")
    ```
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import httpx

#: Request headers the browser/XHR manages itself; setting them is forbidden
#: (``setRequestHeader`` throws) or meaningless. Matches the proven shim.
_SKIP_REQUEST_HEADERS = frozenset(
    {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
)

#: Response headers to drop: the browser has already decoded the body, so a
#: passed-through ``content-encoding``/``content-length`` would make httpx try
#: to decode/validate it again against the already-decoded bytes.
_SKIP_RESPONSE_HEADERS = frozenset({"content-encoding", "content-length"})


class _XHRLike(Protocol):
    """Structural type for the synchronous ``XMLHttpRequest`` surface used here.

    Both the real Pyodide ``js.XMLHttpRequest`` proxy (via :class:`_PyodideXHR`)
    and the in-memory test fake satisfy this Protocol, which is the seam that
    lets :class:`PyfetchTransport` run under CPython. The camelCase names match
    the JS API verbatim.
    """

    @property
    def status(self) -> int:
        """HTTP status code of the completed request (0 on network/CORS failure)."""

    @property
    def responseText(self) -> str | None:
        """Response body as text, or ``None`` when there is no body."""

    def open(self, method: str, url: str, async_flag: bool) -> None:
        """Open a request; ``async_flag=False`` selects synchronous mode."""

    def setRequestHeader(self, name: str, value: str) -> None:
        """Set one outgoing request header."""

    def send(self, body: object) -> None:
        """Send the request, blocking until the response is available."""

    def getAllResponseHeaders(self) -> str | None:
        """Return the raw CRLF-joined response-header block (or ``None``)."""


class _PyodideXHR:
    """Adapter wrapping a real ``js.XMLHttpRequest`` proxy as an :class:`_XHRLike`.

    The only behavior beyond plain delegation is converting an outgoing
    ``bytes`` body to a JS value via ``to_js`` before handing it to
    ``send`` — keeping that (Pyodide-only) conversion out of
    :meth:`PyfetchTransport.handle_request` so the latter stays testable.
    """

    def __init__(self, raw: _XHRLike, to_js: Callable[[object], object]) -> None:
        """Wrap a raw JS XHR proxy and the FFI ``to_js`` converter.

        Args:
            raw: The ``js.XMLHttpRequest.new()`` proxy to delegate to. Typed as
                :class:`_XHRLike` because the JS proxy structurally provides
                that surface; the factory passes the (untyped) proxy in.
            to_js: Pyodide's ``pyodide.ffi.to_js``, used to marshal a
                ``bytes`` body into a JS value ``send`` accepts.
        """
        self._raw = raw
        self._to_js = to_js

    @property
    def status(self) -> int:
        """HTTP status reported by the wrapped XHR."""
        return int(self._raw.status)

    @property
    def responseText(self) -> str | None:
        """Response body text from the wrapped XHR (or ``None``)."""
        return self._raw.responseText

    def open(self, method: str, url: str, async_flag: bool) -> None:
        """Delegate ``open(method, url, async_flag)`` to the wrapped XHR.

        Args:
            method: HTTP method.
            url: Absolute request URL.
            async_flag: ``False`` for the synchronous mode this transport needs.
        """
        self._raw.open(method, url, async_flag)

    def setRequestHeader(self, name: str, value: str) -> None:
        """Delegate a single ``setRequestHeader`` call to the wrapped XHR.

        Args:
            name: Header name.
            value: Header value.
        """
        self._raw.setRequestHeader(name, value)

    def send(self, body: object) -> None:
        """Send the request, converting a ``bytes`` body via ``to_js`` first.

        Args:
            body: Raw request body as ``bytes``, or ``None`` for no body.
        """
        if body is None:
            self._raw.send(None)
        else:
            self._raw.send(self._to_js(body))

    def getAllResponseHeaders(self) -> str | None:
        """Delegate ``getAllResponseHeaders`` to the wrapped XHR."""
        return self._raw.getAllResponseHeaders()


def _default_xhr_factory() -> _XHRLike:  # pragma: no cover - Emscripten only
    """Build a real Pyodide-backed XHR (only importable under Emscripten).

    Imports ``js`` and ``pyodide.ffi`` lazily so this module imports cleanly
    on CPython; both modules exist only inside the Pyodide runtime. Exercised
    by ``just test-pyodide-node``, not the CPython unit suite.

    Returns:
        An :class:`_XHRLike` wrapping a fresh ``js.XMLHttpRequest``.
    """
    import js
    from pyodide.ffi import to_js

    return _PyodideXHR(js.XMLHttpRequest.new(), to_js)


class PyfetchTransport(httpx.BaseTransport):
    """httpx transport backed by a synchronous ``XMLHttpRequest`` (Pyodide).

    Installed automatically by
    :class:`~mixpanel_headless._internal.api_client.MixpanelAPIClient` when
    running under Emscripten and no explicit transport was supplied. Off
    Emscripten it is never constructed by the library; it is instantiable in
    tests by injecting ``xhr_factory``.

    Example:
        ```python
        transport = PyfetchTransport()           # real js-backed XHR
        client = httpx.Client(transport=transport)
        ```
    """

    def __init__(self, *, xhr_factory: Callable[[], _XHRLike] | None = None) -> None:
        """Initialize the transport.

        Args:
            xhr_factory: Zero-arg factory returning a fresh :class:`_XHRLike`
                per request. Defaults to :func:`_default_xhr_factory` (the real
                Pyodide-backed XHR). Tests inject an in-memory fake here.
        """
        self._xhr_factory: Callable[[], _XHRLike] = xhr_factory or _default_xhr_factory

    @staticmethod
    def _parse_response_headers(raw: str | None) -> list[tuple[str, str]]:
        """Split a raw ``getAllResponseHeaders`` block into header pairs.

        Args:
            raw: The CRLF-joined header block (or ``None``).

        Returns:
            ``(name, value)`` pairs with whitespace stripped, omitting headers
            in :data:`_SKIP_RESPONSE_HEADERS` (case-insensitively).
        """
        headers: list[tuple[str, str]] = []
        for line in (raw or "").splitlines():
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            if name.strip().lower() not in _SKIP_RESPONSE_HEADERS:
                headers.append((name.strip(), value.strip()))
        return headers

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Issue ``request`` over a synchronous XHR and return the response.

        Args:
            request: The fully-formed httpx request (httpx calls this).

        Returns:
            An :class:`httpx.Response` synthesized from the XHR result.

        Raises:
            httpx.ConnectError: The underlying ``send`` raised (network/CORS
                failure) or the XHR reported status ``0`` (a silent failure).
        """
        xhr = self._xhr_factory()
        try:
            xhr.open(request.method, str(request.url), False)
            for name, value in request.headers.items():
                if name.lower() not in _SKIP_REQUEST_HEADERS:
                    xhr.setRequestHeader(name, value)
            body = request.content
            xhr.send(body if body else None)
        except Exception as exc:
            # Any failure crossing the JS boundary (DOMException / JsException
            # for network or CORS errors) is a transport-layer failure, not an
            # application response. Scoped to the network ops only — response
            # parsing below must not be misclassified as a connect error.
            raise httpx.ConnectError(str(exc), request=request) from exc

        status = int(xhr.status)
        if status == 0:
            # A synchronous XHR that fails to reach the server (network down,
            # blocked by CORS) returns status 0 without raising. httpx callers
            # expect a transport error there, not a bogus 0-status response.
            raise httpx.ConnectError(
                "XMLHttpRequest reported status 0 (network or CORS failure).",
                request=request,
            )
        text = xhr.responseText
        content = text.encode("utf-8") if text is not None else b""
        return httpx.Response(
            status_code=status,
            headers=self._parse_response_headers(xhr.getAllResponseHeaders()),
            content=content,
            request=request,
        )
