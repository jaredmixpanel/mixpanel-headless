"""Tests that the optional jq dependency degrades gracefully when absent.

jq is a CLI-only, binary extra (``mixpanel_headless[jq]``) excluded from the
base install so the slim wheel micropip-installs cleanly under Pyodide. The
CLI must still import without jq and only fail — with a clear install hint —
when the ``--jq`` filter is actually invoked.
"""

from __future__ import annotations

import sys

import pytest
import typer

from mixpanel_headless.cli.utils import _apply_jq_filter


class TestJqOptionalDegradation:
    """``_apply_jq_filter`` exits cleanly when jq is not installed."""

    def test_missing_jq_exits_with_install_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing jq surfaces a clean Exit(3) naming the optional extra.

        Args:
            monkeypatch: pytest fixture used to force ``import jq`` to fail.
            capsys: captures the stderr install hint.
        """
        # A ``None`` entry in sys.modules makes ``import jq`` raise ImportError.
        monkeypatch.setitem(sys.modules, "jq", None)
        with pytest.raises(typer.Exit) as exc_info:
            _apply_jq_filter('{"a": 1}', ".a")
        assert exc_info.value.exit_code == 3
        err = capsys.readouterr().err
        assert "mixpanel_headless[jq]" in err

    def test_jq_present_filters_normally(self) -> None:
        """With jq installed (dev/test env), filtering still works unchanged."""
        assert _apply_jq_filter('{"name": "x"}', ".name") == ["x"]
