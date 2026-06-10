"""Unit tests for the in-package API introspection surface (``mp.help``).

``mixpanel_headless.help`` renders human-readable (LLM-readable) API
documentation by introspecting the live package — a no-argument overview
of the public surface, and a dotted-path lookup that resolves any public
attribute and renders its signature + docstring. It is the
``run_python``-friendly counterpart to the CLI ``help.py`` script: it
**returns strings** (no printing, no ``sys.exit``) and imports only
Pyodide-safe stdlib.

These tests are written first per strict TDD; they define the contract and
fail until ``src/mixpanel_headless/help.py`` exists and is exported.

Pattern follows ``test_user_validators.py`` (module-level test functions,
fully docstringed helpers).
"""

from __future__ import annotations

import enum
import inspect

import pytest

import mixpanel_headless as mp
from mixpanel_headless import help as mp_help

# Imports that would break a Pyodide/Emscripten install (OS threads, fcntl,
# sockets, subprocess) or pull in the CLI-only extras. The help module must
# touch none of them — it is pure-stdlib introspection.
_FORBIDDEN_IMPORTS = frozenset(
    {
        "os",
        "fcntl",
        "threading",
        "multiprocessing",
        "subprocess",
        "socket",
        "asyncio",
        "typer",
        "rich",
        "click",
        "httpx",
    }
)


# =============================================================================
# help() — no-argument overview
# =============================================================================


def test_overview_returns_nonempty_str() -> None:
    """help() with no argument returns a non-empty string."""
    out = mp_help()
    assert isinstance(out, str)
    assert out.strip()


def test_overview_header_has_package_and_version() -> None:
    """The overview headline names the package and its version."""
    out = mp_help()
    assert "mixpanel_headless" in out
    assert mp.__version__ in out


def test_overview_lists_five_query_engines() -> None:
    """The overview surfaces all five CodeMode query engines by name."""
    out = mp_help()
    for engine in (
        "query",
        "query_funnel",
        "query_retention",
        "query_flow",
        "query_user",
    ):
        assert engine in out, f"missing query engine: {engine}"


def test_overview_mentions_builders_and_discovery() -> None:
    """The overview surfaces param builders and discovery verbs."""
    out = mp_help()
    assert "build_params" in out
    assert "build_funnel_params" in out
    # Discovery surface.
    assert "events" in out
    assert "properties" in out


def test_overview_mentions_crud_areas() -> None:
    """The overview groups entity CRUD by area (dashboards, cohorts, ...)."""
    out = mp_help()
    assert "dashboard" in out
    assert "cohort" in out
    assert "experiment" in out


def test_overview_lists_namespaces() -> None:
    """The overview names the three top-level namespaces."""
    out = mp_help()
    for namespace in ("accounts", "session", "targets"):
        assert namespace in out


def test_overview_is_complete_over_workspace_methods() -> None:
    """Every public Workspace method name appears in the overview.

    Anti-drift guarantee: the overview is derived from introspection, so a
    newly-added method must surface automatically (in a named group or the
    catch-all) rather than being silently dropped.
    """
    out = mp_help()
    public_methods = [
        name
        for name in dir(mp.Workspace)
        if not name.startswith("_")
        and not isinstance(inspect.getattr_static(mp.Workspace, name, None), property)
    ]
    missing = [name for name in public_methods if name not in out]
    assert not missing, f"overview omits Workspace methods: {missing}"


# =============================================================================
# help(target) — dotted-path resolution
# =============================================================================


def test_method_renders_signature_and_docstring() -> None:
    """help('Workspace.query') renders the signature and full docstring."""
    out = mp_help("Workspace.query")
    assert "query(" in out
    # A representative keyword-only parameter appears in the rendered signature.
    assert "from_date" in out
    # The first docstring line is included verbatim.
    assert "Run a typed insights query against the Mixpanel API." in out


def test_method_render_includes_return_annotation() -> None:
    """A rendered method signature includes its ``->`` return annotation."""
    out = mp_help("Workspace.query")
    assert "->" in out
    assert "QueryResult" in out


def test_method_render_drops_self() -> None:
    """The rendered signature omits the bound ``self`` parameter."""
    out = mp_help("Workspace.events")
    # The signature line (first line) must not present ``self`` as a param.
    first_line = out.splitlines()[0]
    assert "self" not in first_line


def test_top_level_class_renders() -> None:
    """help('Filter') renders a class with its docstring."""
    out = mp_help("Filter")
    assert "Filter" in out
    assert "class" in out.lower()


def test_enum_renders_members() -> None:
    """help('FeatureFlagStatus') lists the enum members."""
    out = mp_help("FeatureFlagStatus")
    for member in ("ENABLED", "DISABLED", "ARCHIVED"):
        assert member in out


def test_literal_alias_renders_values() -> None:
    """help('MathType') renders the allowed Literal values."""
    out = mp_help("MathType")
    assert "total" in out
    assert "unique" in out


def test_constant_renders_value() -> None:
    """help('BUSINESS_CONTEXT_MAX_CHARS') renders the constant value."""
    out = mp_help("BUSINESS_CONTEXT_MAX_CHARS")
    assert "50000" in out


def test_union_alias_renders_member_types() -> None:
    """help('Account') renders a Union type alias with its member types."""
    out = mp_help("Account")
    assert "Account" in out
    assert "ServiceAccount" in out  # one of the Union members


def test_namespace_module_renders_members() -> None:
    """help('accounts') lists the namespace's public callables."""
    out = mp_help("accounts")
    assert "login_unified" in out
    assert "add" in out


def test_nested_dotted_path_on_namespace() -> None:
    """help('accounts.login_unified') resolves through the module namespace."""
    out = mp_help("accounts.login_unified")
    assert "login_unified(" in out


def test_workspace_class_renders_method_catalog() -> None:
    """help('Workspace') renders the grouped method catalog."""
    out = mp_help("Workspace")
    assert "query" in out
    assert "Workspace" in out


# =============================================================================
# Unknown targets — return helpful strings, never raise
# =============================================================================


def test_unknown_top_level_returns_error_string_with_suggestion() -> None:
    """An unknown target returns a string (not an exception) with near-matches."""
    out = mp_help("Workspaze")
    assert isinstance(out, str)
    assert "Workspace" in out  # near-match suggestion
    # Communicates failure rather than pretending to resolve.
    assert "not found" in out.lower() or "did you mean" in out.lower()


def test_unknown_attribute_suggests_siblings() -> None:
    """An unknown attribute on a resolved parent suggests sibling names."""
    out = mp_help("Workspace.queyr")
    assert isinstance(out, str)
    assert "query" in out  # fuzzy sibling suggestion
    assert "Workspace." in out


def test_unknown_attribute_with_no_match_lists_available() -> None:
    """An unknown member with no near-match lists the parent's members."""
    out = mp_help("accounts.qqqqqqqqqq")
    assert isinstance(out, str)
    assert "not found" in out.lower()
    assert "Available on" in out
    assert "accounts" in out


def test_unknown_with_no_close_match_still_returns_str() -> None:
    """A nonsense target returns a graceful string and never raises."""
    out = mp_help("zzzzzzzzzzzzzz")
    assert isinstance(out, str)
    assert out.strip()


def test_unknown_does_not_raise_for_any_garbage() -> None:
    """Resolution never raises, regardless of how malformed the path is."""
    for target in ("", ".", "a.b.c.d.e", "Workspace.", ".query", "123"):
        out = mp_help(target)
        assert isinstance(out, str)


# =============================================================================
# Side-effect freedom + Pyodide safety + export wiring
# =============================================================================


def test_help_is_a_function_not_the_submodule() -> None:
    """``mp.help`` resolves to the callable, not the ``help`` submodule.

    Guards the ``from .help import help`` shadowing gotcha: the exported
    name must be the function so ``mp.help(...)`` works under ``run_python``.
    """
    assert callable(mp_help)
    assert inspect.isfunction(mp_help)
    assert not inspect.ismodule(mp_help)


def test_help_exported_in_dunder_all() -> None:
    """``help`` is part of the package's public ``__all__``."""
    assert "help" in mp.__all__


def test_overview_produces_no_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """help() returns its content and writes nothing to stdout/stderr."""
    mp_help()
    mp_help("Workspace.query")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_help_module_imports_are_pyodide_safe() -> None:
    """The help module imports no thread/fcntl/socket/CLI-only modules.

    The feature must survive a slim-wheel install under Emscripten, so it may
    only depend on pure-stdlib introspection helpers.
    """
    help_module = inspect.getmodule(mp_help)
    assert help_module is not None
    imported = {
        name for name, value in vars(help_module).items() if inspect.ismodule(value)
    }
    leaked = imported & _FORBIDDEN_IMPORTS
    assert not leaked, f"help module imports forbidden modules: {leaked}"


# =============================================================================
# No-crash sweeps — the heart of the contract
# =============================================================================


def test_sweep_every_top_level_export_renders() -> None:
    """help(name) returns a non-empty string for every top-level export."""
    for name in mp.__all__:
        out = mp_help(name)
        assert isinstance(out, str), f"{name} did not return a str"
        assert out.strip(), f"{name} returned an empty string"


def test_sweep_every_workspace_member_renders() -> None:
    """help('Workspace.<member>') renders for every public Workspace member."""
    for name in dir(mp.Workspace):
        if name.startswith("_"):
            continue
        out = mp_help(f"Workspace.{name}")
        assert isinstance(out, str), f"Workspace.{name} did not return a str"
        assert out.strip(), f"Workspace.{name} returned an empty string"


def test_sweep_enum_members_render() -> None:
    """help('Enum.MEMBER') renders for enum members without crashing."""
    for name in mp.__all__:
        obj = getattr(mp, name)
        if isinstance(obj, type) and issubclass(obj, enum.Enum):
            for member in obj:
                out = mp_help(f"{name}.{member.name}")
                assert isinstance(out, str)
                assert out.strip()
