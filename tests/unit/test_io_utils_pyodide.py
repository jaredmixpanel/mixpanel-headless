"""Tests that credential reads work under Emscripten, where ``fcntl`` is absent.

Pyodide removes the ``fcntl`` module (browser limitation), yet defines
``os.O_NONBLOCK`` — so the credential-read hygiene step that clears
``O_NONBLOCK`` via ``fcntl`` would crash there. The step is a no-op on the
regular files this reads, so it is gated off under Emscripten. (On Windows
``O_NONBLOCK`` is itself absent, so that platform already skips it.)
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

from mixpanel_headless._internal import io_utils
from mixpanel_headless._internal.io_utils import (
    atomic_write_bytes,
    read_credential_text,
)


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX credential fds only")
class TestCredentialReadWithoutFcntl:
    """``read_credential_bytes`` must not require ``fcntl`` under Emscripten."""

    def test_read_succeeds_under_emscripten_without_fcntl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the gate on and fcntl unavailable, the read still returns content.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: fixture used to fake Emscripten and remove ``fcntl``.
        """
        path = tmp_path / "tokens_us.json"
        atomic_write_bytes(path, b'{"k": "v"}')
        monkeypatch.setattr(io_utils, "is_emscripten", lambda: True)
        # A None entry makes `import fcntl` raise, mimicking Pyodide.
        monkeypatch.setitem(sys.modules, "fcntl", None)
        assert read_credential_text(path) == '{"k": "v"}'

    def test_native_read_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off Emscripten the read works exactly as before (fcntl present).

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: fixture used to force the native path.
        """
        path = tmp_path / "tokens_us.json"
        atomic_write_bytes(path, b'{"k": "v"}')
        monkeypatch.setattr(io_utils, "is_emscripten", lambda: False)
        assert read_credential_text(path) == '{"k": "v"}'
