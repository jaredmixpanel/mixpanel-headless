"""Tests for Pyodide-oriented OAuthStorage additions.

Covers the ``MP_OAUTH_CLIENT_DIR`` override for the DCR client-info path
(mirroring the existing ``MP_OAUTH_STORAGE_DIR`` pattern) and MEMFS chmod
tolerance under Emscripten. All paths are confined to ``tmp_path`` so nothing
touches the developer's real ``~/.mp``.
"""

from __future__ import annotations

import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mixpanel_headless._internal.auth import storage as storage_mod
from mixpanel_headless._internal.auth.storage import OAuthStorage
from mixpanel_headless._internal.auth.token import OAuthClientInfo


def _client_info(region: str = "us") -> OAuthClientInfo:
    """Build an OAuthClientInfo for round-trip tests.

    Args:
        region: Region to stamp on the client info.

    Returns:
        A fully-populated OAuthClientInfo.
    """
    return OAuthClientInfo(
        client_id="client-123",
        region=region,
        redirect_uri="http://localhost:19284/callback",
        scope="projects",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class TestClientDirOverride:
    """``MP_OAUTH_CLIENT_DIR`` redirects only the DCR client-info file."""

    def test_client_path_defaults_to_storage_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no override, the client file sits under the storage dir.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to clear the override env var.
        """
        monkeypatch.delenv("MP_OAUTH_CLIENT_DIR", raising=False)
        storage = OAuthStorage(storage_dir=tmp_path / "s")
        assert storage._client_path("us") == tmp_path / "s" / "client_us.json"

    def test_client_path_honors_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``MP_OAUTH_CLIENT_DIR`` relocates the client file's directory.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to set the override env var.
        """
        monkeypatch.setenv("MP_OAUTH_CLIENT_DIR", str(tmp_path / "cdir"))
        storage = OAuthStorage(storage_dir=tmp_path / "s")
        assert storage._client_path("eu") == tmp_path / "cdir" / "client_eu.json"

    def test_save_load_round_trips_through_override_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A saved client reads back from the override dir, not the storage dir.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to set the override env var.
        """
        cdir = tmp_path / "cdir"
        monkeypatch.setenv("MP_OAUTH_CLIENT_DIR", str(cdir))
        storage = OAuthStorage(storage_dir=tmp_path / "s")
        storage.save_client_info(_client_info("us"))
        assert (cdir / "client_us.json").is_file()
        assert not (tmp_path / "s" / "client_us.json").exists()
        loaded = storage.load_client_info("us")
        assert loaded is not None
        assert loaded.client_id == "client-123"
        assert loaded.region == "us"

    def test_tokens_unaffected_by_client_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token files still resolve under the storage dir when client is moved.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to set the override env var.
        """
        monkeypatch.setenv("MP_OAUTH_CLIENT_DIR", str(tmp_path / "cdir"))
        storage = OAuthStorage(storage_dir=tmp_path / "s")
        assert storage._tokens_path("us") == tmp_path / "s" / "tokens_us.json"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
    def test_override_dir_created_with_0o700(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The override dir is created with owner-only permissions.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to set the override env var.
        """
        cdir = tmp_path / "cdir"
        monkeypatch.setenv("MP_OAUTH_CLIENT_DIR", str(cdir))
        storage = OAuthStorage(storage_dir=tmp_path / "s")
        storage.save_client_info(_client_info("us"))
        assert stat.S_IMODE(cdir.stat().st_mode) == 0o700


class TestStorageChmodMemfsTolerance:
    """Under Emscripten, a chmod that MEMFS rejects must not abort writes."""

    def test_ensure_dir_tolerates_chmod_failure_under_emscripten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing dir chmod is swallowed under Emscripten, write still lands.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to fake Emscripten + a chmod error.
        """
        monkeypatch.delenv("MP_OAUTH_CLIENT_DIR", raising=False)
        monkeypatch.setattr(storage_mod, "is_emscripten", lambda: True)
        storage = OAuthStorage(storage_dir=tmp_path / "s")

        def _boom(self: Path, _mode: int) -> None:
            """Simulate MEMFS rejecting chmod."""
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(Path, "chmod", _boom)
        storage.save_client_info(_client_info("us"))
        assert (tmp_path / "s" / "client_us.json").is_file()

    def test_ensure_dir_chmod_failure_raises_on_native(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On native platforms a chmod failure still propagates (unchanged).

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to force native + a chmod error.
        """
        monkeypatch.delenv("MP_OAUTH_CLIENT_DIR", raising=False)
        monkeypatch.setattr(storage_mod, "is_emscripten", lambda: False)
        storage = OAuthStorage(storage_dir=tmp_path / "s")

        def _boom(self: Path, _mode: int) -> None:
            """Simulate a chmod rejection."""
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(Path, "chmod", _boom)
        with pytest.raises(OSError, match="not permitted"):
            storage.save_client_info(_client_info("us"))

    def test_ensure_account_dir_tolerates_chmod_under_emscripten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ensure_account_dir`` swallows a chmod failure under Emscripten.

        Args:
            tmp_path: pytest temp dir.
            monkeypatch: pytest fixture used to fake Emscripten + a chmod error.
        """
        monkeypatch.setenv("MP_OAUTH_STORAGE_DIR", str(tmp_path / "root"))
        monkeypatch.setattr(storage_mod, "is_emscripten", lambda: True)

        def _boom(self: Path, _mode: int) -> None:
            """Simulate MEMFS rejecting chmod."""
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(Path, "chmod", _boom)
        created = storage_mod.ensure_account_dir("personal")
        assert created.is_dir()
