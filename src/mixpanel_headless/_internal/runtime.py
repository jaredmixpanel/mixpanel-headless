"""Runtime-environment detection for Emscripten/Pyodide gating.

Every Pyodide-compatibility change in this library is **strictly additive
and gated**: native behavior is byte-identical, and the alternate path runs
only under Emscripten (the WASM target Pyodide builds against). This module
is the single seam that decision keys off, so the gate lives in one tested
place instead of being re-spelled as ``sys.platform == "emscripten"`` at
every call site.

Using a function (rather than an inline ``sys.platform`` comparison) is also
deliberate for type-checking: mypy statically resolves ``sys.platform`` to
the *current* build platform and would treat an inline
``if sys.platform == "emscripten":`` body as unreachable on CPython, leaving
the Emscripten-only code unanalyzed. An opaque call keeps those branches
type-checked on every platform.

Example:
    ```python
    from mixpanel_headless._internal.runtime import is_emscripten

    if is_emscripten():
        ...  # Pyodide-only path
    ```
"""

from __future__ import annotations

import sys


def is_emscripten() -> bool:
    """Return whether the interpreter is running under Emscripten/Pyodide.

    Pyodide compiles CPython for the Emscripten WASM platform, which reports
    ``sys.platform == "emscripten"``. Native CPython reports ``"linux"``,
    ``"darwin"``, ``"win32"``, etc., so this is a reliable, dependency-free
    discriminator.

    Returns:
        ``True`` when ``sys.platform`` equals ``"emscripten"`` (the Pyodide
        WASM build), ``False`` on every native platform.

    Example:
        ```python
        if is_emscripten():
            transport = PyfetchTransport()  # browser-backed HTTP
        ```
    """
    return sys.platform == "emscripten"
