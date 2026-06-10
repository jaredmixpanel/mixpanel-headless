"""Proves the base package imports cleanly without any CLI-only dependency.

This is the CPython proxy for the slim-wheel / Pyodide guarantee: the library
must ``import`` and construct its public surface with typer/rich/jq/click
absent, so ``micropip.install`` of the base (slim) wheel succeeds. The check
runs in a fresh subprocess with those modules blocked so an accidental
top-level CLI import would fail loudly. ``just test-pyodide-node`` re-proves
the same property in the real Emscripten runtime.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


class TestSlimImport:
    """The base import surface must not pull a CLI-only dependency."""

    def test_core_imports_without_cli_deps(self) -> None:
        """import + Workspace + transport + client load with CLI deps blocked."""
        script = textwrap.dedent(
            """
            import sys

            # A None entry makes `import <mod>` raise ImportError, mimicking a
            # base/slim install where the CLI-only deps were never installed.
            for mod in ("typer", "rich", "jq", "click", "pygments"):
                sys.modules[mod] = None

            import mixpanel_headless as mp

            assert mp.Workspace.__name__ == "Workspace"
            from mixpanel_headless._internal.api_client import MixpanelAPIClient
            from mixpanel_headless._internal.pyodide_transport import (
                PyfetchTransport,
            )
            from mixpanel_headless._internal.runtime import is_emscripten

            assert MixpanelAPIClient is not None
            assert PyfetchTransport is not None
            assert is_emscripten() is False
            print("SLIM_OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"slim import failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "SLIM_OK" in result.stdout
