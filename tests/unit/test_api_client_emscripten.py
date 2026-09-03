"""Tests for the Emscripten branches of the API client.

Under Pyodide, ``MixpanelAPIClient`` must transparently route through
:class:`PyfetchTransport` so a plain ``Workspace()`` works with no caller
changes. Off Emscripten the behavior is byte-identical to before (transport
stays ``None`` ⇒ httpx default). Conversely, shortlink resolution depends on
reading a suppressed redirect, which the browser fetch/XHR stack cannot
expose, so it must refuse up front instead of misreporting. The gate is
monkeypatched rather than faking ``sys.platform`` so each assertion targets
exactly the branch under test.
"""

from __future__ import annotations

import httpx
import pytest

from mixpanel_headless._internal.api_client import MixpanelAPIClient
from mixpanel_headless._internal.pyodide_transport import PyfetchTransport
from mixpanel_headless.exceptions import ShortLinkResolutionError
from tests.conftest import make_session


class TestEmscriptenTransportAutoRegistration:
    """``__init__`` self-registers :class:`PyfetchTransport` only under Emscripten."""

    def test_self_registers_under_emscripten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no explicit transport under Emscripten, a PyfetchTransport is wired.

        Args:
            monkeypatch: pytest fixture used to force the Emscripten gate on.
        """
        monkeypatch.setattr(
            "mixpanel_headless._internal.api_client.is_emscripten", lambda: True
        )
        client = MixpanelAPIClient(session=make_session())
        assert isinstance(client._transport, PyfetchTransport)

    def test_native_leaves_transport_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off Emscripten the transport stays ``None`` (native path unchanged).

        Args:
            monkeypatch: pytest fixture used to force the Emscripten gate off.
        """
        monkeypatch.setattr(
            "mixpanel_headless._internal.api_client.is_emscripten", lambda: False
        )
        client = MixpanelAPIClient(session=make_session())
        assert client._transport is None

    def test_explicit_transport_is_preserved_under_emscripten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An injected transport is never overwritten, even under Emscripten.

        Args:
            monkeypatch: pytest fixture used to force the Emscripten gate on.
        """
        monkeypatch.setattr(
            "mixpanel_headless._internal.api_client.is_emscripten", lambda: True
        )
        mock = httpx.MockTransport(lambda _req: httpx.Response(200, json=[]))
        client = MixpanelAPIClient(session=make_session(), _transport=mock)
        assert client._transport is mock

    def test_ensure_client_mounts_pyfetch_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The built ``httpx.Client`` is mounted with the auto-wired transport.

        Args:
            monkeypatch: pytest fixture used to force the Emscripten gate on.
        """
        monkeypatch.setattr(
            "mixpanel_headless._internal.api_client.is_emscripten", lambda: True
        )
        client = MixpanelAPIClient(session=make_session())
        http_client = client._ensure_client()
        try:
            assert http_client._transport is client._transport
            assert isinstance(client._transport, PyfetchTransport)
        finally:
            client.close()


class TestShortLinkUnavailableUnderEmscripten:
    """``resolve_short_link`` refuses under Emscripten instead of guessing."""

    def test_raises_without_issuing_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under Emscripten the shortlink call fails fast with an actionable hint.

        ``resolve_short_link`` reads the target out of a *suppressed* redirect
        (``follow_redirects=False`` + the ``Location`` header). The browser
        fetch/XHR stack behind :class:`PyfetchTransport` follows redirects
        transparently and hides ``Location``, so the request would land on the
        final HTML — or a login page — and be misreported as
        ``SHORT_LINK_UNEXPECTED_RESPONSE`` or a silent auth miss. The gate must
        therefore fire before any HTTP is attempted.

        Args:
            monkeypatch: pytest fixture used to force the Emscripten gate on.
        """
        monkeypatch.setattr(
            "mixpanel_headless._internal.api_client.is_emscripten", lambda: True
        )

        def _fail(*_args: object, **_kwargs: object) -> httpx.Response:
            """Fail the test if any shortlink HTTP request is attempted."""
            raise AssertionError("resolve_short_link must not issue a request")

        client = MixpanelAPIClient(session=make_session())
        monkeypatch.setattr(client, "_get_short_link", _fail)

        with pytest.raises(ShortLinkResolutionError) as exc_info:
            client.resolve_short_link("AbC123")

        exc = exc_info.value
        assert exc.code == "SHORT_LINK_UNSUPPORTED_RUNTIME"
        assert "full report URL" in str(exc)
        assert exc.details["short_code"] == "AbC123"

    def test_native_still_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Off Emscripten the gate is inert and a normal 302 still resolves.

        Args:
            monkeypatch: pytest fixture used to force the Emscripten gate off.
        """
        monkeypatch.setattr(
            "mixpanel_headless._internal.api_client.is_emscripten", lambda: False
        )
        target = "https://mixpanel.com/project/3/view/75/app/insights#EBrV5bW2u9Mw"
        client = MixpanelAPIClient(session=make_session())
        monkeypatch.setattr(
            client,
            "_get_short_link",
            lambda _url: httpx.Response(302, headers={"Location": target}),
        )

        assert client.resolve_short_link("AbC123") == target
