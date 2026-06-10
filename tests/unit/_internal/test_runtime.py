"""Tests for Emscripten/Pyodide runtime detection (``_internal.runtime``).

The detection gate is the single seam every Pyodide-compatibility change
keys off, so it is exercised directly here and monkeypatched (rather than
re-implemented inline) in the feature tests that depend on it.
"""

from __future__ import annotations

import sys

import pytest

from mixpanel_headless._internal.runtime import is_emscripten


class TestIsEmscripten:
    """``is_emscripten`` mirrors ``sys.platform == "emscripten"``."""

    def test_true_under_emscripten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns ``True`` when ``sys.platform`` is the Pyodide WASM value.

        Args:
            monkeypatch: pytest fixture used to fake the platform string.
        """
        monkeypatch.setattr(sys, "platform", "emscripten")
        assert is_emscripten() is True

    def test_false_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns ``False`` on a native Linux interpreter.

        Args:
            monkeypatch: pytest fixture used to fake the platform string.
        """
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_emscripten() is False

    def test_false_on_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns ``False`` on macOS.

        Args:
            monkeypatch: pytest fixture used to fake the platform string.
        """
        monkeypatch.setattr(sys, "platform", "darwin")
        assert is_emscripten() is False
