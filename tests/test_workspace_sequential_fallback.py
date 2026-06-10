"""Parity + failure-isolation tests for the Emscripten sequential fallback.

Pyodide has no OS threads, so both ``ThreadPoolExecutor`` sites in
``Workspace`` — ``_execute_user_query_parallel`` and ``fetch_replays`` —
fall back to a sequential loop when ``is_emscripten()`` is true. Only the
*fetch* loop is gated; the result-assembly after it is shared, so these
tests force the sequential path (gate monkeypatched on) and assert the data
(ordering and content) matches the threaded path, plus the per-site failure
semantics (skip non-fatal, re-raise fatal).
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

import mixpanel_headless as mp
from mixpanel_headless.exceptions import RateLimitError
from mixpanel_headless.types import ProfilePageResult, Replay
from tests.conftest import make_session

_GATE = "mixpanel_headless.workspace.is_emscripten"


def _force(monkeypatch: pytest.MonkeyPatch, *, emscripten: bool) -> None:
    """Pin the workspace Emscripten gate for the duration of a test.

    Args:
        monkeypatch: pytest fixture.
        emscripten: Value ``is_emscripten()`` should report inside workspace.
    """
    monkeypatch.setattr(_GATE, lambda: emscripten)


def _ws(client: MagicMock) -> mp.Workspace:
    """Build a Workspace bound to a fake session and a mock API client.

    Args:
        client: Mock standing in for :class:`MixpanelAPIClient`.

    Returns:
        A Workspace wired to ``client``.
    """
    return mp.Workspace(session=make_session(), _api_client=client)


def _page(page: int, total: int, page_size: int) -> ProfilePageResult:
    """Build one deterministic ``ProfilePageResult``.

    Args:
        page: Zero-based page index.
        total: Total profiles across all pages.
        page_size: Profiles per page.

    Returns:
        A page result whose profiles have stable, page-derived ids.
    """
    start = page * page_size
    count = max(0, min(page_size, total - start))
    profiles = [
        {
            "$distinct_id": f"u{start + i:04d}",
            "$properties": {"$last_seen": "2025-01-01T00:00:00"},
        }
        for i in range(count)
    ]
    return ProfilePageResult(
        profiles=profiles,
        page=page,
        total=total,
        page_size=page_size,
        session_id="sess",
        has_more=page < math.ceil(total / page_size) - 1,
    )


def _profiles_side_effect(
    total: int,
    page_size: int,
    *,
    fail: set[int] | None = None,
    fatal: dict[int, Exception] | None = None,
) -> Any:
    """Build an ``export_profiles_page`` side effect keyed on the ``page`` kwarg.

    Args:
        total: Total profiles across pages.
        page_size: Profiles per page.
        fail: Pages that raise a generic (non-fatal) ``Exception``.
        fatal: ``{page: exception}`` raising a fatal API error for that page.

    Returns:
        A callable suitable for ``MagicMock.side_effect``.
    """

    def _se(*_args: Any, page: int = 0, **_kwargs: Any) -> ProfilePageResult:
        """Return the requested page or raise the configured failure."""
        if fatal and page in fatal:
            raise fatal[page]
        if fail and page in fail:
            raise Exception(f"boom on page {page}")
        return _page(page, total, page_size)

    return _se


def _replay(replay_id: str) -> Replay:
    """Build a minimal valid Replay for bundle-assembly parity checks.

    Args:
        replay_id: Identifier to stamp on the replay.

    Returns:
        A Replay with fixed timestamps and project.
    """
    return Replay(
        replay_id=replay_id,
        distinct_id=None,
        project_id=12345,
        start_time=1716810000000,
        end_time=1716810005000,
        retention_days=30,
    )


class TestQueryUserSequentialFallback:
    """``_execute_user_query_parallel`` sequential path matches the threaded one."""

    def test_sequential_data_matches_parallel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same profiles (order + content) and totals, regardless of mode.

        Args:
            monkeypatch: pytest fixture used to flip the Emscripten gate.
        """
        _force(monkeypatch, emscripten=True)
        seq_client = MagicMock()
        seq_client.export_profiles_page.side_effect = _profiles_side_effect(250, 100)
        seq_ws = _ws(seq_client)
        seq = seq_ws.query_user(mode="profiles", parallel=True, limit=100_000)
        seq_ws.close()

        _force(monkeypatch, emscripten=False)
        par_client = MagicMock()
        par_client.export_profiles_page.side_effect = _profiles_side_effect(250, 100)
        par_ws = _ws(par_client)
        par = par_ws.query_user(mode="profiles", parallel=True, limit=100_000)
        par_ws.close()

        # Full list equality proves identical ordering AND content.
        assert seq.profiles == par.profiles
        assert len(seq.profiles) == 250
        assert seq.total == par.total == 250
        assert seq.meta["parallel"] is False
        assert par.meta["parallel"] is True
        assert seq.meta["pages_fetched"] == par.meta["pages_fetched"] == 3

    def test_sequential_isolates_non_fatal_failed_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-fatal page error is recorded in ``failed_pages``, others kept.

        Args:
            monkeypatch: pytest fixture used to flip the Emscripten gate.
        """
        _force(monkeypatch, emscripten=True)
        client = MagicMock()
        client.export_profiles_page.side_effect = _profiles_side_effect(
            300, 100, fail={2}
        )
        ws = _ws(client)
        res = ws.query_user(mode="profiles", parallel=True, limit=100_000)
        ws.close()
        assert res.meta["failed_pages"] == [2]
        assert res.meta["parallel"] is False
        assert len(res.profiles) == 200  # pages 0 + 1; page 2 dropped

    def test_sequential_reraises_fatal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fatal API error (rate limit) propagates instead of being swallowed.

        Args:
            monkeypatch: pytest fixture used to flip the Emscripten gate.
        """
        _force(monkeypatch, emscripten=True)
        client = MagicMock()
        client.export_profiles_page.side_effect = _profiles_side_effect(
            300, 100, fatal={1: RateLimitError("rate limited")}
        )
        ws = _ws(client)
        with pytest.raises(RateLimitError):
            ws.query_user(mode="profiles", parallel=True, limit=100_000)
        ws.close()


class TestFetchReplaysSequentialFallback:
    """``fetch_replays`` sequential path matches the threaded one."""

    def test_sequential_order_matches_parallel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both modes return replays in input order.

        Args:
            monkeypatch: pytest fixture used to flip the Emscripten gate.
        """
        ids = ["r-1", "r-2", "r-3"]

        _force(monkeypatch, emscripten=True)
        seq_ws = _ws(MagicMock())
        monkeypatch.setattr(seq_ws, "fetch_replay", lambda rid, **_k: _replay(rid))
        seq = seq_ws.fetch_replays(ids)

        _force(monkeypatch, emscripten=False)
        par_ws = _ws(MagicMock())
        monkeypatch.setattr(par_ws, "fetch_replay", lambda rid, **_k: _replay(rid))
        par = par_ws.fetch_replays(ids)

        assert [r.replay_id for r in seq.replays] == ids
        assert [r.replay_id for r in par.replays] == ids

    def test_sequential_skips_per_replay_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One failing replay is skipped; the rest survive in order.

        Args:
            monkeypatch: pytest fixture used to flip the Emscripten gate.
        """
        _force(monkeypatch, emscripten=True)
        ws = _ws(MagicMock())

        def _fetch(rid: str, **_kwargs: Any) -> Replay:
            """Fail r-2, succeed otherwise."""
            if rid == "r-2":
                raise RuntimeError("CDN stall")
            return _replay(rid)

        monkeypatch.setattr(ws, "fetch_replay", _fetch)
        bundle = ws.fetch_replays(["r-1", "r-2", "r-3"])
        assert [r.replay_id for r in bundle.replays] == ["r-1", "r-3"]

    def test_sequential_all_fail_raises_first_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every replay fails, the first underlying error propagates.

        Args:
            monkeypatch: pytest fixture used to flip the Emscripten gate.
        """
        _force(monkeypatch, emscripten=True)
        ws = _ws(MagicMock())

        def _fetch(rid: str, **_kwargs: Any) -> Replay:
            """Always fail."""
            raise RuntimeError(f"down {rid}")

        monkeypatch.setattr(ws, "fetch_replay", _fetch)
        with pytest.raises(RuntimeError):
            ws.fetch_replays(["r-1", "r-2"])
