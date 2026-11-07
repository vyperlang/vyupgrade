"""Plan external call edits from the source compiler's per-use evidence."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import re
from dataclasses import dataclass, field
from tokenize import NAME, TokenInfo
from typing import Any

from ..analysis import SourceFacts, parse_source_facts
from ..ast_facts import CompilerSource, iter_nodes, node_span
from ..models import Fix
from ..rule_registry import RuleContext
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    line_offsets,
    logical_statement_tokens,
    lone_cr_as_lf,
)
from .legacy_interfaces import IMPORT_RENAMES


@dataclass
class _Tokens:
    source: str
    lines: list[int] = field(init=False)
    functions: dict[str, list[list[TokenInfo]]] = field(default_factory=dict)
    imports: list[tuple[str, ...]] = field(default_factory=list)

    def __post_init__(self) -> None:
        text = lone_cr_as_lf(self.source)
        self.lines = line_offsets(text)
        function = None
        for statement in logical_statement_tokens(text):
            first = statement[0]
            if first.start[1] == 0:
                function = None
                if first.string == "def" and len(statement) > 1:
                    name = statement[1].string
                    if name in self.functions:
                        self.functions[name] = []
                    else:
                        function = []
                        self.functions[name] = function
                elif first.string in {"from", "import"}:
                    self.imports.append(tuple(t.string for t in statement))
            if function is not None:
                function.append(statement)

    def offset(self, position: tuple[int, int]) -> int:
        row, column = position
        return self.lines[row - 1] + column


def _align(
    original: _Tokens,
    current: _Tokens,
    name: str,
    renames: set[tuple[str, str]],
    statements: set[tuple[str, str]],
    original_names: set[int],
    changed_bindings: set[str],
) -> tuple[dict[int, int], dict[int, int]] | None:
    """Match the entire function, including its signature, statement order and scopes."""
    old = original.functions.get(name)
    new = current.functions.get(name)
    if not old or not new or len(old) != len(new):
        return None
    starts, ends = {}, {}
    for left, right in zip(old, new, strict=True):
        if left[0].line[: left[0].start[1]] != right[0].line[: right[0].start[1]]:
            return None
        if any(token.type == NAME and token.string in changed_bindings for token in left):
            return None
        if len(left) == len(right) and all(
            a.string == b.string
            or (
                a.type == b.type == NAME
                and original.offset(a.start) in original_names
                and (a.string, b.string) in renames
            )
            for a, b in zip(left, right, strict=True)
        ):
            for a, b in zip(left, right, strict=True):
                starts[original.offset(a.start)] = current.offset(b.start)
                ends[original.offset(a.end)] = current.offset(b.end)
            continue
        # This exact statement was replaced earlier. Its interior has no
        # provenance; later sites in the same function can still use their spans.
        before = original.source[original.offset(left[0].start) : original.offset(left[-1].end)]
        after = current.source[current.offset(right[0].start) : current.offset(right[-1].end)]
        if (before, after) not in statements:
            return None
    return starts, ends


@dataclass(frozen=True)
class _Site:
    src: str
    method: str
    interface: str | None = None
    mode: str | None = None
    returns: str | None = None


def _sites(rows: object, *, members: bool = False) -> dict[int, _Site] | None:
    if not isinstance(rows, list):
        return None
    sites = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        node_id, method = row.get("node_id"), row.get("method")
        if (
            type(node_id) is not int
            or node_id < 0
            or node_id in sites
            or node_span(row) is None
            or not isinstance(method, str)
            or not method.isidentifier()
        ):
            return None
        if members:
            if method not in {"append", "pop"}:
                return None
            sites[node_id] = _Site(row["src"], method)
        else:
            interface, mode, returns = (
                row.get("interface"),
                row.get("mutability"),
                row.get("return_type"),
            )
            if (
                not isinstance(interface, str)
                or not interface.isidentifier()
                or mode not in ("view", "pure", "nonpayable", "payable")
                or (
                    returns is not None
                    and (
                        not isinstance(returns, str) or not re.fullmatch(r"[\w.,()\[\] ]+", returns)
                    )
                )
            ):
                return None
            sites[node_id] = _Site(row["src"], method, interface, mode, returns)
    return sites


@dataclass(frozen=True)
class _Program:
    source: CompilerSource
    source_id: int
    current: _Tokens
    external: dict[int, _Site]
    members: dict[int, _Site]
    local_interfaces: set[str]
    aliases: dict[str, str]


def proven_call_keywords(context: RuleContext) -> tuple[str, list[Fix], set[str]] | None:
    facts = context.compiler_facts
    evidence = facts.external_calls
    module = (facts.ast or {}).get("ast")
    if (
        not isinstance(evidence, dict)
        or not isinstance(module, dict)
        or context.original_source is None
    ):
        return None
    source = CompilerSource(context.original_source)
    if not source.matches_digest(evidence.get("source_sha256sum")):
        return None
    external = _sites(evidence.get("calls"))
    members = _sites(evidence.get("member_calls", []), members=True)
    module_span = node_span(module)
    if (
        external is None
        or members is None
        or not (external or members)
        or external.keys() & members.keys()
        or module_span is None
        or not isinstance(module.get("body"), list)
        or not all(isinstance(n, dict) for n in module["body"])
    ):
        return None
    original, current = _Tokens(context.original_source), _Tokens(context.source)
    if not _same_import_bindings(original, current, context.prior_fixes):
        return None
    before = parse_source_facts(context.original_source)
    module_body = module["body"]
    renames = _identifier_renames(context, module_body, before)
    aliases = dict(renames)
    changed_bindings = _changed_bindings(before, context.facts, aliases)
    original_names = {
        span.start
        for n in iter_nodes(module, "Name")
        if isinstance(n.get("id"), str)
        and n["id"] in aliases
        and (span := source.span(n, source_id=module_span.source_id))
    }
    statements = {
        (fix.before.strip(), fix.after.strip())
        for fix in context.prior_fixes
        if fix.rule in {"VY070", "VY112"}
    }
    local_interfaces = {
        n["name"]
        for n in module_body
        if n.get("ast_type") == "InterfaceDef" and isinstance(n.get("name"), str)
    }
    program = _Program(
        source, module_span.source_id, current, external, members, local_interfaces, aliases
    )
    taken = code_identifiers(context.source)
    edits, fixes, owned = [], [], set()
    local_vars = {
        name: before.function_vars.get(row, {}) for row, name in before.function_names.items()
    }
    for function in module_body:
        if function.get("ast_type") != "FunctionDef":
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name.isidentifier():
            return None
        local = local_vars.get(name, {})
        safe_renames = {(a, b) for a, b in renames if a not in local and b not in local}
        aligned = _align(
            original, current, name, safe_renames, statements, original_names, changed_bindings
        )
        if aligned is None:
            continue
        next_taken = taken.copy()
        planned = _function_edits(context, function, aligned, program, next_taken)
        if planned is not None:
            local_edits, local_fixes = planned
            edits.extend(local_edits)
            fixes.extend(local_fixes)
            owned.add(name)
            taken = next_taken
    return (apply_edits(context.source, edits), fixes, owned) if owned else None


def _same_import_bindings(original: _Tokens, current: _Tokens, fixes: tuple[Fix, ...]) -> bool:
    def tokens(text: str) -> tuple[str, ...]:
        return tuple(t.string for part in logical_statement_tokens(text) for t in part)

    import_updates = {
        (tokens(f.before), tokens(f.after))
        for f in fixes
        if f.rule == "VY020" and f.before.startswith("from ")
    }
    return len(original.imports) == len(current.imports) and all(
        old == new or (old, new) in import_updates
        for old, new in zip(original.imports, current.imports, strict=True)
    )


def _identifier_renames(
    context: RuleContext, module_body: list[dict], before: SourceFacts
) -> set[tuple[str, str]]:
    source_names = code_identifiers(context.original_source)
    immutables = {
        n["target"]["id"]
        for n in module_body
        if n.get("ast_type") == "VariableDecl"
        and n.get("is_immutable")
        and isinstance(n.get("target"), dict)
        and isinstance(n["target"].get("id"), str)
    }
    builtin_imports = {
        n.get("name")
        for n in module_body
        if n.get("ast_type") == "ImportFrom"
        and n.get("module") == "vyper.interfaces"
        and not n.get("alias")
        and isinstance(n.get("name"), str)
    }
    renames = set()
    for fix in context.prior_fixes:
        a, b = fix.before, fix.after
        if a not in source_names or b in source_names:
            continue
        if fix.rule == "VY020" and a in builtin_imports and IMPORT_RENAMES.get(a) == b:
            renames.add((a, b))
        if (
            fix.rule == "VY013"
            and a in immutables
            and b.isidentifier()
            and (
                before.global_vars.get(a) == context.facts.global_vars.get(b)
                and b in context.facts.global_vars
            )
        ):
            renames.add((a, b))
    return renames


def _changed_bindings(before: SourceFacts, after: SourceFacts, aliases: dict[str, str]) -> set[str]:
    def migrated_type(typ: str) -> str:
        return re.sub(r"\b\w+\b", lambda m: aliases.get(m[0], m[0]), typ)

    changed_bindings = set()
    for old_map, new_map in (
        (before.global_vars, after.global_vars),
        (before.function_return_names, after.function_return_names),
    ):
        for symbol, typ in old_map.items():
            new_name = symbol if symbol in new_map else aliases.get(symbol, symbol)
            if migrated_type(typ) != new_map.get(new_name):
                changed_bindings.add(symbol)
    for struct, fields in before.struct_fields.items():
        updated = after.struct_fields.get(struct, {})
        for name, typ in fields.items():
            if migrated_type(typ) != updated.get(name):
                changed_bindings.add(name)
    return changed_bindings


def _function_edits(
    context: RuleContext,
    function: dict[str, Any],
    aligned: tuple[dict[int, int], dict[int, int]],
    program: _Program,
    taken: set[str],
) -> tuple[list[TextEdit], list[Fix]] | None:
    source, source_id = program.source, program.source_id
    starts, ends = aligned
    function_span = source.span(function, source_id=source_id)
    if function_span is None or function_span.start not in starts:
        return None
    # Sort prefixes outermost-first when a receiver chain shares a start.
    prefixes: dict[int, list[tuple[int, str]]] = defaultdict(list)
    closings: list[TextEdit] = []
    fixes: list[Fix] = []
    grouped, discarded = set(), {}
    for parent in iter_nodes(function):
        base = parent.get("value")
        if (
            not isinstance(base, dict)
            or base.get("ast_type") != "Call"
            or type(base.get("node_id")) is not int
        ):
            continue
        if parent.get("ast_type") == "Expr":
            discarded[base.get("node_id")] = parent
        if parent.get("ast_type") in ("Subscript", "Attribute"):
            inner, outer = node_span(base), node_span(parent)
            if inner and outer and inner.start == outer.start:
                grouped.add(base.get("node_id"))
    consumed = False
    for call in iter_nodes(function, "Call"):
        func = call.get("func")
        if not isinstance(func, dict):
            return None
        if func.get("ast_type") == "Name":
            continue
        if func.get("ast_type") != "Attribute" or not isinstance(func.get("value"), dict):
            return None
        method = func.get("attr")
        call_id = call.get("node_id")
        if type(call_id) is not int or not isinstance(method, str) or not method.isidentifier():
            return None
        proof = program.external.get(call_id)
        if proof is None:
            receiver = func.get("value", {})
            if (
                isinstance(receiver, dict)
                and receiver.get("ast_type") == "Name"
                and receiver.get("id") == "self"
            ):
                continue
            proof = program.members.get(call_id)
            if proof is None:
                return None
        span = source.span(call, source_id=source_id)
        if (
            span is None
            or not function_span.start <= span.start < span.end <= function_span.end
            or proof.src != call.get("src")
            or proof.method != method
            or span.start not in starts
            or span.end not in ends
        ):
            return None
        consumed = True
        if proof.mode is None:
            # The producer positively identified a native dynamic-array member.
            continue
        start, end = starts[span.start], ends[span.end]
        mode = proof.mode
        interface = program.aliases.get(proof.interface, proof.interface)
        if (
            proof.interface in program.local_interfaces or interface != proof.interface
        ) and context.facts.interfaces.get(interface, {}).get(method) != mode:
            return None
        keyword, code = (
            ("staticcall", "VY041") if mode in {"view", "pure"} else ("extcall", "VY040")
        )
        if not context.is_enabled(code):
            continue
        row = bisect_right(program.current.lines, start)
        before = context.source[start:end]
        after = f"{keyword} {before}"
        prefix = keyword + " "
        fixes.append(Fix(code, row, f"added {keyword} to {mode} external call", before, after))
        if call_id in grouped and context.is_enabled("VY042"):
            prefix = "(" + prefix
            closings.append(TextEdit(end, end, ")"))
            fixes.append(
                Fix(
                    "VY042",
                    row,
                    "parenthesized external call before member access",
                    after,
                    f"({after})",
                )
            )
        return_type = proof.returns
        if call_id in discarded and code == "VY041" and return_type and context.is_enabled("VY057"):
            expr = source.span(discarded[call_id], source_id=source_id)
            if expr is None or expr.start not in starts:
                return None
            type_name = (
                context.facts.interface_returns.get(interface, {}).get(method) or return_type
            )
            variable = discard_assignment_name(row, taken)
            assignment = f"{variable}: {type_name} = "
            prefixes[starts[expr.start]].append((-(len(context.source) + 1), assignment))
            fixes.append(
                Fix(
                    "VY057", row, "assigned ignored external call result", after, assignment + after
                )
            )
        prefixes[start].append((-end, prefix))
    if not consumed:
        return None
    edits = [
        TextEdit(pos, pos, "".join(s for _, s in sorted(parts))) for pos, parts in prefixes.items()
    ]
    return [*closings, *edits], fixes


def discard_assignment_name(line_no: int, taken: set[str]) -> str:
    base = f"__vyupgrade_discard_{line_no}"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    taken.add(candidate)
    return candidate
