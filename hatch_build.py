"""Hatchling build hook implementing the standard/slim dual-wheel build.

The wheel target statically *excludes* the CLI surface (``cli/`` and
``__main__.py``), so the raw file selection is the **slim**, library-only
wheel that micropip-installs cleanly under Pyodide. This hook adds the CLI
surface back via ``force_include`` for the DEFAULT build, so a plain
``uv build`` (and CI) produce the **standard** full wheel with the same files
as before — the default is unchanged.

Set ``MXD_BUILD_SLIM=1`` in the environment to skip the re-inclusion and emit
the slim wheel from the same source tree (a packaging-time exclude, never a
source strip).

Build commands (see the ``build-wheel`` / ``build-wheel-slim`` just recipes):

    uv build --wheel                      # standard (full) wheel
    MXD_BUILD_SLIM=1 uv build --wheel     # slim (library-only) wheel
"""

from __future__ import annotations

import os
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: Source → distribution path mapping for the CLI surface that the slim build
#: omits. Re-added via ``force_include`` for the standard build.
_CLI_SURFACE = {
    "src/mixpanel_headless/cli": "mixpanel_headless/cli",
    "src/mixpanel_headless/__main__.py": "mixpanel_headless/__main__.py",
}

#: Env var that, when set to ``"1"``, selects the slim (CLI-less) wheel.
_SLIM_ENV = "MXD_BUILD_SLIM"


class CustomBuildHook(BuildHookInterface):
    """Re-include the CLI surface unless a slim build was requested."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Add the CLI surface to ``force_include`` for standard builds.

        Args:
            version: The build version string (unused).
            build_data: Mutable build-data dict; the wheel builder honors its
                ``force_include`` mapping.
        """
        del version  # unused; signature fixed by BuildHookInterface
        if os.environ.get(_SLIM_ENV) == "1":
            # Slim build: leave the CLI surface excluded.
            return
        force_include = build_data.setdefault("force_include", {})
        for source, dest in _CLI_SURFACE.items():
            force_include[source] = dest
