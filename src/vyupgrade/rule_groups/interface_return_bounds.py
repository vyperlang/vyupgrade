"""Interface return bounds under the covariant return check of Vyper 0.5.

Vyper 0.4 accepted an implementation whose ``String``/``Bytes``/``DynArray``
return bound was at least the interface's. Since 0.5.0a2 (vyper#4967) the bound
must be at most the interface's. Each exceeded bound becomes the wildcard ``...``
(VY125), which the implementations satisfy and which resolves at each call site
to the type the context expects. VY125 passes each call that does not expect
the old type through an internal function typed with the old bound, so callers
decode and check returns as before. Raising the
bound to the widest implementation instead (VY124) broadens callers' accepted
return values and needs aggressive mode with VY125 ignored.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..analysis import (
    INTERFACE_RE,
    SourceFacts,
    indexed_value_type,
    infer_expr_type,
    normalize_type,
    parse_source_facts,
    unwrap_type,
)
from ..models import Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    find_matching_open,
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
_IMPLEMENTS_RE = re.compile(r"^[ \t]*implements[ \t]*:", re.MULTILINE)
_MAX_IMPLEMENTS_LINES = 64
_IMPORT_RE = re.compile(
    r"^[ \t]*import[ \t]+(?P<module>[A-Za-z_][\w.]*)(?:[ \t]+as[ \t]+(?P<alias>[A-Za-z_]\w*))?",
    re.MULTILINE,
)
_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+(?P<package>[.\w]+)[ \t]+import[ \t]+(?P<names>[^#\n]+)", re.MULTILINE
)
_IMPORTED_NAME_RE = re.compile(r"(?P<name>[A-Za-z_]\w*)(?:\s+as\s+(?P<alias>[A-Za-z_]\w*))?")
_METHOD_CALL_RE = re.compile(r"\.\s*(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CALL_KEYWORDS = frozenset({"staticcall", "extcall"})
_ANNOTATED_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?P<type>.+?)\s*=")
_BOUND_FUNCTION = "__vyupgrade_return_bound"
_BOUND_FUNCTION_RE = re.compile(
    rf"^def[ \t]+(?P<name>{_BOUND_FUNCTION}\w*)\s*\(\s*\w+\s*:\s*(?P<type>.+?)\)\s*->",
    re.MULTILINE,
)
# A type name, possibly qualified by a module, and the names every module can use.
_TYPE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*")
_BUILTIN_TYPE_NAME_RE = re.compile(
    r"String|Bytes|DynArray|bool|address|decimal|bytes[0-9]*|u?int[0-9]*"
)


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


@dataclass(frozen=True)
class _Origin:
    """An interface function declaration: its file, inline interface, and name."""

    path: Path | None
    interface: str | None
    function: str


@dataclass(frozen=True)
class _Rewritten:
    """A wildcard return as one caller sees it: its old type, nameable by that caller or not."""

    old_type: str
    nameable: bool
    origin: _Origin


@dataclass(frozen=True)
class _CallSite:
    """A call whose return the wildcard would change: ``rewritten`` is None if unresolved."""

    method: str
    start: int
    end: int
    line: int
    call: str
    rewritten: _Rewritten | None


@dataclass
class _Blockers:
    """Calls anywhere in the closure that the wildcard would leave unchecked.

    An unresolved receiver may be any interface with the method, so it blocks
    every declaration of that name; an unnameable old type blocks its origin.
    """

    methods: dict[str, str]
    origins: dict[_Origin, str]

    def reason(self, origin: _Origin) -> str | None:
        return self.origins.get(origin) or self.methods.get(origin.function)


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

    def literal(self) -> str:
        """The declared type with each bound written as its integer length."""
        return self._with_bounds([str(bound.length) for bound in self.bounds])

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
    snapshot = rule_context.config.source_snapshot
    blockers = (
        _closure_blockers(snapshot, rule_context.path, source, exceeded)
        if use_wildcard and exceeded
        else None
    )
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    for exceeded_return in exceeded:
        declaration = exceeded_return.declaration
        declared = exceeded_return.declared
        line = line_number(source, declaration.start)
        if blockers is not None and (
            reason := blockers.reason(_origin(rule_context.path, declaration))
        ):
            diagnostics.append(
                Diagnostic(
                    "VYD020",
                    line,
                    f"kept interface return bound {declared} because {reason}; the wildcard "
                    "would stop checking that call's return, so give the call a typed receiver "
                    "or the old return type",
                )
            )
            continue
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
    if use_wildcard:
        call_edits, call_fixes, call_diagnostics = _bound_untyped_calls(
            rule_context, exceeded, blockers
        )
        edits.extend(call_edits)
        fixes.extend(call_fixes)
        diagnostics.extend(call_diagnostics)
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
    return {name: [returns] for name in _implemented_names(source)}


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
    """The implemented `.vyi` modules of ``consumer``, with the name it imports each as.

    An import that more than one closure member could satisfy is skipped: which
    one the compiler binds depends on its search order, not the closure's.
    """
    imports = _imported_modules(source)
    implemented: dict[Path, str] = {}
    for name in _implemented_names(source):
        if name in imports:
            candidates = snapshot.import_candidates(consumer, imports[name])
            if len(candidates) == 1:
                implemented[candidates[0]] = name
    return implemented


def _implemented_names(source: str) -> set[str]:
    """Each `implements:` target as a dotted name (`Types.IFoo`), parsed as an expression."""
    mask = code_mask(source)
    names: set[str] = set()
    for match in _IMPLEMENTS_RE.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        targets = _implements_expression(source, match.end())
        if targets is not None:
            elements = targets.elts if isinstance(targets, ast.Tuple) else [targets]
            names.update(name for element in elements if (name := _dotted_name(element)))
    return names


def _implements_expression(source: str, start: int) -> ast.expr | None:
    """Parse the expression after `implements:`, extending over continuation lines."""
    end = start
    for _ in range(_MAX_IMPLEMENTS_LINES):
        newline = source.find("\n", end)
        end = len(source) if newline < 0 else newline + 1
        try:
            return ast.parse(source[start:end].strip(), mode="eval").body
        except SyntaxError:
            if newline < 0:
                return None
    return None


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _dotted_name(node.value)):
        return f"{base}.{node.attr}"
    return None


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


def _bound_untyped_calls(
    rule_context: RuleContext,
    local_exceeded: list[_ExceededReturn],
    local_blockers: _Blockers | None,
) -> tuple[list[TextEdit], list[Fix], list[Diagnostic]]:
    """Pass calls through a function typed with the old bound unless they expect that type.

    The wildcard resolves to the type the call site expects: an unbounded length
    where there is none, and a wider bound where the site expects a wider type.
    Calls that cannot keep the old bound are diagnosed; their declaration keeps it.
    """
    source = rule_context.source
    path = rule_context.path
    snapshot = rule_context.config.source_snapshot
    constants = integer_constant_values(source, rule_context.config.source_ast)
    sites = _call_sites(source, path, snapshot, constants, local_exceeded)
    if not sites:
        return [], [], []
    blockers = local_blockers or _closure_blockers(snapshot, path, source, local_exceeded)
    taken_names = code_identifiers(source)
    bound_functions: dict[str, str] = {}
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    for site in sites:
        if site.rewritten is None:
            diagnostics.append(
                Diagnostic(
                    "VYD020",
                    site.line,
                    f"cannot resolve the receiver of {site.call}; every {site.method}() "
                    "interface return bound it may call keeps its old bound",
                )
            )
            continue
        old_type = site.rewritten.old_type
        if not site.rewritten.nameable:
            diagnostics.append(
                Diagnostic(
                    "VYD020",
                    site.line,
                    f"{site.call} does not expect its old return type {old_type}, which this "
                    "file cannot name, so the interface keeps its old bound",
                )
            )
            continue
        if blockers.reason(site.rewritten.origin) is not None:
            continue
        name = bound_functions.get(old_type)
        if name is None:
            name = _unique_name(_BOUND_FUNCTION, taken_names)
            bound_functions[old_type] = name
        edits.append(TextEdit(site.start, site.start, f"self.{name}("))
        edits.append(TextEdit(site.end, site.end, ")"))
        fixes.append(
            Fix(
                "VY125",
                site.line,
                f"kept the {old_type} return bound of a call that does not expect it",
                site.call,
                f"self.{name}({site.call})",
            )
        )
    if bound_functions:
        parameter = _unique_name("returned", taken_names)
        definitions = "".join(
            f"\n\n@internal\n@pure\ndef {name}({parameter}: {old_type}) -> {old_type}:\n"
            f"    return {parameter}\n"
            for old_type, name in bound_functions.items()
        )
        separator = "" if source.endswith("\n") else "\n"
        edits.append(TextEdit(len(source), len(source), separator + definitions))
    return edits, fixes, diagnostics


def _call_sites(
    source: str,
    path: Path | None,
    snapshot: ImportClosure | None,
    constants: dict[str, int],
    local_exceeded: list[_ExceededReturn],
) -> list[_CallSite]:
    """Calls in ``source`` that do not expect the old type of a return that exceeds its bound."""
    rewritten = _rewritten_returns(source, path, snapshot, local_exceeded)
    # A receiver that is not resolved may be any rewritten interface in the closure.
    methods = {method for _interface, method in rewritten} | _closure_rewritten_methods(snapshot)
    if not methods:
        return []
    facts = parse_source_facts(source)
    mask = code_mask(source)
    modules = _imported_modules(source)
    module_casts = {
        f"{binding}.{interface}"
        for binding, (_path, module_source) in _imported_sources(source, path, snapshot).items()
        for interface in _declared_interfaces(module_source)
    }
    # Interfaces this file names; a receiver of any other type is not resolved.
    known_interfaces = _declared_interfaces(source) | set(modules) | module_casts
    # Comments, strings and line continuations are blanked so they can sit anywhere in a call.
    code = "".join(
        char if in_code else " " for char, in_code in zip(source, mask, strict=True)
    ).replace("\\\n", " \n")
    existing_functions = {
        match.group("name"): _literal_type(match.group("type"), {})
        for match in _BOUND_FUNCTION_RE.finditer(code)
    }
    sites: list[_CallSite] = []
    for match in _METHOD_CALL_RE.finditer(code):
        method = match.group("method")
        if method not in methods or not span_is_code(mask, match.start(), match.start() + 1):
            continue
        target = _call_target(source, mask, match.start(), facts, modules, module_casts)
        method_close = find_matching(source, match.end() - 1)
        if target is None or method_close is None:
            continue
        start, target_type = target
        old = rewritten.get((target_type, method))
        if old is None and target_type in known_interfaces:
            continue
        call_start, call_end = _call_span(source, mask, start, method_close + 1)
        site = _CallSite(
            method,
            call_start,
            call_end,
            line_number(source, start),
            source[call_start:call_end],
            old,
        )
        if old is not None:
            wrapper = _wrapping_function(source, mask, call_start)
            if wrapper is not None and existing_functions.get(wrapper) == _literal_type(
                old.old_type, {}
            ):
                continue
            expected = _expected_type(source, call_start, call_end, facts)
            if expected is not None and _literal_type(expected, constants) == _literal_type(
                old.old_type, {}
            ):
                continue
        sites.append(site)
    return sites


def _closure_blockers(
    snapshot: ImportClosure | None,
    path: Path | None,
    source: str,
    local_exceeded: list[_ExceededReturn],
) -> _Blockers:
    """Calls in this file and every closure contract that a wildcard would leave unchecked."""
    own = path.resolve() if path is not None else None
    consumers: dict[Path | None, str] = {own: source}
    if snapshot is not None:
        for member, content in snapshot.contents.items():
            if member.suffix == ".vy" and member != own:
                consumers[member] = content.decode("utf-8")
    blockers = _Blockers({}, {})
    for consumer, consumer_source in consumers.items():
        exceeded = (
            local_exceeded
            if consumer == own
            else _exceeded_returns(
                consumer_source, consumer, snapshot, integer_constant_values(consumer_source)
            )
        )
        for site in _call_sites(
            consumer_source,
            consumer,
            snapshot,
            integer_constant_values(consumer_source),
            exceeded,
        ):
            where = f"{site.call} at {consumer.name if consumer else 'line'}:{site.line}"
            if site.rewritten is None:
                blockers.methods.setdefault(site.method, f"the receiver of {where} is unresolved")
            elif not site.rewritten.nameable:
                blockers.origins.setdefault(
                    site.rewritten.origin, f"{where} cannot name the old return type"
                )
    return blockers


def _origin(path: Path | None, declaration: _ReturnDeclaration) -> _Origin:
    return _Origin(
        path.resolve() if path is not None else None,
        declaration.interface,
        declaration.function,
    )


def _call_span(source: str, mask: list[bool], start: int, end: int) -> tuple[int, int]:
    """Span of the external call whose receiver starts at ``start`` and that ends at ``end``.

    It takes in the call keyword, and the parentheses between the keyword and
    the call, as in ``staticcall (I(a).f())``.
    """
    span_start, span_end = start, end
    while True:
        keyword_start, keyword = _word_before(source, mask, span_start)
        if keyword in _CALL_KEYWORDS:
            return keyword_start, span_end
        open_paren = _skip_back(source, mask, span_start) - 1
        if open_paren < 0 or source[open_paren] != "(":
            return start, end
        close_paren = find_matching(source, open_paren)
        if close_paren is None or _skip_forward(source, mask, span_end) != close_paren:
            return start, end
        span_start, span_end = open_paren, close_paren + 1


def _call_target(
    source: str,
    mask: list[bool],
    dot: int,
    facts: SourceFacts,
    modules: dict[str, str],
    module_casts: set[str],
) -> tuple[int, str] | None:
    """Start and interface type ("" if unknown) of the receiver of the method call at ``dot``.

    None for a call that is not external: an internal or module function call.
    """
    end = _skip_back(source, mask, dot)
    if source[end - 1 : end] != ")":
        start, name = _dotted_name(source, mask, end)
        if name == "self" or name in modules:
            return None
        return start, _expression_type(name, end, source, mask, facts)
    return _receiver_type(source, mask, end, facts, module_casts)


def _receiver_type(
    source: str, mask: list[bool], end: int, facts: SourceFacts, module_casts: set[str]
) -> tuple[int, str]:
    """Start and type ("" if unknown) of the expression that ends at ``end``.

    ``module_casts`` holds the ``binding.Name`` interfaces of imported modules.
    """
    if source[end - 1 : end] != ")":
        start, name = _dotted_name(source, mask, end)
        return start, _expression_type(name, end, source, mask, facts)
    receiver_open = find_matching_open(source, end - 1)
    if receiver_open is None:
        return end, ""
    start, callee = _dotted_name(source, mask, receiver_open)
    if callee in {"", *_CALL_KEYWORDS}:
        # A parenthesized expression.
        _inner_start, inner_type = _receiver_type(
            source, mask, _skip_back(source, mask, end - 1), facts, module_casts
        )
        return receiver_open, inner_type
    if callee.startswith("self."):
        return start, normalize_type(facts.function_return_names.get(callee[5:]) or "")
    if "." in callee and callee not in module_casts:
        # A function of an imported module.
        return start, ""
    # An interface cast, possibly qualified by its module.
    return start, callee


def _dotted_name(source: str, mask: list[bool], end: int) -> tuple[int, str]:
    """Start and text of the dotted, indexed name that ends at ``end``, without spacing.

    The text starts with ``.`` or ``[`` when the name is not rooted in an identifier.
    """
    parts: list[str] = []
    start = end
    while True:
        position = _skip_back(source, mask, start)
        while source[position - 1 : position] == "]":
            index_open = find_matching_open(source, position - 1, "[", "]")
            if index_open is None:
                break
            parts.append(" ".join(source[index_open:position].split()))
            position = _skip_back(source, mask, index_open)
        name_start = position
        while name_start > 0 and (source[name_start - 1].isalnum() or source[name_start - 1] == "_"):
            name_start -= 1
        if name_start == position:
            break
        parts.append(source[name_start:position])
        start = name_start
        dot = _skip_back(source, mask, start)
        if source[dot - 1 : dot] != ".":
            break
        parts.append(".")
        start = dot - 1
    return start, "".join(reversed(parts))


def _word_before(source: str, mask: list[bool], end: int) -> tuple[int, str]:
    """Start and text of the identifier right before ``end``, skipping spacing and comments."""
    word_end = _skip_back(source, mask, end)
    word_start = word_end
    while word_start > 0 and (source[word_start - 1].isalnum() or source[word_start - 1] == "_"):
        word_start -= 1
    return word_start, source[word_start:word_end]


def _skip_forward(source: str, mask: list[bool], start: int) -> int:
    """Offset of the first code character from ``start`` on that is not spacing."""
    while start < len(source) and (
        source[start].isspace()
        or not mask[start]
        or (source[start] == "\\" and source[start + 1 : start + 2] == "\n")
    ):
        start += 1
    return start


def _skip_back(source: str, mask: list[bool], end: int) -> int:
    """Offset right after the last code character before ``end`` that is not spacing.

    Comments and line continuations count as spacing.
    """
    while end > 0 and (
        source[end - 1].isspace()
        or not mask[end - 1]
        or (source[end - 1] == "\\" and source[end : end + 1] == "\n")
    ):
        end -= 1
    return end


def _wrapping_function(source: str, mask: list[bool], call_start: int) -> str | None:
    """The internal function whose sole argument starts at ``call_start``, if any."""
    open_paren = _skip_back(source, mask, call_start)
    if source[open_paren - 1 : open_paren] != "(":
        return None
    _start, callee = _dotted_name(source, mask, open_paren - 1)
    return callee[5:] if callee.startswith("self.") else None


def _expression_type(
    expression: str, offset: int, source: str, mask: list[bool], facts: SourceFacts
) -> str:
    if not expression or expression[0] in ".[":
        return ""
    if expression.startswith("self.") and expression[5:] in facts.storage_vars:
        return normalize_type(facts.storage_vars[expression[5:]]).strip("() ")
    variables = facts.vars_at_line(line_number(source, offset))
    root = re.match(r"[A-Za-z_][A-Za-z0-9_]*", expression)
    # A local is typed by its declaration in scope, not the function's last one.
    # Vyper forbids shadowing, so a name without one in scope is a parameter.
    if root is not None and root.group() != "self":
        scoped = _scoped_local_type(source, mask, offset, root.group())
        if scoped is not None:
            variables[root.group()] = scoped
    target_type = infer_expr_type(expression, variables, facts)
    return normalize_type(target_type or "").strip("() ")


def _scoped_local_type(source: str, mask: list[bool], offset: int, name: str) -> str | None:
    """Type of the declaration of ``name`` in a block enclosing ``offset``.

    Walks back through the function: a line indented deeper than the innermost
    enclosing block so far belongs to a block that has already closed.
    """
    declaration = re.compile(
        rf"(?:for[ \t]+)?{re.escape(name)}[ \t]*:[ \t]*(?P<type>.+?)[ \t]*(?:=|[ \t]in[ \t]|$)"
    )
    line_start = source.rfind("\n", 0, offset) + 1
    current = source[line_start:].split("\n", 1)[0]
    limit = len(current) - len(current.lstrip())
    for line_end in _line_starts_before(source, line_start):
        start = source.rfind("\n", 0, line_end - 1) + 1
        line = source[start : line_end - 1]
        stripped = line.strip()
        if not stripped or not line_starts_in_code(source, mask, start):
            continue
        indent = len(line) - len(line.lstrip())
        if indent > limit:
            continue
        found = declaration.match(stripped.split("#", 1)[0].rstrip())
        if found is not None:
            return found.group("type")
        if indent < limit:
            if _DEF_RE.match(line):
                # Parameters are typed once for the whole function.
                return None
            limit = indent
    return None


def _line_starts_before(source: str, offset: int) -> Iterator[int]:
    """Start offsets of the lines before the one at ``offset``, nearest first."""
    while offset > 0:
        yield offset
        offset = source.rfind("\n", 0, offset - 1) + 1


def _rewritten_returns(
    source: str,
    path: Path | None,
    snapshot: ImportClosure | None,
    local_exceeded: list[_ExceededReturn],
) -> dict[tuple[str, str], _Rewritten]:
    """Old return type, and whether this file can name it, by (interface, function).

    Covers this file's inline interfaces, whose types are in scope, and the
    interfaces of the modules it imports: a `.vyi` module's functions under its
    binding, a contract's inline interfaces under ``binding.Name``. Their types
    are written as this file names them.
    """
    rewritten = {
        (exceeded.declaration.interface, exceeded.declaration.function): _Rewritten(
            exceeded.literal(), True, _origin(path, exceeded.declaration)
        )
        for exceeded in local_exceeded
        if exceeded.declaration.interface is not None
    }
    imported = _imported_sources(source, path, snapshot)
    if snapshot is None or not imported:
        return rewritten
    bindings = {module_path: binding for binding, (module_path, _source) in imported.items()}
    for binding, (module_path, module_source) in imported.items():
        for exceeded in _exceeded_returns(
            module_source, module_path, snapshot, integer_constant_values(module_source)
        ):
            interface = exceeded.declaration.interface
            key = binding if interface is None else f"{binding}.{interface}"
            old_type, nameable = _caller_type(
                exceeded.literal(), binding, module_source, module_path, snapshot, bindings
            )
            rewritten[(key, exceeded.declaration.function)] = _Rewritten(
                old_type, nameable, _origin(module_path, exceeded.declaration)
            )
    return rewritten


def _closure_rewritten_methods(snapshot: ImportClosure | None) -> set[str]:
    """Functions of every interface in the closure whose return bound becomes the wildcard."""
    if snapshot is None:
        return set()
    methods: set[str] = set()
    for member, content in snapshot.contents.items():
        if member.suffix != ".vyi" and b"interface" not in content:
            continue
        source = content.decode("utf-8")
        methods.update(
            exceeded.declaration.function
            for exceeded in _exceeded_returns(
                source, member, snapshot, integer_constant_values(source)
            )
        )
    return methods


def _imported_sources(
    source: str, path: Path | None, snapshot: ImportClosure | None
) -> dict[str, tuple[Path, str]]:
    """Path and source of each closure module this file imports, by binding."""
    if snapshot is None or path is None or path.resolve() not in snapshot.contents:
        return {}
    return {
        binding: (module_path, snapshot.contents[module_path].decode("utf-8"))
        for binding, module in _imported_modules(source).items()
        if (module_path := snapshot.resolve_import(path, module)) is not None
    }


def _declared_interfaces(source: str) -> set[str]:
    mask = code_mask(source)
    names: set[str] = set()
    offset = 0
    for line in source.split("\n"):
        line_offset, offset = offset, offset + len(line) + 1
        header = INTERFACE_RE.match(line.split("#", 1)[0].rstrip())
        if header is not None and line_starts_in_code(source, mask, line_offset):
            names.add(header.group(1))
    return names


def _caller_type(
    type_text: str,
    binding: str,
    module_source: str,
    module_path: Path,
    snapshot: ImportClosure,
    bindings: dict[Path, str],
) -> tuple[str, bool]:
    """``type_text`` of an imported module as its importer names it, and whether it can.

    The module's own structs, flags and interfaces are qualified by ``binding``; a type it
    imports is nameable when the importer imports the same module, under its own
    binding.
    """
    module_facts = parse_source_facts(module_source)
    own_types = (
        module_facts.structs | module_facts.flags_or_enums | _declared_interfaces(module_source)
    )
    module_bindings = {
        name: snapshot.resolve_import(module_path, module)
        for name, module in _imported_modules(module_source).items()
    }
    nameable = True

    def rename(match: re.Match[str]) -> str:
        nonlocal nameable
        name = re.sub(r"\s+", "", match.group())
        qualifier, _dot, member = name.partition(".")
        if not member and _BUILTIN_TYPE_NAME_RE.fullmatch(name):
            return name
        if not member and name in own_types:
            return f"{binding}.{name}"
        imported = module_bindings.get(qualifier)
        if imported is not None and imported in bindings:
            return bindings[imported] + (f".{member}" if member else "")
        nameable = False
        return name

    return _TYPE_NAME_RE.sub(rename, type_text), nameable


def _expected_type(
    source: str, call_start: int, call_end: int, facts: SourceFacts
) -> str | None:
    """The declared type a call takes when it is a whole annotated value or return value."""
    line_start = source.rfind("\n", 0, call_start) + 1
    line_end = source.find("\n", call_end)
    rest = source[call_end : len(source) if line_end == -1 else line_end]
    if rest.split("#", 1)[0].strip():
        return None
    prefix = source[line_start:call_start].strip()
    if prefix == "return":
        return facts.return_type_at_line(line_number(source, call_start))
    annotated = _ANNOTATED_ASSIGNMENT_RE.fullmatch(prefix)
    return None if annotated is None else annotated.group("type")


def _literal_type(type_text: str, constants: dict[str, int]) -> str:
    """``type_text`` with its bounds as integer literals, without spaces."""
    _shape, bounds = _bounded_shape(type_text, constants)
    literal = apply_edits(
        type_text, [TextEdit(bound.start, bound.end, str(bound.length)) for bound in bounds]
    )
    return re.sub(r"\s+", "", literal)


def _unique_name(base: str, taken_names: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in taken_names:
        candidate = f"{base}_{suffix}"
        suffix += 1
    taken_names.add(candidate)
    return candidate


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
            crossing("VYD020", "0.5.0a2"),
        ),
    ),
)
