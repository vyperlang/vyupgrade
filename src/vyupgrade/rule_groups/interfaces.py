from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ..analysis import SourceFacts, infer_expr_type, normalize_type, parse_source_facts
from ..models import Config, Diagnostic, Fix
from ..rule_groups.external_calls import _all_external_call_matches
from ..rule_groups.legacy import IMPORT_RENAMES
from ..rule_helpers import (
    line_match_starts_outside_string as _line_match_starts_outside_string,
    line_offsets as _line_offsets,
    nested_under_config_path as _nested_under_config_path,
)
from ..rule_registry import any_enabled as _any_enabled, is_enabled as _enabled
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    line_number,
    replace_identifier,
    span_is_code,
)
from ..versions import MigrationContext


def _legacy_constants(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY012", config, context):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    replacements = {
        "MAX_UINT256": "max_value(uint256)",
        "MIN_INT128": "min_value(int128)",
        "MAX_INT128": "max_value(int128)",
        "MIN_INT256": "min_value(int256)",
        "MAX_INT256": "max_value(int256)",
        "ZERO_ADDRESS": "empty(address)",
        "EMPTY_BYTES32": "empty(bytes32)",
    }
    for before, after in replacements.items():
        current, edits = replace_identifier(current, before, after)
        for edit in edits:
            fixes.append(
                Fix(
                    "VY012",
                    line_number(current, edit.start),
                    f"replaced legacy constant {before}",
                    before,
                    after,
                )
            )
    return current, fixes, []


def _immutable_accessor_collisions(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY013", config, context):
        return source, [], []
    current, fixes = _accessor_collision_rewrites(
        source,
        r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*immutable\s*\(",
        "VY013",
        "immutable",
        _is_immutable_declaration_name,
    )
    return current, fixes, []


def _constant_accessor_collisions(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY016", config, context):
        return source, [], []
    current, fixes = _accessor_collision_rewrites(
        source,
        r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\s*\(",
        "VY016",
        "constant",
        _is_constant_declaration_name,
    )
    return current, fixes, []


def _accessor_collision_rewrites(
    source: str,
    declaration_pattern: str,
    rule: str,
    kind: str,
    is_allowed_declaration: Callable[[str, int], bool],
) -> tuple[str, list[Fix]]:
    declaration_names = {
        match.group(1)
        for match in re.finditer(
            declaration_pattern,
            source,
            re.MULTILINE,
        )
    }
    if not declaration_names:
        return source, []
    function_names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            source,
            re.MULTILINE,
        )
    }
    collisions = sorted(declaration_names & function_names)
    if not collisions:
        return source, []

    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    taken = code_identifiers(source)
    for name in collisions:
        replacement = _private_backing_name(name, taken)
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        name_edits: list[TextEdit] = []
        for match in pattern.finditer(source):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            if _is_function_definition_name(source, match.start()):
                continue
            if _is_attribute_name(source, match.start()):
                continue
            if _is_type_declaration_name(
                source, match.start(), match.end()
            ) and not is_allowed_declaration(source, match.start()):
                continue
            if _is_keyword_argument_name(source, match.start(), match.end()):
                continue
            name_edits.append(TextEdit(match.start(), match.end(), replacement))
        edits.extend(name_edits)
        fixes.extend(
            Fix(
                rule,
                line_number(source, edit.start),
                f"renamed {kind} backing variable that collides with accessor",
                name,
                replacement,
            )
            for edit in name_edits
        )
    return apply_edits(source, edits), fixes


def _private_backing_name(name: str, taken: set[str]) -> str:
    candidate = f"_{name}"
    while candidate in taken:
        candidate = f"_{candidate}"
    taken.add(candidate)
    return candidate


def _is_function_definition_name(source: str, start: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    return bool(re.fullmatch(r"[ \t]*def\s+", source[line_start:start]))


def _is_attribute_name(source: str, start: int) -> bool:
    i = start - 1
    while i >= 0 and source[i].isspace() and source[i] != "\n":
        i -= 1
    return i >= 0 and source[i] == "."


def _is_keyword_argument_name(source: str, start: int, end: int) -> bool:
    i = end
    while i < len(source) and source[i].isspace() and source[i] != "\n":
        i += 1
    if i >= len(source) or source[i] != "=":
        return False
    j = start - 1
    while j >= 0 and source[j].isspace():
        j -= 1
    return j >= 0 and source[j] in "(,{"


def _is_type_declaration_name(source: str, start: int, end: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    prefix = source[line_start:start]
    if prefix.strip():
        return False
    i = end
    while i < len(source) and source[i].isspace() and source[i] != "\n":
        i += 1
    return i < len(source) and source[i] == ":"


def _is_immutable_declaration_name(source: str, start: int) -> bool:
    line_end = source.find("\n", start)
    if line_end == -1:
        line_end = len(source)
    return bool(re.search(r":\s*immutable\s*\(", source[start:line_end]))


def _is_constant_declaration_name(source: str, start: int) -> bool:
    line_end = source.find("\n", start)
    if line_end == -1:
        line_end = len(source)
    return bool(re.search(r":\s*constant\s*\(", source[start:line_end]))


def _interface_view_mutability(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY014", config, context):
        return source, [], []
    view_names = _view_implementation_names(source)
    if not view_names:
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(
        r"^([ \t]*def[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\([^#\n]*\)[ \t]*(?:->[ \t]*[^:#\n]+)?[ \t]*:[ \t]*)(nonpayable)\b",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if match.group(2) not in view_names or not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(3), match.end(3), "view"))
        fixes.append(
            Fix(
                "VY014",
                line_number(source, match.start()),
                "changed local interface mutability to match view implementation",
                match.group(0),
                f"{match.group(1)}view",
            )
        )
    return apply_edits(source, edits), fixes, []


def _view_implementation_names(source: str) -> set[str]:
    names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*public\s*\(",
            source,
            re.MULTILINE,
        )
    }
    decorators: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        decorator = re.fullmatch(r"@([A-Za-z_][A-Za-z0-9_]*)", stripped)
        if decorator is not None:
            decorators.add(decorator.group(1))
            continue
        def_match = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", stripped)
        if def_match is not None:
            if decorators & {"view", "pure"}:
                names.add(def_match.group(1))
            decorators = set()
            continue
        if stripped:
            decorators = set()
    return names


def _pure_immutable_reads(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY015", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    immutable_names = _immutable_names(facts)
    mask = code_mask(source)
    line_offsets = _line_offsets(source)
    lines = source.splitlines(keepends=True)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for function_line, decorators in facts.function_decorators.items():
        if "pure" not in decorators:
            continue
        read_name = _function_read_name(
            source, mask, line_offsets, facts, function_line, immutable_names
        )
        has_static_raw_call = _function_contains(
            source, mask, line_offsets, facts, function_line, "raw_call"
        )
        has_external_view_call = _function_contains_external_view_call(source, facts, function_line)
        if read_name is None and not has_static_raw_call and not has_external_view_call:
            continue
        decorator_line = facts.function_decorator_lines.get(function_line, {}).get("pure")
        if decorator_line is None or decorator_line > len(lines):
            continue
        line_start = line_offsets[decorator_line - 1]
        decorator_match = re.search(r"@pure\b", lines[decorator_line - 1])
        if decorator_match is None:
            continue
        edits.append(
            TextEdit(
                line_start + decorator_match.start() + 1, line_start + decorator_match.end(), "view"
            )
        )
        message = (
            f"relaxed pure function that reads immutable {read_name}"
            if read_name is not None
            else (
                "relaxed pure function that performs static raw_call"
                if has_static_raw_call
                else "relaxed pure function that calls a view external function"
            )
        )
        fixes.append(
            Fix(
                "VY015",
                decorator_line,
                message,
                "@pure",
                "@view",
            )
        )
    return apply_edits(source, edits), fixes, []


def _function_contains_external_view_call(
    source: str, facts: SourceFacts, function_line: int
) -> bool:
    line_offsets = _line_offsets(source)
    body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
    for start, _end, target, method, cast_type in _all_external_call_matches(source, facts):
        if not (body_start <= start < body_end):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, start))
        if target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(
                target, vars_for_line, facts
            )
        else:
            target_type = cast_type or infer_expr_type(target, vars_for_line, facts)
        mutability = facts.interfaces.get(normalize_type(target_type or ""), {}).get(method)
        if mutability in {"view", "pure"}:
            return True
    return False


def _immutable_names(facts: SourceFacts) -> set[str]:
    return {name for name, type_name in facts.global_vars.items() if _is_immutable_type(type_name)}


def _is_immutable_type(type_name: str) -> bool:
    type_name = type_name.strip()
    if type_name.startswith("immutable("):
        return True
    return bool(re.fullmatch(r"public\s*\(\s*immutable\s*\(.+\)\s*\)", type_name))


def _function_read_name(
    source: str,
    mask: list[bool],
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
    names: set[str],
) -> str | None:
    body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
    local_names = set(facts.function_params.get(facts.function_names.get(function_line, ""), {}))
    for name in sorted(names):
        if name in local_names:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        for match in pattern.finditer(source, body_start, body_end):
            if span_is_code(mask, match.start(), match.end()) and not _is_attribute_name(
                source, match.start()
            ):
                return name
    return None


def _function_contains(
    source: str,
    mask: list[bool],
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
    name: str,
) -> bool:
    body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    return any(
        span_is_code(mask, match.start(), match.end())
        for match in pattern.finditer(source, body_start, body_end)
    )


def _function_body_span(
    source: str,
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
) -> tuple[int, int]:
    start = line_offsets[function_line] if function_line < len(line_offsets) else len(source)
    end_line = facts.function_ends.get(function_line, len(line_offsets))
    end = line_offsets[end_line] if end_line < len(line_offsets) else len(source)
    return start, end


def _interface_imports(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY020", "VYD003"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    lines = source.splitlines(keepends=True)
    changed = False
    requested_rewrites: dict[str, str] = {}
    taken = code_identifiers(source)
    mask = code_mask(source)
    offset = 0

    for i, line in enumerate(lines):
        match = re.match(r"(\s*)from\s+vyper\.interfaces\s+import\s+(.+?)(\s*(?:#.*)?)(\n?)$", line)
        if not match or not _line_match_starts_outside_string(source, mask, offset):
            offset += len(line)
            continue
        imports = [part.strip() for part in match.group(2).split(",")]
        mapped = [IMPORT_RENAMES.get(name, name) for name in imports]
        if mapped != imports and _enabled("VY020", config, context):
            import_entries: list[str] = []
            for old, new in zip(imports, mapped, strict=True):
                if old == new:
                    import_entries.append(new)
                elif new in taken:
                    import_entries.append(f"{new} as {old}")
                else:
                    import_entries.append(new)
                    requested_rewrites[old] = new
            lines[i] = (
                f"{match.group(1)}from ethereum.ercs import {', '.join(import_entries)}{match.group(3)}{match.group(4)}"
            )
            fixes.append(
                Fix(
                    "VY020",
                    i + 1,
                    "updated built-in interface import path",
                    line.rstrip("\n"),
                    lines[i].rstrip("\n"),
                )
            )
            changed = True
        elif "vyper.interfaces" in line:
            if _enabled("VYD003", config, context):
                diagnostics.append(
                    Diagnostic(
                        "VYD003", i + 1, "unknown built-in interface import; review manually"
                    )
                )
        offset += len(line)

    current = "".join(lines) if changed else source
    for old, new in requested_rewrites.items():
        next_source, edits = replace_identifier(current, old, new)
        for edit in edits:
            fixes.append(
                Fix(
                    "VY020",
                    line_number(current, edit.start),
                    f"renamed interface type {old} to {new}",
                    old,
                    new,
                )
            )
        current = next_source
    return current, fixes, diagnostics


def _absolute_relative_imports(path: Path | None):
    def rule(
        source: str, config: Config, context: MigrationContext
    ) -> tuple[str, list[Fix], list[Diagnostic]]:
        if (
            not _enabled("VYD015", config, context)
            or path is None
            or not _nested_under_config_path(path, config)
        ):
            return source, [], []
        diagnostics: list[Diagnostic] = []
        for match in re.finditer(
            r"^\s*import\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?\s*(?:#.*)?$",
            source,
            re.MULTILINE,
        ):
            module = match.group(1)
            if module in {"math"}:
                continue
            diagnostics.append(
                Diagnostic(
                    "VYD015",
                    line_number(source, match.start()),
                    "nested module uses bare import; 0.4.1 disallows implicit relative imports, review as 'from . import ...'",
                )
            )
        return source, [], diagnostics

    return rule

