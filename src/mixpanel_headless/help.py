"""In-package API introspection — the ``run_python``-friendly ``mp.help``.

This module renders human- and LLM-readable documentation for the
``mixpanel_headless`` public API by *introspecting the live package*, so the
output never drifts from the code:

- ``help()`` — a compact overview of the public surface: the top-level
  exports (namespaces, functions, type/enum/exception counts) plus the
  ``Workspace`` facade grouped into the five CodeMode query engines, param
  builders, streaming, session-replay, entity CRUD, and discovery/operations.
- ``help("Workspace.query")`` / ``help("Filter")`` / ``help("accounts")`` —
  resolves a dotted attribute path against the package and renders the
  signature (via :func:`inspect.signature`) and full docstring.

Design contract (important — this is the public ``mp.help`` surface):

- It **returns strings**; it never prints, never calls ``sys.exit``, and has
  no side effects. Callers print the result (``print(mp.help(...))``).
- An unknown target returns a descriptive string with near-matches, **not**
  an exception — the output is meant to be read by an LLM mid-``run_python``.
- It imports only Pyodide-safe pure-stdlib helpers (``inspect``, ``difflib``,
  ``importlib``, ``enum``, ``typing``, ``re``, ``textwrap``). It pulls in
  nothing that needs OS threads / ``fcntl`` / sockets and none of the
  CLI-only extras (Typer/Rich/jq), so it works under the slim Emscripten
  wheel.

The package is imported lazily (via :func:`importlib.import_module`) rather
than at module top level: ``__init__`` imports this module, so a top-level
``import mixpanel_headless`` here would be circular.
"""

from __future__ import annotations

import difflib
import enum
import importlib
import inspect
import re
import textwrap
import typing
from typing import Any

_PACKAGE = "mixpanel_headless"

# The five CodeMode query engines (SPEC). Kept as a small, stable, spec-defined
# constant; everything else about the overview is derived from introspection.
# Engines not present at runtime are simply skipped (and would then surface in
# the catch-all group), so this can never hide a method.
_QUERY_ENGINES: tuple[str, ...] = (
    "query",
    "query_funnel",
    "query_retention",
    "query_flow",
    "query_user",
)

# Entity nouns for grouping CRUD methods into areas, ordered most-specific
# first so e.g. ``feature_flag`` matches before ``flag`` and ``custom_event``
# before ``event``. This is a *grouping aid* only: methods that match no noun
# fall through to the catch-all group, so a brand-new entity is never dropped
# from the overview — it just lands in "discovery & operations" until a noun
# is added here.
_ENTITY_NOUNS: tuple[str, ...] = (
    "custom_property",
    "custom_event",
    "lookup_table",
    "drop_filter",
    "feature_flag",
    "annotation_tag",
    "lexicon_tag",
    "blueprint",
    "dashboard",
    "bookmark",
    "experiment",
    "annotation",
    "webhook",
    "cohort",
    "alert",
    "schema",
)


# =============================================================================
# Public entry point
# =============================================================================


def help(target: str | None = None) -> str:
    """Render API documentation for ``mixpanel_headless`` as a string.

    With no argument, returns a compact overview of the public surface. With a
    dotted attribute path, resolves it against the package and renders the
    target's signature and docstring. This is designed to be called from
    ``run_python`` and printed (``print(mp.help("Workspace.query"))``).

    Args:
        target: Optional dotted path to a public attribute, such as
            ``"Workspace.query"``, ``"Filter"``, ``"accounts"``, or
            ``"accounts.login_unified"``. When ``None`` (the default), the
            no-argument overview is returned.

    Returns:
        A formatted documentation string. For an unrecognized ``target`` the
        string describes the failure and lists near-matches; it is never an
        exception, because the output is intended to be read by an LLM.

    Raises:
        None: All resolution failures are reported in the returned string
        rather than raised.

    Example:
        ```python
        import mixpanel_headless as mp

        print(mp.help())                      # API overview
        print(mp.help("Workspace.query"))     # signature + docstring
        print(mp.help("Filter"))              # type docs
        ```
    """
    package = _load_package()
    if target is None:
        return _render_overview(package)
    try:
        resolved = _resolve(package, target)
    except AttributeError:
        return _not_found(package, target)
    return _render_object(package, resolved, target)


# =============================================================================
# Resolution
# =============================================================================


def _load_package() -> Any:
    """Import and return the ``mixpanel_headless`` package object.

    Imported lazily to avoid a circular import (``__init__`` imports this
    module during package initialization).

    Returns:
        The imported ``mixpanel_headless`` module object. Typed as ``Any``
        because the renderers access dynamically-introspected attributes
        (``__all__``, ``Workspace``, …) that a static module type forbids.
    """
    return importlib.import_module(_PACKAGE)


def _resolve(package: Any, target: str) -> Any:
    """Walk a dotted path from the package to the referenced attribute.

    Args:
        package: The root package object to resolve against.
        target: A dotted attribute path, e.g. ``"Workspace.query"``.

    Returns:
        The resolved attribute (any object).

    Raises:
        AttributeError: If any path segment does not exist (including the
            empty-string segments produced by malformed paths like ``"a."``).
    """
    obj: Any = package
    for part in target.split("."):
        obj = getattr(obj, part)
    return obj


# =============================================================================
# Rendering — dispatch
# =============================================================================


def _render_object(package: Any, obj: Any, path: str) -> str:
    """Render a resolved object according to its kind.

    Args:
        package: The root package object (used to special-case the facade).
        obj: The resolved object to document.
        path: The dotted path the object was resolved from (used as a title).

    Returns:
        The formatted documentation string for ``obj``.
    """
    if isinstance(obj, property):
        return _render_property(obj, path)
    if inspect.ismodule(obj):
        return _render_module(obj, path)
    if isinstance(obj, type):
        if issubclass(obj, enum.Enum):
            return _render_enum(obj, path)
        if obj is getattr(package, "Workspace", None):
            return _render_workspace_surface(obj, include_header=True)
        return _render_class(obj, path)
    # Typing constructs (Literal/Union/generic aliases) are callable, so this
    # must precede the callable branch or e.g. ``MathType`` renders as a func.
    if typing.get_origin(obj) is not None:
        return _render_alias_or_constant(obj, path)
    if callable(obj):
        return _render_callable(obj, path)
    return _render_alias_or_constant(obj, path)


def _render_callable(obj: Any, path: str) -> str:
    """Render a function or method as ``signature`` + docstring.

    Args:
        obj: A callable (module-level function or unbound method).
        path: The dotted path used as the display qualname.

    Returns:
        The formatted signature followed by the full docstring.
    """
    signature = _format_signature(obj, path)
    doc = inspect.getdoc(obj) or "(no docstring)"
    return f"{signature}\n\n{doc}"


def _render_property(obj: Any, path: str) -> str:
    """Render a property descriptor as a typed accessor + docstring.

    Args:
        obj: A :class:`property` descriptor (as obtained from the class).
        path: The dotted path used as the display name.

    Returns:
        A one-line accessor description followed by the property's docstring.
    """
    return_type = ""
    getter = obj.fget
    if getter is not None:
        try:
            signature = inspect.signature(getter)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            return_type = _format_type(signature.return_annotation)
    doc = inspect.getdoc(obj)
    if not doc and getter is not None:
        doc = inspect.getdoc(getter)
    suffix = f" -> {return_type}" if return_type else ""
    return f"{path}{suffix}  (property)\n\n{doc or '(no docstring)'}"


def _render_class(obj: Any, path: str) -> str:
    """Render a (non-enum) class as a header + constructor signature + doc.

    Args:
        obj: A class object.
        path: The dotted path used as the display name.

    Returns:
        The class header, its constructor signature when introspectable, and
        the class docstring.
    """
    bases = [base.__name__ for base in obj.__mro__[1:] if base.__name__ != "object"]
    header = f"class {path}" + (f"({', '.join(bases)})" if bases else "")
    parts = [header]
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parts.append(f"\nConstructor:\n{_format_signature(obj, path)}")
    doc = inspect.getdoc(obj) or "(no docstring)"
    parts.append(f"\n{doc}")
    return "\n".join(parts)


def _render_enum(obj: Any, path: str) -> str:
    """Render an :class:`enum.Enum` subclass as its members + docstring.

    Args:
        obj: An ``Enum`` subclass.
        path: The dotted path used as the display name.

    Returns:
        A header, one ``NAME = value`` line per member, and the docstring.
    """
    members = list(obj)
    lines = [f"{path} — Enum ({len(members)} members)", ""]
    lines.extend(f"  {member.name} = {member.value!r}" for member in members)
    doc = inspect.getdoc(obj)
    # ``Enum`` injects a default "An enumeration." docstring; suppress it.
    if doc and doc.strip() != "An enumeration.":
        lines.append(f"\n{doc}")
    return "\n".join(lines)


def _render_module(obj: Any, path: str) -> str:
    """Render a namespace module as a list of its public members.

    Args:
        obj: A module object (e.g. the ``accounts`` namespace).
        path: The dotted path used as the display name.

    Returns:
        A header, the module summary, and one line per public member with its
        first docstring line.
    """
    members = _public_member_summaries(obj)
    lines = [f"{path} — namespace ({len(members)} members)"]
    summary = _summary_paragraph(obj)
    if summary:
        lines.append(f"\n{summary}")
    lines.append("")
    for name, first_line in members:
        lines.append(f"  {name}" + (f" — {first_line}" if first_line else ""))
    return "\n".join(lines)


def _render_alias_or_constant(obj: Any, path: str) -> str:
    """Render a type alias, generic/Union alias, or plain constant value.

    Args:
        obj: A typing alias (``Literal[...]`` / ``X | Y``), or any non-callable
            value (e.g. an ``int`` constant or an enum member).
        path: The dotted path used as the display name.

    Returns:
        For a ``Literal`` alias, the allowed values; for a Union/generic alias,
        the cleaned alias and its member types; otherwise ``path = <repr>``.
    """
    args = typing.get_args(obj)
    if typing.get_origin(obj) is typing.Literal:
        values = ", ".join(repr(arg) for arg in args)
        return f"{path} — Literal type alias\n\nAllowed values: {values}"
    if args:
        members = ", ".join(
            getattr(arg, "__name__", None) or _format_type(arg) for arg in args
        )
        return (
            f"{path} — type alias\n\n{path} = {_format_type(obj)}\nMembers: {members}"
        )
    return f"{path} = {obj!r}\n\n(value of type {type(obj).__name__})"


# =============================================================================
# Rendering — the Workspace facade surface
# =============================================================================


def _render_workspace_surface(workspace_cls: Any, *, include_header: bool) -> str:
    """Render the ``Workspace`` facade as a grouped catalog of its surface.

    Used both by the no-argument overview and by ``help("Workspace")``. The
    five query engines are shown with one-line summaries; every other public
    method is listed by name under a derived group, so the rendering is
    complete (no method is silently dropped).

    Args:
        workspace_cls: The ``Workspace`` class object.
        include_header: When ``True``, prepend the ``class Workspace`` header
            and summary paragraph (for ``help("Workspace")``); when ``False``,
            render only the grouped surface (for the overview).

    Returns:
        The formatted, grouped Workspace surface.
    """
    properties = sorted(
        name
        for name in _public_names(workspace_cls)
        if isinstance(inspect.getattr_static(workspace_cls, name, None), property)
    )
    methods = sorted(
        name
        for name in _public_names(workspace_cls)
        if not isinstance(inspect.getattr_static(workspace_cls, name, None), property)
    )

    lines: list[str] = []
    if include_header:
        lines.append("class Workspace — the primary facade")
        summary = _summary_paragraph(workspace_cls)
        if summary:
            lines.append(f"\n{summary}")
        lines.append("")

    lines.append(f"Workspace surface ({len(properties) + len(methods)} members):")
    if properties:
        lines.append(f"  Context (properties): {', '.join(properties)}")

    for title, names in _group_workspace_methods(workspace_cls, methods):
        if title.startswith("Query engines"):
            lines.append(f"  {title}:")
            for name in names:
                first_line = _first_line(inspect.getattr_static(workspace_cls, name))
                lines.append(f"    {name}" + (f" — {first_line}" if first_line else ""))
        else:
            lines.append(f"  {title}:")
            lines.append(_wrap_names(names, indent="    "))
    return "\n".join(lines)


def _group_workspace_methods(
    workspace_cls: Any, methods: list[str]
) -> list[tuple[str, list[str]]]:
    """Partition Workspace methods into ordered, mutually-exclusive groups.

    Every method lands in exactly one group; whatever matches no specific rule
    falls into the final catch-all group, so the union of all groups always
    equals ``methods`` (the anti-drift / no-silent-drop guarantee).

    Args:
        workspace_cls: The ``Workspace`` class (unused for content, kept for a
            stable signature alongside :func:`_render_workspace_surface`).
        methods: Sorted public method names to group.

    Returns:
        An ordered list of ``(group_title, method_names)`` pairs, omitting any
        group that ended up empty.
    """
    del workspace_cls  # content is derived purely from the method names
    used: set[str] = set()
    groups: list[tuple[str, list[str]]] = []

    engines = [name for name in _QUERY_ENGINES if name in methods]
    used.update(engines)
    groups.append(("Query engines (CodeMode)", engines))

    builders = sorted(
        name for name in methods if name.startswith("build_") and name not in used
    )
    used.update(builders)
    groups.append(("Param builders", builders))

    streaming = sorted(
        name for name in methods if name.startswith("stream_") and name not in used
    )
    used.update(streaming)
    groups.append(("Streaming", streaming))

    replay = sorted(name for name in methods if "replay" in name and name not in used)
    used.update(replay)
    groups.append(("Session replay", replay))

    for noun in _ENTITY_NOUNS:
        area = sorted(name for name in methods if noun in name and name not in used)
        if area:
            used.update(area)
            groups.append((f"CRUD · {noun}", area))

    catch_all = sorted(name for name in methods if name not in used)
    groups.append(("Discovery, typed queries & operations", catch_all))

    return [(title, names) for title, names in groups if names]


# =============================================================================
# Rendering — the no-argument overview
# =============================================================================


def _render_overview(package: Any) -> str:
    """Render the compact, no-argument API overview.

    Args:
        package: The ``mixpanel_headless`` package object.

    Returns:
        The full overview string: headline, usage, a classified summary of the
        top-level exports, and the grouped ``Workspace`` surface.
    """
    version = str(getattr(package, "__version__", "?"))
    buckets = _classify_top_level(package)
    total = len(_unique_exports(package))

    title = f"{_PACKAGE} {version} — programmable interface to Mixpanel analytics"
    lines = [title, "=" * len(title), ""]
    lines.append("Usage:")
    lines.append("  import mixpanel_headless as mp")
    lines.append('  print(mp.help("Workspace.query"))   # signature + docstring')
    lines.append("")
    lines.append(f"Top-level exports ({total}):")
    lines.append("  Facade:        Workspace")
    lines.append(f"  Namespaces:    {', '.join(buckets['module'])}")
    lines.append(f"  Functions:     {', '.join(buckets['function'])}")
    lines.append(
        f"  Data types:    {len(buckets['class'])}  (e.g. {_examples(buckets['class'])})"
    )
    lines.append(
        f"  Type aliases:  {len(buckets['alias'])}  (e.g. {_examples(buckets['alias'])})"
    )
    lines.append(
        f"  Enums:         {len(buckets['enum'])}  ({', '.join(buckets['enum'])})"
    )
    lines.append(
        f"  Exceptions:    {len(buckets['exception'])}  (e.g. {_examples(buckets['exception'])})"
    )
    lines.append("")
    lines.append(_render_workspace_surface(package.Workspace, include_header=False))
    lines.append("")
    lines.append(
        'Drill in:  mp.help("Workspace.<method>"), mp.help("<Type>"), mp.help("accounts")'
    )
    return "\n".join(lines)


def _classify_top_level(package: Any) -> dict[str, list[str]]:
    """Classify the package's ``__all__`` exports by kind.

    Args:
        package: The ``mixpanel_headless`` package object.

    Returns:
        A mapping with keys ``module``, ``function``, ``class`` (data types,
        excluding the ``Workspace`` facade), ``exception``, ``enum``, and
        ``alias`` (type aliases / constants), each holding deduplicated export
        names in ``__all__`` order.
    """
    buckets: dict[str, list[str]] = {
        "module": [],
        "function": [],
        "class": [],
        "exception": [],
        "enum": [],
        "alias": [],
    }
    for name in _unique_exports(package):
        obj = getattr(package, name)
        if inspect.ismodule(obj):
            buckets["module"].append(name)
        elif isinstance(obj, type) and issubclass(obj, Exception):
            buckets["exception"].append(name)
        elif isinstance(obj, type) and issubclass(obj, enum.Enum):
            buckets["enum"].append(name)
        elif isinstance(obj, type):
            if name != "Workspace":
                buckets["class"].append(name)
        elif typing.get_origin(obj) is not None:
            # Literal/Union/generic type aliases are callable, so they must be
            # caught before the callable branch or they masquerade as functions.
            buckets["alias"].append(name)
        elif callable(obj):
            buckets["function"].append(name)
        else:
            buckets["alias"].append(name)
    return buckets


# =============================================================================
# Not-found rendering + suggestions
# =============================================================================


def _not_found(package: Any, target: str) -> str:
    """Build a helpful "not found" string with near-matches.

    Resolution failures never raise — this string is the user-facing result.
    When the parent of a dotted path resolves, suggestions are drawn from the
    parent's members; otherwise they are drawn from the package's top-level
    public names.

    Args:
        package: The ``mixpanel_headless`` package object.
        target: The dotted path that failed to resolve.

    Returns:
        A descriptive message naming near-matches (or listing what is
        available) plus a usage hint.
    """
    parent_path, _, leaf = target.rpartition(".")
    if parent_path:
        parent = None
        try:
            parent = _resolve(package, parent_path)
        except AttributeError:
            parent = None
        if parent is not None:
            members = _public_member_summaries(parent)
            header = f"help: '{target}' not found."
            suggestion = _suggest(leaf, members, prefix=f"{parent_path}.")
            if suggestion:
                return f"{header}\n\n{suggestion}"
            available = _wrap_names([name for name, _ in members])
            return f"{header}\n\nAvailable on '{parent_path}':\n{available}"

    members = _all_public_names(package)
    header = f"help: '{target}' not found in {_PACKAGE}."
    hint = 'Call help() for the overview, or help("Workspace.query") for a method.'
    suggestion = _suggest(target, members)
    if suggestion:
        return f"{header}\n\n{suggestion}\n\n{hint}"
    return f"{header}\n\n{hint}"


def _suggest(query: str, candidates: list[tuple[str, str]], prefix: str = "") -> str:
    """Format a "Did you mean?" block from fuzzy near-matches.

    Args:
        query: The failed lookup token.
        candidates: ``(name, summary)`` pairs to match against.
        prefix: Optional string prepended to each suggested name (e.g.
            ``"Workspace."`` for dotted lookups).

    Returns:
        A formatted suggestion block, or an empty string when nothing is close
        enough (edit-distance cutoff 0.5).
    """
    names = [name for name, _ in candidates]
    matches = difflib.get_close_matches(query, names, n=5, cutoff=0.5)
    if not matches:
        return ""
    summaries = dict(candidates)
    lines = ["Did you mean?"]
    for match in matches:
        summary = summaries.get(match, "")
        label = f"{prefix}{match}"
        lines.append(f"  {label}" + (f" — {summary}" if summary else ""))
    return "\n".join(lines)


def _all_public_names(package: Any) -> list[tuple[str, str]]:
    """Collect top-level + ``Workspace``-qualified names for suggestions.

    Args:
        package: The ``mixpanel_headless`` package object.

    Returns:
        ``(name, first_docstring_line)`` pairs covering every ``__all__``
        export and every public ``Workspace`` member (as ``Workspace.<name>``).
    """
    pairs: list[tuple[str, str]] = []
    for name in _unique_exports(package):
        pairs.append((name, _first_line(getattr(package, name))))
    workspace_cls = getattr(package, "Workspace", None)
    if workspace_cls is not None:
        for name in _public_names(workspace_cls):
            member = inspect.getattr_static(workspace_cls, name, None)
            pairs.append((f"Workspace.{name}", _first_line(member)))
    return pairs


# =============================================================================
# Introspection helpers
# =============================================================================


def _unique_exports(package: Any) -> list[str]:
    """Return the package's ``__all__`` names, de-duplicated, order preserved.

    The package's ``__all__`` lists some names under more than one section
    comment; de-duplicating keeps the overview and suggestions from repeating
    them.

    Args:
        package: The ``mixpanel_headless`` package object.

    Returns:
        Each exported name once, in first-occurrence order.
    """
    return list(dict.fromkeys(package.__all__))


def _public_names(obj: Any) -> list[str]:
    """Return the non-underscore attribute names of an object.

    Args:
        obj: Any object (class or module).

    Returns:
        Sorted-by-``dir`` attribute names that do not start with ``_``.
    """
    return [name for name in dir(obj) if not name.startswith("_")]


def _public_member_summaries(obj: Any) -> list[tuple[str, str]]:
    """Return ``(name, first_docstring_line)`` for an object's public members.

    For modules, prefers the module's ``__all__`` when present (so re-exported
    third-party names such as ``Path`` are not listed); otherwise falls back to
    public ``dir`` names.

    Args:
        obj: A module or class to enumerate.

    Returns:
        ``(name, first_line)`` pairs for each public member, in declared (for
        ``__all__``) or ``dir`` order.
    """
    if inspect.ismodule(obj):
        declared = list(getattr(obj, "__all__", []))
        names = declared if declared else _public_names(obj)
    else:
        names = _public_names(obj)
    pairs: list[tuple[str, str]] = []
    for name in names:
        try:
            member = inspect.getattr_static(obj, name)
        except Exception:  # noqa: BLE001 — introspection must never crash help
            try:
                member = getattr(obj, name)
            except Exception:  # noqa: BLE001 — skip anything that won't yield
                continue
        pairs.append((name, _first_line(member)))
    return pairs


def _first_line(obj: Any) -> str:
    """Return the first line of an object's docstring (or empty string).

    Args:
        obj: Any object that may carry a docstring.

    Returns:
        The trimmed first docstring line, or ``""`` when there is no docstring.
    """
    doc = inspect.getdoc(obj)
    if not doc:
        return ""
    return doc.strip().split("\n", 1)[0].strip()


def _summary_paragraph(obj: Any) -> str:
    """Return the first paragraph of an object's docstring.

    Args:
        obj: Any object that may carry a docstring.

    Returns:
        The text before the first blank line, trimmed, or ``""`` when there is
        no docstring.
    """
    doc = inspect.getdoc(obj)
    if not doc:
        return ""
    return doc.strip().split("\n\n", 1)[0].strip()


def _format_signature(obj: Any, qualname: str) -> str:
    """Format a callable's signature, dropping ``self``/``cls`` and cleaning types.

    Args:
        obj: A callable to introspect.
        qualname: The display name to render before the parameter list.

    Returns:
        A multi-line ``name(\\n    param: T = d,\\n) -> R`` rendering, or
        ``name(...)`` if the signature cannot be introspected.
    """
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return f"{qualname}(...)"

    rendered: list[str] = []
    star_emitted = False
    for name, param in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            star_emitted = True
            rendered.append(f"*{name}{_annotation_suffix(param)}")
            continue
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            rendered.append(f"**{name}{_annotation_suffix(param)}")
            continue
        if param.kind is inspect.Parameter.KEYWORD_ONLY and not star_emitted:
            rendered.append("*")
            star_emitted = True
        rendered.append(f"{name}{_annotation_suffix(param)}{_default_suffix(param)}")

    return_type = _format_type(signature.return_annotation)
    return_suffix = f" -> {return_type}" if return_type else ""
    if not rendered:
        return f"{qualname}(){return_suffix}"
    body = ",\n    ".join(rendered)
    return f"{qualname}(\n    {body},\n){return_suffix}"


def _annotation_suffix(param: inspect.Parameter) -> str:
    """Return ``": Type"`` for an annotated parameter, else an empty string.

    Args:
        param: The parameter to inspect.

    Returns:
        The formatted annotation suffix, or ``""`` when unannotated.
    """
    annotation = _format_type(param.annotation)
    return f": {annotation}" if annotation else ""


def _default_suffix(param: inspect.Parameter) -> str:
    """Return ``" = repr"`` for a parameter with a default, else empty string.

    Args:
        param: The parameter to inspect.

    Returns:
        The formatted default suffix, or ``""`` when the parameter is required.
        Falls back to ``" = ..."`` if the default's ``repr`` raises.
    """
    if param.default is inspect.Parameter.empty:
        return ""
    try:
        return f" = {param.default!r}"
    except Exception:  # noqa: BLE001 — a hostile __repr__ must not crash help
        return " = ..."


def _format_type(annotation: Any) -> str:
    """Render a type annotation as a clean string.

    Handles both string annotations (the common case under
    ``from __future__ import annotations``) and live type objects, stripping
    ``typing.``/internal-module prefixes and ``<class '...'>`` noise.

    Args:
        annotation: A parameter/return annotation, a type, or an empty
            sentinel.

    Returns:
        A cleaned, single-line type string, or ``""`` for an empty annotation.
    """
    if annotation is inspect.Parameter.empty or annotation is inspect.Signature.empty:
        return ""
    if annotation is None or annotation is type(None):
        return "None"
    if isinstance(annotation, str):
        text = annotation
    elif isinstance(annotation, type) and not hasattr(annotation, "__args__"):
        return annotation.__name__
    else:
        text = str(annotation)
    text = text.replace("typing.", "").replace("typing_extensions.", "")
    text = text.replace(f"{_PACKAGE}._internal.", "").replace(f"{_PACKAGE}.", "")
    text = re.sub(r"<class '([^']+)'>", r"\1", text)
    text = text.replace("NoneType", "None")
    return " ".join(text.split())


def _examples(names: list[str], count: int = 6) -> str:
    """Return a short comma-joined sample of names with an ellipsis if trimmed.

    Args:
        names: The full list of names.
        count: Maximum number of names to include.

    Returns:
        ``"a, b, c"`` style sample, suffixed with ``", …"`` when truncated, or
        ``"(none)"`` when the list is empty.
    """
    if not names:
        return "(none)"
    sample = ", ".join(names[:count])
    return f"{sample}, …" if len(names) > count else sample


def _wrap_names(names: list[str], indent: str = "  ") -> str:
    """Wrap a list of names into an indented, comma-separated block.

    Args:
        names: The names to render.
        indent: The indentation applied to every wrapped line.

    Returns:
        A ``textwrap``-filled block, or ``"<indent>(none)"`` when empty.
    """
    if not names:
        return f"{indent}(none)"
    return textwrap.fill(
        ", ".join(names),
        width=88,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
