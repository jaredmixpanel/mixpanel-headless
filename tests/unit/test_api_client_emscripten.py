"""Tests for Emscripten auto-wiring of the Pyodide transport in the API client.

Under Pyodide, ``MixpanelAPIClient`` must transparently route through
:class:`PyfetchTransport` so a plain ``Workspace()`` works with no caller
changes. Off Emscripten the behavior is byte-identical to before (transport
stays ``None`` ⇒ httpx default). The gate is monkeypatched rather than faking
``sys.platform`` so the assertion targets exactly the client's branch.
"""

from __future__ import annotations

import httpx
import pytest

from mixpanel_headless._internal.api_client import MixpanelAPIClient
from mixpanel_headless._internal.pyodide_transport import PyfetchTransport
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
