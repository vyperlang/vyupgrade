"""Interface return bounds under the covariant return check of Vyper 0.5.

Vyper 0.4 accepted an implementation whose ``String``/``Bytes``/``DynArray``
return bound was at least the interface's. Since 0.5.0a2 (vyper#4967) the bound
must be at most the interface's. Each exceeded bound becomes the wildcard ``...``
(VY125), which the implementations satisfy and which resolves at each call site
to the type the context expects. Only interfaces that an implementing contract
in the closure exceeds change; interfaces a contract only calls keep their
bounds. Raising the bound to the widest implementation instead (VY124) broadens
callers' accepted return values and needs aggressive mode with VY125 ignored.
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
from .numeric_constant_helpers import integer_constant_values

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


# Type tokens with length bounds blanked, and the bound lengths in order.
_Shape = tuple[tuple[str | None, ...], list[int]]


@dataclass(frozen=True)
class _ExceededReturn:
    declaration: _ReturnDeclaration
    declared: str
    bounds: list[_Bound]
    widest: list[int]

    def widened(self) -> str:
        return self._with_bounds(
            [str(widest) if widest > bound.length else None for bound, widest in self._pairs()]
        )

    def wildcard(self) -> str:
        return self._with_bounds(
            ["..." if widest > bound.length else None for bound, widest in self._pairs()]
        )

    def _pairs(self) -> zip[tuple[_Bound, int]]:
        return zip(self.bounds, self.widest, strict=True)

    def _with_bounds(self, replacements: list[str | None]) -> str:
        return apply_edits(
            self.declared,
            [
                TextEdit(bound.start, bound.end, replacement)
                for bound, replacement in zip(self.bounds, replacements, strict=True)
                if replacement is not None
            ],
        )


def _interface_return_bounds(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    exceeded = _exceeded_returns(
        source,
        rule_context.path,
        rule_context.config.source_snapshot,
        integer_constant_values(source, rule_context.config.source_ast),
    )
    use_wildcard = rule_context.is_enabled("VY125")
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    for exceeded_return in exceeded:
        declaration = exceeded_return.declaration
        declared = exceeded_return.declared
        line = line_number(source, declaration.start)
        if use_wildcard:
            wildcard = exceeded_return.wildcard()
            edits.append(TextEdit(declaration.start, declaration.end, wildcard))
            fixes.append(
                Fix(
                    "VY125",
                    line,
                    "replaced exceeded interface return bound with the wildcard",
                    declared,
                    wildcard,
                )
            )
            continue
        widened = exceeded_return.widened()
        diagnostics.append(
            Diagnostic(
                "VYD019",
                line,
                f"interface return bound {declared} needs widening to {widened}; "
                "this broadens accepted external return values; review callers "
                "and use --aggressive to apply VY124",
            )
        )
        if not rule_context.config.aggressive or not rule_context.is_enabled("VY124"):
            continue
        edits.append(TextEdit(declaration.start, declaration.end, widened))
        fixes.append(
            Fix(
                "VY124",
                line,
                "widened interface return bound to the widest implementation",
                declared,
                widened,
            )
        )
    return apply_edits(source, edits), fixes, diagnostics


def _exceeded_returns(
    source: str,
    path: Path | None,
    snapshot: ImportClosure | None,
    constants: dict[str, int],
) -> list[_ExceededReturn]:
    """Return declarations of ``source`` whose bounds an implementation exceeds."""
    declarations = _return_declarations(source, path)
    if not declarations:
        return []
    implementations = _implementation_returns_by_interface(source, path, snapshot, constants)
    exceeded: list[_ExceededReturn] = []
    for declaration in declarations:
        declared = source[declaration.start : declaration.end]
        shape, bounds = _bounded_shape(declared, constants)
        widest = [bound.length for bound in bounds]
        for returns in implementations.get(declaration.interface, ()):
            implemented = returns.get(declaration.function)
            if implemented is not None and implemented[0] == shape:
                widest = [max(pair) for pair in zip(widest, implemented[1], strict=True)]
        if any(length > bound.length for bound, length in zip(bounds, widest, strict=True)):
            exceeded.append(_ExceededReturn(declaration, declared, bounds, widest))
    return exceeded


def _return_declarations(source: str, path: Path | None) -> list[_ReturnDeclaration]:
    declarations: list[_ReturnDeclaration] = []
    for interface, function, open_index in _interface_functions(source, path):
        close_index = find_matching(source, open_index)
        annotation = None if close_index is None else _RETURN_RE.match(source, close_index + 1)
        if annotation is not None:
            declarations.append(
                _ReturnDeclaration(
                    interface, function, annotation.start("type"), annotation.end("type")
                )
            )
    return declarations


def _interface_functions(source: str, path: Path | None) -> Iterator[tuple[str | None, str, int]]:
    """Yield interface, name, and parameter-list offset of each declared interface function.

    A `.vyi` module declares its functions at top level; a contract declares them
    in inline `interface` blocks.
    """
    mask = code_mask(source)
    module_is_interface = _is_interface_module(path)
    interface: str | None = None
    offset = 0
    for line in source.split("\n"):
        line_offset, offset = offset, offset + len(line) + 1
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or not line_starts_in_code(source, mask, line_offset)
        ):
            continue
        top_level = not line[0].isspace()
        if top_level:
            header = INTERFACE_RE.match(stripped.split("#", 1)[0].strip())
            interface = header.group(1) if header else None
        def_match = _DEF_RE.match(line)
        if def_match is None:
            continue
        open_index = line_offset + def_match.end() - 1
        if module_is_interface and top_level:
            yield None, def_match.group("name"), open_index
        elif not module_is_interface and interface is not None and not top_level:
            yield interface, def_match.group("name"), open_index


def _implementation_returns_by_interface(
    source: str,
    path: Path | None,
    snapshot: ImportClosure | None,
    constants: dict[str, int],
) -> dict[str | None, list[dict[str, _Shape]]]:
    if _is_interface_module(path):
        return {None: list(_module_implementation_returns(path, snapshot))}
    returns = _implementation_returns(parse_source_facts(source), constants)
    return {name: [returns] for name in implemented_interface_names(source)}


def _module_implementation_returns(
    path: Path | None, snapshot: ImportClosure | None
) -> Iterator[dict[str, _Shape]]:
    """Return shapes of each closure contract that implements this `.vyi` module."""
    if snapshot is None or path is None:
        return
    interface = path.resolve()
    for member, content in snapshot.contents.items():
        if member.suffix != ".vy" or b"implements" not in content:
            continue
        source = content.decode("utf-8")
        binding = _implemented_modules(snapshot, member, source).get(interface)
        if binding is not None:
            yield _implementation_returns(
                parse_source_facts(source), integer_constant_values(source), binding
            )


def _implemented_modules(snapshot: ImportClosure, consumer: Path, source: str) -> dict[Path, str]:
    """The implemented `.vyi` modules of ``consumer``, with the name it imports each as."""
    imports = _imported_modules(source)
    return {
        path: name
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


def _implementation_returns(
    facts: SourceFacts, constants: dict[str, int], interface_binding: str | None = None
) -> dict[str, _Shape]:
    """Return shapes of external functions and public getters, by name.

    Types qualified by ``interface_binding``, the implemented `.vyi` module's
    own, are compared as the module writes them: unqualified.
    """
    returns = {
        facts.function_names[line]: return_type
        for line, return_type in facts.function_returns.items()
        if "external" in facts.function_decorators.get(line, ())
    }
    for name, declared in facts.global_vars.items():
        getter = _public_getter_return_type(declared)
        if getter is not None:
            returns.setdefault(name, getter)
    shapes: dict[str, _Shape] = {}
    for name, declared in returns.items():
        return_type = (
            declared
            if interface_binding is None
            else re.sub(rf"(?<![\w.]){re.escape(interface_binding)}\s*\.\s*", "", declared)
        )
        shape, bounds = _bounded_shape(return_type, constants)
        shapes[name] = (shape, [bound.length for bound in bounds])
    return shapes


def _public_getter_return_type(declared: str) -> str | None:
    match = re.fullmatch(r"public\((.+)\)", declared.strip())
    if match is None:
        return None
    getter_type = unwrap_type(match.group(1))
    # `public(reentrant(T))` declares a getter that skips the reentrancy lock.
    if (reentrant := re.fullmatch(r"reentrant\((.+)\)", getter_type)) is not None:
        getter_type = reentrant.group(1).strip()
    while (value_type := indexed_value_type(getter_type)) is not None:
        getter_type = value_type
    return getter_type


def _bounded_shape(
    type_text: str, constants: dict[str, int]
) -> tuple[tuple[str | None, ...], list[_Bound]]:
    """Split a type into its tokens with length bounds blanked, and those bounds.

    A bound is an integer literal or a name in ``constants``.
    """
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
        elif (length := _length_bound(tokens, index, open_groups, constants)) is not None:
            bounds.append(_Bound(token.start(), token.end(), length))
            shape.append(None)
            continue
        shape.append(text)
    return tuple(shape), bounds


def _length_bound(
    tokens: list[re.Match[str]],
    index: int,
    open_groups: list[tuple[str, int]],
    constants: dict[str, int],
) -> int | None:
    text = tokens[index].group()
    length = int(text) if text.isdigit() else constants.get(text)
    if not open_groups or length is None:
        return None
    constructor, argument = open_groups[-1]
    whole_argument = tokens[index - 1].group() in {"[", ","} and (
        index + 1 < len(tokens) and tokens[index + 1].group() in {"]", ","}
    )
    return length if whole_argument and _BOUND_ARGUMENT.get(constructor) == argument else None


def _is_interface_module(path: Path | None) -> bool:
    return path is not None and path.suffix == ".vyi"


RULES = (
    Rule(
        "interface_return_bounds",
        runner=_interface_return_bounds,
        changes=(
            crossing("VY124", "0.5.0a2"),
            crossing("VY125", "0.5.0a2"),
            crossing("VYD019", "0.5.0a2"),
        ),
    ),
)
