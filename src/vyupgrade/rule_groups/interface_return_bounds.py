"""Widen interface return bounds for the covariant return check of Vyper 0.5.

Vyper 0.4 accepted an implementation whose ``String``/``Bytes``/``DynArray``
return bound was at least the interface's. Since 0.5.0a2 (vyper#4967) the bound
must be at most the interface's. Raising each declared bound to the widest
implementing bound satisfies the new check and leaves implementations untouched.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..analysis import (
    INTERFACE_RE,
    SourceFacts,
    indexed_value_type,
    parse_source_facts,
    unwrap_type,
)
from ..models import Diagnostic, Fix
from ..rule_helpers import implemented_interface_names
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    find_matching,
    line_number,
    line_starts_in_code,
    span_is_code,
)

if TYPE_CHECKING:
    from ..compiler import ImportClosure


# Position of the length argument of each bounded type constructor.
_BOUND_ARGUMENT = {"String": 0, "Bytes": 0, "DynArray": 1}
_TYPE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|\S")
_DEF_RE = re.compile(r"[ \t]*def[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\(")
_RETURN_RE = re.compile(r"[ \t]*->[ \t]*(?P<type>[^:#\n]*[^:#\s])[ \t]*:")
_IMPORT_RE = re.compile(
    r"^[ \t]*import[ \t]+(?P<module>[A-Za-z_][\w.]*)(?:[ \t]+as[ \t]+(?P<alias>[A-Za-z_]\w*))?",
    re.MULTILINE,
)
_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+(?P<package>[.\w]+)[ \t]+import[ \t]+(?P<names>[^#\n]+)", re.MULTILINE
)
_IMPORTED_NAME_RE = re.compile(r"(?P<name>[A-Za-z_]\w*)(?:\s+as\s+(?P<alias>[A-Za-z_]\w*))?")


@dataclass(frozen=True)
class _ReturnDeclaration:
    # Inline interface name, or None for the functions of a `.vyi` module.
    interface: str | None
    function: str
    start: int
    end: int


@dataclass(frozen=True)
class _Bound:
    start: int
    end: int
    length: int


def _widen_interface_return_bounds(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    declarations = _return_declarations(rule_context)
    if not declarations:
        return source, [], []
    implementations = _implementation_returns_by_interface(rule_context)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for declaration in declarations:
        declared = source[declaration.start : declaration.end]
        implemented = [
            returns[declaration.function]
            for returns in implementations.get(declaration.interface, ())
            if declaration.function in returns
        ]
        widened = _widest_type(declared, implemented)
        if widened == declared:
            continue
        edits.append(TextEdit(declaration.start, declaration.end, widened))
        fixes.append(
            Fix(
                "VY124",
                line_number(source, declaration.start),
                "widened interface return bound to the widest implementation",
                declared,
                widened,
            )
        )
    return apply_edits(source, edits), fixes, []


def _return_declarations(rule_context: RuleContext) -> list[_ReturnDeclaration]:
    source = rule_context.source
    declarations: list[_ReturnDeclaration] = []
    for interface, function, open_index in _interface_functions(rule_context):
        close_index = find_matching(source, open_index)
        annotation = None if close_index is None else _RETURN_RE.match(source, close_index + 1)
        if annotation is not None:
            declarations.append(
                _ReturnDeclaration(
                    interface, function, annotation.start("type"), annotation.end("type")
                )
            )
    return declarations


def _interface_functions(rule_context: RuleContext) -> Iterator[tuple[str | None, str, int]]:
    """Yield interface, name, and parameter-list offset of each declared interface function.

    A `.vyi` module declares its functions at top level; a contract declares them
    in inline `interface` blocks.
    """
    source = rule_context.source
    module_is_interface = _is_interface_module(rule_context.path)
    interface: str | None = None
    for offset, line in zip(rule_context.line_offsets, source.split("\n"), strict=True):
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or not line_starts_in_code(source, rule_context.code_mask, offset)
        ):
            continue
        top_level = not line[0].isspace()
        if top_level:
            header = INTERFACE_RE.match(stripped.split("#", 1)[0].strip())
            interface = header.group(1) if header else None
        def_match = _DEF_RE.match(line)
        if def_match is None:
            continue
        open_index = offset + def_match.end() - 1
        if module_is_interface and top_level:
            yield None, def_match.group("name"), open_index
        elif not module_is_interface and interface is not None and not top_level:
            yield interface, def_match.group("name"), open_index


def _implementation_returns_by_interface(
    rule_context: RuleContext,
) -> dict[str | None, list[dict[str, str]]]:
    if _is_interface_module(rule_context.path):
        return {None: list(_module_implementation_returns(rule_context))}
    returns = _implementation_returns(rule_context.facts)
    return {name: [returns] for name in implemented_interface_names(rule_context.source)}


def _module_implementation_returns(rule_context: RuleContext) -> Iterator[dict[str, str]]:
    """Return types of each closure contract that implements this `.vyi` module."""
    snapshot = rule_context.config.source_snapshot
    if snapshot is None or rule_context.path is None:
        return
    interface = rule_context.path.resolve()
    for member, content in snapshot.contents.items():
        if member.suffix != ".vy" or b"implements" not in content:
            continue
        source = content.decode("utf-8")
        if interface in _implemented_modules(snapshot, member, source):
            yield _implementation_returns(parse_source_facts(source))


def _implemented_modules(snapshot: ImportClosure, consumer: Path, source: str) -> set[Path]:
    imports = _imported_modules(source)
    return {
        path
        for name in implemented_interface_names(source)
        if name in imports and (path := snapshot.resolve_import(consumer, imports[name]))
    }


def _imported_modules(source: str) -> dict[str, str]:
    """Map each imported binding to its dotted module; leading dots mark relative imports."""
    mask = code_mask(source)
    modules: dict[str, str] = {}
    for match in _IMPORT_RE.finditer(source):
        if span_is_code(mask, match.start("module"), match.end("module")):
            modules[match.group("alias") or match.group("module")] = match.group("module")
    for match in _FROM_IMPORT_RE.finditer(source):
        if not span_is_code(mask, match.start("package"), match.end("package")):
            continue
        package = match.group("package")
        prefix = package if package.endswith(".") else f"{package}."
        for name in _IMPORTED_NAME_RE.finditer(match.group("names").strip().strip("()")):
            modules[name.group("alias") or name.group("name")] = prefix + name.group("name")
    return modules


def _implementation_returns(facts: SourceFacts) -> dict[str, str]:
    """Return types of external functions and public getters, by name."""
    returns = {
        facts.function_names[line]: return_type
        for line, return_type in facts.function_returns.items()
        if "external" in facts.function_decorators.get(line, ())
    }
    for name, declared in facts.global_vars.items():
        getter = _public_getter_return_type(declared)
        if getter is not None:
            returns.setdefault(name, getter)
    return returns


def _public_getter_return_type(declared: str) -> str | None:
    match = re.fullmatch(r"public\((.+)\)", declared.strip())
    if match is None:
        return None
    getter_type = unwrap_type(match.group(1))
    while (value_type := indexed_value_type(getter_type)) is not None:
        getter_type = value_type
    return getter_type


def _widest_type(declared: str, implemented: list[str]) -> str:
    """``declared`` with each length bound raised to the widest same-shaped implementation."""
    shape, bounds = _bounded_shape(declared)
    lengths = [bound.length for bound in bounds]
    for return_type in implemented:
        implemented_shape, implemented_bounds = _bounded_shape(return_type)
        if implemented_shape == shape:
            lengths = [
                max(length, bound.length)
                for length, bound in zip(lengths, implemented_bounds, strict=True)
            ]
    return apply_edits(
        declared,
        [
            TextEdit(bound.start, bound.end, str(length))
            for bound, length in zip(bounds, lengths, strict=True)
            if length > bound.length
        ],
    )


def _bounded_shape(type_text: str) -> tuple[tuple[str | None, ...], list[_Bound]]:
    """Split a type into its tokens with literal length bounds blanked, and those bounds."""
    tokens = list(_TYPE_TOKEN_RE.finditer(type_text))
    shape: list[str | None] = []
    bounds: list[_Bound] = []
    # (constructor, argument index) of each open bracket or parenthesis.
    open_groups: list[tuple[str, int]] = []
    for index, token in enumerate(tokens):
        text = token.group()
        if text in {"[", "("}:
            constructor = tokens[index - 1].group() if text == "[" and index > 0 else ""
            open_groups.append((constructor, 0))
        elif text in {"]", ")"} and open_groups:
            open_groups.pop()
        elif text == "," and open_groups:
            constructor, argument = open_groups[-1]
            open_groups[-1] = (constructor, argument + 1)
        elif _is_length_bound(tokens, index, open_groups):
            bounds.append(_Bound(token.start(), token.end(), int(text)))
            shape.append(None)
            continue
        shape.append(text)
    return tuple(shape), bounds


def _is_length_bound(
    tokens: list[re.Match[str]], index: int, open_groups: list[tuple[str, int]]
) -> bool:
    if not open_groups or not tokens[index].group().isdigit():
        return False
    constructor, argument = open_groups[-1]
    whole_argument = tokens[index - 1].group() in {"[", ","} and (
        index + 1 < len(tokens) and tokens[index + 1].group() in {"]", ","}
    )
    return whole_argument and _BOUND_ARGUMENT.get(constructor) == argument


def _is_interface_module(path: Path | None) -> bool:
    return path is not None and path.suffix == ".vyi"


RULES = (
    Rule(
        "interface_return_bounds",
        runner=_widen_interface_return_bounds,
        changes=(crossing("VY124", "0.5.0a2"),),
    ),
)
