"""Plan bound changes once from the 0.4.3 compiler's view of the closure."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..ast_facts import CompilerSource, SourceSpan, iter_nodes
from ..constant_evaluation import eval_integer_constant_expr, integer_constant_values
from ..interfaces import split_interfaces_to_vyi
from ..models import CompilerFacts, Config, Diagnostic, Fix, RewriteResult
from ..rule_registry import Rule, crossing, is_enabled, rule_changes
from ..source import TextEdit, apply_edits, code_mask, line_number, lone_cr_as_lf
from ..versions import MigrationContext, infer_pragma, parse_version

if TYPE_CHECKING:
    from ..compiler import CompileResult, ImportClosure


_BOUND_TYPES = frozenset({"String", "Bytes", "DynArray"})
_SCALAR_TYPE_RE = re.compile(r"(?:bool|address|decimal|u?int\d+|bytes\d+)")
_EXTERNAL_CALLS = frozenset({"StaticCall", "ExtCall"})
# vyper.compiler.input_bundle.BUILTIN: the source id of every module Vyper itself ships.
_BUILTIN_SOURCE_ID = -2

RULES = (
    Rule(
        "interface_return_wildcards",
        changes=(
            crossing("VY125", "0.5.0a2"),
            crossing("VYD022", "0.5.0a2"),
            crossing("VYD023", "0.5.0a2"),
        ),
    ),
)
_CHANGES = rule_changes(RULES)

_Node = dict[str, Any]
_Origin = tuple[Path, int]


@dataclass(frozen=True)
class _Module:
    path: Path
    node: _Node
    source: CompilerSource
    constants: dict[str, int]

    @property
    def body(self) -> list[_Node]:
        return self.node["body"]

    def span(self, node: _Node) -> SourceSpan | None:
        return self.source.span(node, source_id=self.node["source_id"], byte_columns=True)

    def origin(self, node: _Node) -> _Origin | None:
        span = self.span(node)
        return (self.path, span.start) if span is not None else None


@dataclass(frozen=True)
class _Document:
    modules: dict[int, _Module]
    missing_caller: bool
    # Captured files this compilation may have loaded, mapped or not.
    scope: frozenset[Path]


@dataclass(frozen=True)
class _Type:
    name: str
    members: tuple[_Type, ...] = ()
    length: int | None = None
    bound: SourceSpan | None = field(default=None, compare=False)

    def shape(self) -> tuple:
        fixed_length = self.length if self.name == "fixed array" else None
        return self.name, fixed_length, tuple(member.shape() for member in self.members)

    def bounds(self) -> Iterator[tuple[SourceSpan, int]]:
        if self.bound is not None and self.length is not None:
            yield self.bound, self.length
        for member in self.members:
            yield from member.bounds()


@dataclass
class _Candidate:
    source: str
    annotation: tuple[int, int]
    old_type: _Type
    exceeded: set[tuple[int, int]] = field(default_factory=set)
    blocker: str | None = None


def _node(value: object) -> _Node:
    return value if isinstance(value, dict) else {}


def _members(node: _Node, key: str = "body") -> list[_Node]:
    values = node.get(key)
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _name(node: _Node) -> str | None:
    name = node.get("id")
    return name if node.get("ast_type") == "Name" and isinstance(name, str) else None


def _enabled(source_spec: str | None, config: Config) -> bool:
    version = MigrationContext.from_specs(source_spec, config.target_version)
    return version.source_can_migrate_to_target() and any(
        is_enabled(code, config, version, _CHANGES) for code in _CHANGES
    )


def _compiler_modules(
    result: CompileResult,
    path: Path,
    sources: dict[Path, CompilerSource],
    constants: dict[Path, dict[str, int]],
) -> _Document | None:
    artifacts = result.artifacts or {}
    annotated = _node(artifacts.get("annotated_ast"))
    root = _node(annotated.get("ast"))
    paths = _node(artifacts.get("annotated_ast_source_paths"))
    imports = annotated.get("imports")
    if (
        result.status != "passed" or result.resolved_compiler != "0.4.3" or not root
        or not isinstance(imports, list) or not all(isinstance(module, dict) for module in imports)
    ):
        return None
    modules: dict[int, _Module] = {}
    seen_paths: dict[int, object] = {}
    missing_caller = False
    scope: set[Path] = set()
    for module in [root, *imports]:
        resolved = module.get("resolved_path")
        source_id = module.get("source_id")
        if source_id == _BUILTIN_SOURCE_ID and module.get("is_interface") is True:
            # Vyper's own interfaces share one id and are never captured candidates.
            continue
        if type(source_id) is not int or source_id < 0:
            return None
        if source_id in seen_paths:
            # The compiler lists a module once per import request that reached it.
            if not isinstance(resolved, str) or seen_paths[source_id] != resolved:
                return None
            continue
        seen_paths[source_id] = resolved
        actual_path = paths.get(resolved) if isinstance(resolved, str) else None
        module_path = Path(actual_path) if isinstance(actual_path, str) else None
        source = sources.get(module_path) if module_path is not None else None
        if (
            module_path is None or source is None
            or not source.matches_digest(module.get("source_sha256sum"))
            or not isinstance(module.get("body"), list)
            or module.get("ast_type") != "Module"
        ):
            if module.get("is_interface") is not True:
                missing_caller = True
            digest = module.get("source_sha256sum")
            # A module matching no captured digest is not a captured file, and any
            # captured file it imports is listed here on its own.
            scope.update(
                (path for path, captured in sources.items() if captured.matches_digest(digest))
                if isinstance(digest, str) else sources
            )
            if module_path in sources:
                scope.add(module_path)
            continue
        scope.add(module_path)
        if module_path not in constants:
            constants[module_path] = integer_constant_values(source.normalized, module)
        modules[source_id] = _Module(module_path, module, source, constants[module_path])
    root_module = modules.get(root["source_id"])
    return (
        _Document(modules, missing_caller, frozenset(scope))
        if root_module is not None and root_module.path == path else None
    )


def _interface_binding(
    module: _Module, modules: dict[int, _Module], annotation: _Node,
) -> tuple[_Module, _Node] | None:
    alias = _name(annotation)
    if alias is not None:
        inline = _inline_interface(module, alias)
        if inline is not None:
            return module, inline
        target = _import_binding(module, modules, alias)
        return (target, target.node) if target is not None and target.node.get("is_interface") else None
    if annotation.get("ast_type") == "Attribute":
        alias = _name(_node(annotation.get("value")))
        name = annotation.get("attr")
        if alias is not None and isinstance(name, str):
            target = _import_binding(module, modules, alias)
            if target is not None and (inline := _inline_interface(target, name)) is not None:
                return target, inline
    return None


def _inline_interface(module: _Module, name: str) -> _Node | None:
    return next(
        (node for node in module.body if node.get("ast_type") == "InterfaceDef" and node.get("name") == name),
        None,
    )


def _import_binding(module: _Module, modules: dict[int, _Module], alias: str) -> _Module | None:
    for node in module.body:
        info = _node(node.get("import_info"))
        if info.get("alias") == alias:
            source_id = info.get("source_id")
            return modules.get(source_id) if type(source_id) is int else None
    return None


def _implementation_returns(module: _Module) -> dict[str, _Node]:
    returns: dict[str, _Node] = {}
    for node in module.body:
        if node.get("ast_type") == "FunctionDef" and any(
            _name(decorator) == "external" for decorator in _members(node, "decorator_list")
        ):
            name = node.get("name")
            annotation = _node(node.get("returns"))
        elif node.get("ast_type") == "VariableDecl" and node.get("is_public") is True:
            name = _name(_node(node.get("target")))
            annotation = _node(node.get("annotation"))
            # Scalar dynamic values, including String and Bytes, have a zero-argument getter.
            if _name(_node(annotation.get("value"))) not in {"String", "Bytes"}:
                continue
        else:
            continue
        if isinstance(name, str) and annotation:
            returns[name] = annotation
    return returns


def _return_type(module: _Module, node: _Node) -> _Type | None:
    name = _name(node)
    if name is not None:
        return _Type(name) if _SCALAR_TYPE_RE.fullmatch(name) else None
    kind = node.get("ast_type")
    if kind == "Tuple":
        members = tuple(_return_type(module, item) for item in _members(node, "elements"))
        if members and all(member is not None for member in members):
            return _Type("tuple", tuple(member for member in members if member is not None))
        return None
    if kind != "Subscript":
        return None
    base = _node(node.get("value"))
    constructor = _name(base)
    subscript = _node(node.get("slice"))
    bounds = subscript
    if constructor == "DynArray":
        parts = _members(subscript, "elements")
        if subscript.get("ast_type") != "Tuple" or len(parts) != 2:
            return None
        element, bounds = parts
        member = _return_type(module, element)
    elif constructor in {"String", "Bytes"}:
        member = None
    else:
        constructor = "fixed array"
        member = _return_type(module, base)
    if constructor not in {"String", "Bytes"} and member is None:
        return None
    span = module.span(bounds)
    if span is None:
        return None
    raw = module.source.original[span.start : span.end]
    length = eval_integer_constant_expr(raw, module.constants)
    if length is None or length < 0:
        return None
    return _Type(
        constructor,
        () if member is None else (member,),
        length,
        span if constructor in _BOUND_TYPES else None,
    )


def _collect_candidates(
    modules: dict[int, _Module],
    specs: dict[Path, str | None],
    config: Config,
    candidates: dict[_Origin, _Candidate],
    unproven: dict[_Origin, Diagnostic],
    unmapped: dict[Path, Diagnostic],
) -> None:
    for module in modules.values():
        implementations = [node for node in module.body if node.get("ast_type") == "ImplementsDecl"]
        if not implementations:
            continue
        implementation_returns = _implementation_returns(module)
        for declaration in implementations:
            target = _interface_binding(module, modules, _node(declaration.get("annotation")))
            if target is None:
                continue
            interface_module, interface = target
            path = interface_module.path
            if path not in specs or not _enabled(specs[path], config):
                continue
            for method in _members(interface):
                name = method.get("name")
                annotation = _node(method.get("returns"))
                if (
                    method.get("ast_type") != "FunctionDef"
                    or not isinstance(name, str)
                    or name not in implementation_returns
                    or not any(_name(node) in _BOUND_TYPES for node in iter_nodes(annotation, "Name"))
                ):
                    continue
                origin = interface_module.origin(method)
                annotation_span = interface_module.span(annotation)
                if origin is None or annotation_span is None:
                    row = method.get("lineno")
                    unmapped[path] = Diagnostic(
                        "VYD023",
                        row if type(row) is int and row > 0 else 1,
                        f"kept return bounds for {name}; the declaration does not map "
                        "to the captured source",
                    )
                    continue
                declared = _return_type(interface_module, annotation)
                implemented = _return_type(module, implementation_returns[name])
                if declared is None or implemented is None or declared.shape() != implemented.shape():
                    unproven[origin] = Diagnostic(
                        "VYD023",
                        line_number(interface_module.source.original, annotation_span.start),
                        f"kept return bounds for {name}; the declaration and implementation types "
                        "could not be compared",
                    )
                    continue
                exceeded = {
                    (span.start, span.end)
                    for (span, old), (_, new) in zip(declared.bounds(), implemented.bounds(), strict=True)
                    if old < new
                }
                if exceeded:
                    # Source IDs belong to one compilation. The origin already
                    # identifies the captured file, so retain its text offsets alone.
                    annotation_offsets = (annotation_span.start, annotation_span.end)
                    candidate = candidates.setdefault(
                        origin,
                        _Candidate(interface_module.source.original, annotation_offsets, declared),
                    )
                    if candidate.old_type != declared or candidate.annotation != annotation_offsets:
                        candidate.blocker = "source compilers disagreed about its original bound"
                    candidate.exceeded.update(exceeded)


def _declared_function_origins(modules: dict[int, _Module]) -> dict[tuple[int, int], _Origin]:
    origins: dict[tuple[int, int], _Origin] = {}
    for source_id, module in modules.items():
        for node in iter_nodes(module.node, "FunctionDef"):
            node_id = node.get("node_id")
            if type(node_id) is int and (origin := module.origin(node)) is not None:
                origins[(source_id, node_id)] = origin
    return origins


def _expected_return_types(module: _Module) -> dict[int, _Type]:
    types = {}
    for function in module.body:
        if function.get("ast_type") != "FunctionDef":
            continue
        for node in iter_nodes(function):
            kind = node.get("ast_type")
            call = _node(node.get("value"))
            if call.get("ast_type") not in _EXTERNAL_CALLS:
                continue
            if kind == "AnnAssign":
                expected = _node(node.get("annotation"))
            elif kind == "Return":
                expected = _node(function.get("returns"))
            else:
                continue
            typ = _return_type(module, expected)
            if typ is not None:
                types[id(call)] = typ
    return types


def _check_consumers(
    modules: dict[int, _Module],
    candidates: dict[_Origin, _Candidate],
) -> tuple[Path, int] | None:
    origins = _declared_function_origins(modules)
    unknown = None
    for module in modules.values():
        expected = _expected_return_types(module)
        for call in iter_nodes(module.node):
            if call.get("ast_type") not in _EXTERNAL_CALLS:
                continue
            function = _node(_node(call.get("value")).get("func"))
            identity = _node(_node(function.get("type")).get("type_decl_node"))
            source_id = identity.get("source_id")
            node_id = identity.get("node_id")
            if source_id == _BUILTIN_SOURCE_ID and type(node_id) is int:
                continue
            span = module.span(call)
            line = line_number(module.source.original, span.start) if span is not None else 1
            if (
                type(source_id) is not int
                or type(node_id) is not int
                or (origin := origins.get((source_id, node_id))) is None
            ):
                # The unknown declaration could be any candidate in this compilation.
                unknown = unknown or (module.path, line)
                continue
            candidate = candidates.get(origin)
            if candidate is not None and (span is None or expected.get(id(call)) != candidate.old_type):
                candidate.blocker = (
                    f"a call in {module.path.name} on line {line} does not have "
                    "an assignment or return with the original type"
                )
    return unknown


def _compiled_before_interfaces(result: CompileResult | None) -> bool:
    """Whether a full pre-0.4 compile had no imports outside the old builtin namespace."""
    if result is None or result.status != "passed":
        return False
    artifacts = result.artifacts or {}
    if (
        not isinstance(artifacts.get("bytecode"), str)
        or not isinstance(ast := artifacts.get("ast"), dict)
    ):
        return False
    version = parse_version(result.resolved_compiler)
    # 0.4.0 prereleases already resolve .vyi imports, so compare the release only.
    if version is None or version.release >= (0, 4, 0):
        return False
    return all(
        (node.get("ast_type") == "ImportFrom" and node.get("module") == "vyper.interfaces"
         and node.get("level") == 0)
        for node in iter_nodes(ast)
        if node.get("ast_type") in {"Import", "ImportFrom"}
    )


def _legacy_migration_is_unrelated(
    path: Path, source: str, spec: str | None, result: CompileResult, config: Config,
) -> bool:
    if not _compiled_before_interfaces(result):
        return False

    # Imported builtin names have a reserved namespace after VY020 runs. If
    # the old import survives, the target can instead resolve it to a local file.
    # `math`, which VY101 imports for `isqrt`, is also resolved as a builtin
    # before any search path, so it can't bind a captured interface either.
    from ..rules import RULE_CHANGES, apply_rules

    file_config = replace(config, source_version=spec, source_ast=None)
    candidate = apply_rules(
        source, file_config, path, compiler_facts=CompilerFacts.from_artifacts(result.artifacts),
    ).source
    context = MigrationContext.from_specs(spec or infer_pragma(source), config.target_version)
    if config.split_interfaces and is_enabled("VY120", config, context, RULE_CHANGES):
        candidate = split_interfaces_to_vyi(candidate, path).source
    candidate = lone_cr_as_lf(candidate)
    code = "".join(char if live else " " for char, live in zip(candidate, code_mask(candidate), strict=True))
    remaining = re.sub(r"^[ \t]*from[ \t]+ethereum\.ercs[ \t]+import\b", "", code, flags=re.MULTILINE)
    remaining = re.sub(
        r"^[ \t]*import[ \t]+math(?:[ \t]+as[ \t]+\w+)?[ \t]*$", "", remaining, flags=re.MULTILINE,
    )
    return re.search(r"\bimport\b", remaining) is None


def plan_interface_wildcards(
    snapshot: ImportClosure | None,
    inputs: list[tuple[Path, str, str | None, CompileResult]],
    config: Config,
) -> dict[Path, RewriteResult]:
    """Build exact-context wildcard edits before any per-file rules change the source."""
    if snapshot is None or not any(
        _enabled(spec or infer_pragma(source), config) for _, source, spec, _ in inputs
    ):
        return {}
    sources = {
        path: CompilerSource(content.decode("utf-8"))
        for path, content in snapshot.contents.items()
        if path.suffix in {".vy", ".vyi"}
    }
    specs = {
        path.resolve(): spec or infer_pragma(source)
        for path, source, spec, _ in inputs
        if path.resolve() in sources and sources[path.resolve()].original == source
    }
    constants: dict[Path, dict[str, int]] = {}
    documents = {
        path.resolve(): document
        for path, _, _, result in inputs
        if (document := _compiler_modules(result, path.resolve(), sources, constants)) is not None
    }
    candidates: dict[_Origin, _Candidate] = {}
    unproven: dict[_Origin, Diagnostic] = {}
    unmapped: dict[Path, Diagnostic] = {}
    verified: set[Path] = set()
    for document in documents.values():
        verified.update(module.path for module in document.modules.values())
        _collect_candidates(document.modules, specs, config, candidates, unproven, unmapped)
    missing = sorted(
        path for path in (sources.keys() - verified) | (set(snapshot.roots) - documents.keys())
        if path.suffix == ".vy"
    )
    input_by_path = {path.resolve(): (source, spec, result) for path, source, spec, result in inputs}
    declaration_paths = {path for path, _ in candidates}
    unproven_file = next(
        (path for path in missing if path in declaration_paths or path not in specs
         or not _legacy_migration_is_unrelated(path, *input_by_path[path], config)),
        None,
    )
    # Each compilation's own blockers reach only the captured files it loaded.
    unverifiable_imports = [
        (path, document.scope) for path, document in sorted(documents.items()) if document.missing_caller
    ]
    unknown_calls = [
        (call, document.scope) for _, document in sorted(documents.items())
        if (call := _check_consumers(document.modules, candidates)) is not None
    ]
    edits: dict[Path, list[TextEdit]] = defaultdict(list)
    fixes: dict[Path, list[Fix]] = defaultdict(list)
    diagnostics: dict[Path, list[Diagnostic]] = defaultdict(list)
    for origin, diagnostic in unproven.items():
        diagnostics[origin[0]].append(diagnostic)
    for path, diagnostic in unmapped.items():
        diagnostics[path].append(diagnostic)
    for origin, candidate in candidates.items():
        path = origin[0]
        if origin in unproven or path in unmapped:
            continue
        blocker = candidate.blocker
        importer = next((file for file, scope in unverifiable_imports if path in scope), None)
        unknown = next((call for call, scope in unknown_calls if path in scope), None)
        if unproven_file is not None:
            blocker = f"no matching compiler evidence is available for {unproven_file.name}"
        elif importer is not None:
            blocker = f"a module imported by {importer.name} lacks verified caller evidence"
        elif unknown is not None:
            caller, line = unknown
            blocker = f"the compiler declaration of a call in {caller.name} on line {line} is unresolved"
        start, end = candidate.annotation
        before = candidate.source[start:end]
        line = line_number(candidate.source, start)
        if not all(start <= bound_start < bound_end <= end for bound_start, bound_end in candidate.exceeded):
            blocker = "an exceeded bound does not map to the captured return annotation"
        if blocker is not None:
            diagnostics[path].append(Diagnostic(
                "VYD022", line, f"kept interface return bound {before} because {blocker}",
            ))
            continue
        after = apply_edits(before, [
            TextEdit(bound_start - start, bound_end - start, "...")
            for bound_start, bound_end in sorted(candidate.exceeded)
        ])
        if before != after:
            edits[path].append(TextEdit(start, end, after))
            fixes[path].append(Fix(
                "VY125", line, "replaced exceeded interface return bound with the wildcard", before, after,
            ))
    return {
        path: RewriteResult(apply_edits(sources[path].original, edits[path]), fixes[path], diagnostics[path])
        for path in edits.keys() | diagnostics.keys()
    }
