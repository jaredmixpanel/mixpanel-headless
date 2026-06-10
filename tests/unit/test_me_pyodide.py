"""Tests for MEMFS chmod tolerance in MeCache under Emscripten.

The ``/me`` cache holds PII (emails, org/project names), so on native
platforms a directory that refuses ``0o700`` is a hard ConfigError — that
behavior is unchanged and covered by ``test_me.py``. Under Emscripten the
cache lives on Pyodide's per-worker, ephemeral MEMFS where mode bits are
unenforceable and a chmod can raise for a no-op; there the failure must be
tolerated so the cache write still lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mixpanel_headless._internal import me as me_mod
from mixpanel_headless._internal.me import MeCache, MeResponse
from mixpanel_headless.exceptions import ConfigError


def _chmod_failing_on(cache_dir: Path) -> Any:
    """Build an ``os.chmod`` replacement that fails only for ``cache_dir``.

    Args:
        cache_dir: The directory whose chmod should raise.

    Returns:
        A drop-in for ``os.chmod`` that raises ``OSError`` for ``cache_dir``
        and delegates to the real chmod otherwise.
    """
    real_chmod = __import__("os").chmod

    def _chmod(path: Any, mode: int) -> None:
        """Raise for the target dir; otherwise delegate to the real chmod."""
        if str(path) == str(cache_dir):
            raise OSError(13, "Permission denied")
        real_chmod(path, mode)

    return _chmod


class TestMeCacheChmodMemfsTolerance:
    """``MeCache.put`` tolerates a chmod failure only under Emscripten."""

    def test_put_tolerates_chmod_failure_under_emscripten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under Emscripten the cache still writes despite a chmod rejection.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to fake Emscripten + a chmod error.
        """
        cache_dir = tmp_path / "accounts" / "personal"
        cache_dir.mkdir(parents=True)
        cache = MeCache(account_name="personal", storage_dir=cache_dir)
        resp = MeResponse(user_id=1, user_email="a@example.com")

        monkeypatch.setattr(me_mod, "is_emscripten", lambda: True)
        monkeypatch.setattr("os.chmod", _chmod_failing_on(cache_dir))

        cache.put(resp)  # must not raise
        loaded = cache.get()
        assert loaded is not None
        assert loaded.user_id == 1

    def test_put_chmod_failure_still_raises_on_native(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On native platforms the chmod failure still surfaces a ConfigError.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to force native + a chmod error.
        """
        cache_dir = tmp_path / "accounts" / "personal"
        cache_dir.mkdir(parents=True)
        cache = MeCache(account_name="personal", storage_dir=cache_dir)
        resp = MeResponse(user_id=1, user_email="a@example.com")

        monkeypatch.setattr(me_mod, "is_emscripten", lambda: False)
        monkeypatch.setattr("os.chmod", _chmod_failing_on(cache_dir))

        with pytest.raises(ConfigError, match="0o700"):
            cache.put(resp)
