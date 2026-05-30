from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from collections import Counter
from pathlib import Path

from .analysis import (
    SourceFacts,
    infer_expr_type,
    normalize_type,
    parse_source_facts,
)
from .models import Config, Diagnostic, Fix, RewriteResult
from .rule_helpers import (
    has_line_comment as _has_line_comment,
    innermost_non_overlapping as _innermost_non_overlapping,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    line_offsets as _line_offsets,
    nested_under_config_path as _nested_under_config_path,
    remove_constructor_decorators as _remove_constructor_decorators,
    strip_arg_comments as _strip_arg_comments,
)
from .rule_groups.comparisons import not_in_comparator
from .rule_groups.diagnostics import (
    decimal_diagnostic,
    missing_pragma_diagnostic,
    prevrandao_diagnostic,
)
from .rule_groups.legacy import (
    IMPORT_RENAMES,
    _early_beta_syntax,
    _event_kwargs,
    _legacy_builtin_calls,
    _legacy_constructor_locks,
    _legacy_decorators,
    _legacy_diagnostics,
    _legacy_dynamic_types,
    _legacy_events,
    _legacy_maps_and_interfaces,
    _legacy_type_units,
    _natspec_strictness,
    _pragma,
    _reserved_parameter_names,
)
from .rule_groups.external_calls import (
    _all_external_call_matches,
    _external_call_keywords,
    _external_call_subscripts,
    ignored_external_call_results,
)
from .rule_groups.numeric import (
    _bitwise,
    _cast_integer_arg_to_expected,
    _constant_exponent_literals_context,
    _constant_integer_decl_casts,
    _dynamic_bytes_hex_literals,
    _dynamic_pow_mod256,
    _integer_assignment_casts,
    _integer_division,
    _mixed_signed_unsigned_arithmetic,
    _pre_04_expression_rewrites,
    _range_bound,
    _redundant_integer_convert,
    _signed_integer_array_constant_types,
    _sqrt,
    _typed_array_literal_arguments,
    _typed_external_call_arguments,
    _typed_range_loops,
    _unsigned_range_bound_signed_constants,
)
from .rule_registry import (
    ContextRuleRunner,
    Rule,
    RuleContext,
    any_enabled as _any_enabled,
    configure_rule_changes,
    crossing,
    is_enabled as _enabled,
    rule_changes,
    target_floor,
    target_update,
)
from .source import (
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
    replace_identifier,
    split_top_level_args,
    span_is_code,
    TextEdit,
)
from .versions import MigrationContext, infer_pragma


def apply_rules(source: str, config: Config, path: Path | None = None) -> RewriteResult:
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    context = MigrationContext.from_specs(
        config.source_version or infer_pragma(source), config.target_version
    )

    current = source
    rule_context = RuleContext(
        current, config, context, path, lambda rule: _enabled(rule, config, context)
    )
    for rule in _runnable_rules():
        current, rule_fixes, rule_diagnostics = rule(rule_context)
        rule_context = rule_context.with_source(current)
        fixes.extend(rule_fixes)
        diagnostics.extend(rule_diagnostics)

    fixes = [fix for fix in fixes if _enabled(fix.rule, config, context)]
    diagnostics = [diag for diag in diagnostics if _enabled(diag.rule, config, context)]
    return RewriteResult(current, fixes, diagnostics)


def _runnable_rules() -> Iterator[ContextRuleRunner]:
    for rule in RULES:
        runner = rule.bind()
        if runner is not None:
            yield runner


def _constructor_deploy(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY002", config, context):
        return source, [], []
    current, fixes, insertions = _remove_constructor_decorators(
        source,
        {"@external", "@internal", "@public", "@private"},
        "VY002",
        "removed invalid constructor decorator",
        add_deploy=True,
    )
    fixes.extend(insertions)
    return current, fixes, []


def _abi_builtins(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    current = source
    for before, after, rule in [
        ("_abi_encode", "abi_encode", "VY010"),
        ("_abi_decode", "abi_decode", "VY011"),
    ]:
        if not _enabled(rule, config, context):
            continue
        next_source, edits = replace_identifier(current, before, after)
        for edit in edits:
            fixes.append(
                Fix(
                    rule,
                    line_number(current, edit.start),
                    f"renamed {before} to {after}",
                    before,
                    after,
                )
            )
        current = next_source
    return current, fixes, []


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


def _enum_to_flag(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY030"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if re.search(r"\benum\s+\w+:", source) is None:
        return source, fixes, diagnostics

    mask = code_mask(source)
    pattern = re.compile(r"^([ \t]*)enum[ \t]+([A-Za-z_][A-Za-z0-9_]*):", re.MULTILINE)
    for match in pattern.finditer(source):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
        diagnostics.append(
            Diagnostic(
                "VY030",
                line_number(source, match.start()),
                f"enum {match.group(2)} should be reviewed for flag compatibility",
            )
        )
    if not config.aggressive:
        return source, fixes, diagnostics

    def repl(match: re.Match[str]) -> str:
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        before = match.group(0)
        after = f"{match.group(1)}flag {match.group(2)}:"
        fixes.append(
            Fix("VY030", line_number(source, match.start()), "changed enum to flag", before, after)
        )
        return after

    return pattern.sub(repl, source), fixes, diagnostics


def _remove_internal_nonreentrant(source: str) -> tuple[str, list[Fix]]:
    lines = source.splitlines(keepends=True)
    fixes: list[Fix] = []
    out = list(lines)
    offset = 0
    for index, line in enumerate(lines):
        if not re.match(r"\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", line):
            continue
        start = index
        while start > 0 and re.match(
            r"\s*@[A-Za-z_][A-Za-z0-9_]*(?:\(.*\))?\s*(?:#.*)?$", lines[start - 1]
        ):
            start -= 1
        decorators = [
            decor.strip().split("(", 1)[0].split("#", 1)[0] for decor in lines[start:index]
        ]
        if "@internal" not in decorators or "@nonreentrant" not in decorators:
            continue
        for original_index in range(index - 1, start - 1, -1):
            if re.match(r"\s*@nonreentrant\b", lines[original_index]):
                before = out[original_index + offset].rstrip("\n")
                del out[original_index + offset]
                offset -= 1
                fixes.append(
                    Fix(
                        "VY090",
                        original_index + 1,
                        "removed internal nonreentrant decorator to avoid global-lock self-call violation",
                        before,
                        "",
                    )
                )
                break
    return "".join(out), fixes


def _struct_kwargs(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY060", config, context):
        return source, [], []
    current = source
    all_fixes: list[Fix] = []
    while True:
        facts = parse_source_facts(current)
        fixes: list[Fix] = []
        edits: list[TextEdit] = []
        mask = code_mask(current)
        for struct_name in sorted(facts.structs):
            field_order = list(facts.struct_fields.get(struct_name, {}))
            if not field_order:
                continue
            for match in re.finditer(rf"\b{re.escape(struct_name)}\s*\(", current):
                if not span_is_code(mask, match.start(), match.end()):
                    continue
                paren = current.find("(", match.start())
                close = find_matching(current, paren)
                if close is None:
                    continue
                raw_inner = current[paren + 1 : close]
                vars_for_line = facts.vars_at_line(line_number(current, match.start()))
                replacement_inner = _ordered_struct_args(
                    raw_inner,
                    facts.struct_fields.get(struct_name, {}),
                    vars_for_line,
                    facts,
                )
                if replacement_inner is None or replacement_inner == raw_inner:
                    continue
                replacement = f"{struct_name}({replacement_inner})"
                edits.append(TextEdit(match.start(), close + 1, replacement))
                fixes.append(
                    Fix(
                        "VY060",
                        line_number(current, match.start()),
                        "ordered struct constructor keyword arguments",
                        current[match.start() : close + 1],
                        replacement,
                    )
                )
        if not edits:
            return current, all_fixes, []
        selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
        all_fixes.extend(selected_fixes)
        current = apply_edits(current, selected_edits)


def _ordered_struct_args(
    raw_inner: str,
    struct_fields: dict[str, str],
    vars_for_line: dict[str, str],
    facts: SourceFacts,
) -> str | None:
    field_order = list(struct_fields)
    if "\n" in raw_inner and _has_line_comment(raw_inner):
        return None
    stripped = raw_inner.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        fields = split_top_level_args(_strip_arg_comments(stripped[1:-1]))
        if fields is None:
            return None
        pairs: list[tuple[str, str]] = []
        for field in fields:
            pair = _split_struct_pair(field, ":")
            if pair is None:
                return None
            pairs.append(pair)
        return _ordered_kwarg_string(pairs, field_order, struct_fields, vars_for_line, facts)

    args = split_top_level_args(_strip_arg_comments(raw_inner))
    if args is None:
        return None
    pairs = []
    for arg in args:
        pair = _split_struct_pair(arg, "=")
        if pair is None:
            return None
        pairs.append(pair)
    ordered = _ordered_kwarg_string(pairs, field_order, struct_fields, vars_for_line, facts)
    return ordered if ordered != ", ".join(arg.strip() for arg in args) else None


def _split_struct_pair(raw: str, sep: str) -> tuple[str, str] | None:
    if sep == "=":
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)\Z", raw, re.DOTALL)
    else:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)\Z", raw, re.DOTALL)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def _ordered_kwarg_string(
    pairs: list[tuple[str, str]],
    field_order: list[str],
    struct_fields: dict[str, str],
    vars_for_line: dict[str, str],
    facts: SourceFacts,
) -> str:
    by_name = dict(pairs)
    ordered_names = [name for name in field_order if name in by_name]
    ordered_names.extend(name for name, _value in pairs if name not in field_order)
    return ", ".join(
        f"{name}={_cast_integer_arg_to_expected(by_name[name], struct_fields.get(name), vars_for_line, facts)}"
        for name in ordered_names
    )


def _create_from_blueprint(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY080", config, context):
        return source, [], []
    diagnostics: list[Diagnostic] = []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bcreate_from_blueprint\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = source[match.end() : close]
        if "code_offset" in args:
            continue
        diagnostics.append(
            Diagnostic(
                "VY080",
                line_number(source, match.start()),
                "create_from_blueprint default code_offset changed from 0 to 3",
            )
        )
        edits.append(TextEdit(close, close, ", code_offset=0"))
        fixes.append(
            Fix(
                "VY080",
                line_number(source, match.start()),
                "added code_offset=0 to preserve 0.3.x behavior",
                "",
                "code_offset=0",
            )
        )
    return apply_edits(source, edits), fixes, diagnostics


def _nonreentrant(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY090", "VYD002"}, config, context):
        return source, [], []
    pattern = re.compile(r"@nonreentrant\(\s*([\"'])(.+?)\1\s*\)")
    locks = [match.group(2) for match in pattern.finditer(source)]
    diagnostics: list[Diagnostic] = []
    fixes: list[Fix] = []
    if not locks:
        return source, fixes, diagnostics
    counts = Counter(locks)
    if len(counts) > 1:
        first = pattern.search(source)
        diagnostics.append(
            Diagnostic(
                "VYD002",
                line_number(source, first.start() if first else 0),
                "multiple named reentrancy locks found; 0.4.x uses a global lock",
            )
        )
    if not _enabled("VY090", config, context):
        return source, fixes, diagnostics
    diagnostics.extend(
        Diagnostic(
            "VY090",
            line_number(source, match.start()),
            "single named nonreentrant lock rewritten; review callback assumptions",
        )
        for match in pattern.finditer(source)
    )

    def repl(match: re.Match[str]) -> str:
        fixes.append(
            Fix(
                "VY090",
                line_number(source, match.start()),
                "removed named nonreentrant lock",
                match.group(0),
                "@nonreentrant",
            )
        )
        return "@nonreentrant"

    current = pattern.sub(repl, source)
    current, internal_fixes = _remove_internal_nonreentrant(current)
    fixes.extend(internal_fixes)
    return current, fixes, diagnostics


RULES = (
    Rule("pragma", runner=_pragma, changes=(target_update("VY001", (0, 3, 10)),)),
    Rule("legacy_decorators", runner=_legacy_decorators, changes=(target_floor("VY201", (0, 2, 1)),)),
    Rule("legacy_type_units", runner=_legacy_type_units, changes=(target_floor("VY202", (0, 2, 1)),)),
    Rule(
        "legacy_events",
        runner=_legacy_events,
        changes=(
            target_floor("VY203", (0, 2, 1)),
            target_floor("VY204", (0, 2, 1)),
        ),
    ),
    Rule("event_kwargs", runner=_event_kwargs, changes=(crossing("VY112", (0, 4, 1)),)),
    Rule(
        "legacy_maps_and_interfaces",
        runner=_legacy_maps_and_interfaces,
        changes=(
            target_floor("VY205", (0, 2, 1)),
            target_floor("VY206", (0, 2, 1)),
        ),
    ),
    Rule(
        "early_beta_syntax",
        runner=_early_beta_syntax,
        changes=(
            target_floor("VY216", (0, 2, 1)),
            target_floor("VY217", (0, 2, 1)),
            target_floor("VY218", (0, 2, 1)),
            target_floor("VY219", (0, 2, 1)),
            target_floor("VY221", (0, 2, 1)),
        ),
    ),
    Rule("legacy_dynamic_types", runner=_legacy_dynamic_types, changes=(target_floor("VY207", (0, 2, 1)),)),
    Rule("reserved_parameter_names", runner=_reserved_parameter_names, changes=(target_floor("VY212", (0, 2, 1)),)),
    Rule(
        "legacy_diagnostics",
        runner=_legacy_diagnostics,
        changes=(
            target_floor("VYD210", (0, 2, 1)),
            target_floor("VYD211", (0, 2, 1)),
            target_floor("VYD212", (0, 2, 1)),
            target_floor("VYD213", (0, 2, 1)),
            target_floor("VYD214", (0, 2, 1)),
            target_floor("VYD215", (0, 2, 1)),
        ),
    ),
    Rule("natspec_strictness", runner=_natspec_strictness, changes=(crossing("VY058", (0, 4, 0)),)),
    Rule(
        "legacy_builtin_calls",
        runner=_legacy_builtin_calls,
        changes=(
            target_floor("VY208", (0, 2, 1)),
            target_floor("VY209", (0, 2, 1)),
        ),
    ),
    Rule(
        "not_in_comparator",
        context_runner=not_in_comparator,
        changes=(crossing("VY211", (0, 2, 8)),),
    ),
    Rule("legacy_constructor_locks", runner=_legacy_constructor_locks, changes=(crossing("VY210", (0, 2, 16)),)),
    Rule(
        "pre_04_expression_rewrites",
        runner=_pre_04_expression_rewrites,
        changes=(
            crossing("VY220", (0, 3, 7)),
            crossing("VY230", (0, 3, 8)),
            crossing("VY231", (0, 3, 8)),
            crossing("VYD013", (0, 3, 8)),
        ),
    ),
    Rule("constructor_deploy", runner=_constructor_deploy, changes=(crossing("VY002", (0, 4, 0)),)),
    Rule(
        "abi_builtins",
        runner=_abi_builtins,
        changes=(
            crossing("VY010", (0, 4, 0)),
            crossing("VY011", (0, 4, 0)),
        ),
    ),
    Rule("legacy_constants", runner=_legacy_constants, changes=(crossing("VY012", (0, 4, 0)),)),
    Rule("immutable_accessor_collisions", runner=_immutable_accessor_collisions, changes=(crossing("VY013", (0, 4, 0)),)),
    Rule("constant_accessor_collisions", runner=_constant_accessor_collisions, changes=(crossing("VY016", (0, 4, 0)),)),
    Rule("interface_view_mutability", runner=_interface_view_mutability, changes=(crossing("VY014", (0, 4, 0)),)),
    Rule("pure_immutable_reads", runner=_pure_immutable_reads, changes=(crossing("VY015", (0, 4, 0)),)),
    Rule(
        "interface_imports",
        runner=_interface_imports,
        changes=(
            crossing("VY020", (0, 4, 0)),
            crossing("VYD003", (0, 4, 0)),
        ),
    ),
    Rule(
        "absolute_relative_imports",
        path_runner=_absolute_relative_imports,
        changes=(crossing("VYD015", (0, 4, 1)),),
    ),
    Rule("enum_to_flag", runner=_enum_to_flag, changes=(crossing("VY030", (0, 4, 0)),)),
    Rule(
        "range_bound",
        runner=_range_bound,
        changes=(
            crossing("VY071", (0, 4, 0)),
            crossing("VYD011", (0, 4, 0)),
            crossing("VYD014", (0, 3, 10)),
        ),
    ),
    Rule("typed_range_loops", runner=_typed_range_loops, changes=(crossing("VY070", (0, 4, 0)),)),
    Rule("integer_assignment_casts", runner=_integer_assignment_casts, changes=(crossing("VY052", (0, 4, 0)),)),
    Rule(
        "external_call_keywords",
        runner=_external_call_keywords,
        changes=(
            crossing("VY040", (0, 4, 0)),
            crossing("VY041", (0, 4, 0)),
        ),
    ),
    Rule("external_call_subscripts", runner=_external_call_subscripts, changes=(crossing("VY042", (0, 4, 0)),)),
    Rule("external_call_keywords_after_subscripts", runner=_external_call_keywords),
    Rule(
        "ignored_external_call_results",
        context_runner=ignored_external_call_results,
        changes=(crossing("VY057", (0, 4, 0)),),
    ),
    Rule(
        "integer_division",
        runner=_integer_division,
        changes=(
            crossing("VY050", (0, 4, 0)),
            crossing("VYD004", (0, 4, 0)),
        ),
    ),
    Rule(
        "constant_exponent_literals",
        context_runner=_constant_exponent_literals_context,
        changes=(crossing("VY054", (0, 4, 0)),),
    ),
    Rule("mixed_signed_unsigned_arithmetic", runner=_mixed_signed_unsigned_arithmetic),
    Rule("signed_integer_array_constant_types", runner=_signed_integer_array_constant_types),
    Rule("typed_array_literal_arguments", runner=_typed_array_literal_arguments),
    Rule("unsigned_range_bound_signed_constants", runner=_unsigned_range_bound_signed_constants, changes=(crossing("VY056", (0, 4, 0)),)),
    Rule("typed_external_call_arguments", runner=_typed_external_call_arguments),
    Rule("dynamic_pow_mod256", runner=_dynamic_pow_mod256, changes=(crossing("VY055", (0, 4, 0)),)),
    Rule("redundant_integer_convert", runner=_redundant_integer_convert, changes=(crossing("VY051", (0, 4, 0)),)),
    Rule("constant_integer_decl_casts", runner=_constant_integer_decl_casts),
    Rule("dynamic_bytes_hex_literals", runner=_dynamic_bytes_hex_literals, changes=(crossing("VY053", (0, 4, 0)),)),
    Rule("struct_kwargs", runner=_struct_kwargs, changes=(crossing("VY060", (0, 4, 0)),)),
    Rule("create_from_blueprint", runner=_create_from_blueprint, changes=(crossing("VY080", (0, 4, 0)),)),
    Rule(
        "nonreentrant",
        runner=_nonreentrant,
        changes=(
            crossing("VY090", (0, 4, 0)),
            crossing("VYD002", (0, 4, 0)),
        ),
    ),
    Rule("sqrt", runner=_sqrt, changes=(crossing("VY100", (0, 4, 2)),)),
    Rule(
        "bitwise",
        runner=_bitwise,
        changes=(
            crossing("VY110", (0, 4, 2)),
            crossing("VY111", (0, 4, 2)),
            crossing("VYD012", (0, 4, 2)),
        ),
    ),
    Rule("decimal_diagnostic", context_runner=decimal_diagnostic, changes=(crossing("VYD001", (0, 4, 0)),)),
    Rule("prevrandao_diagnostic", context_runner=prevrandao_diagnostic, changes=(crossing("VYD010", (0, 4, 0)),)),
    Rule("missing_pragma_diagnostic", context_runner=missing_pragma_diagnostic, changes=(crossing("VYD005", (0, 4, 0)),)),
    Rule("interface_split", changes=(crossing("VY120", (0, 4, 0)),)),
    Rule(
        "validation",
        changes=(
            crossing("VYD006", (0, 4, 0)),
            crossing("VYD007", (0, 4, 0)),
            crossing("VYD008", (0, 4, 0)),
            crossing("VYD009", (0, 4, 0)),
        ),
    ),
)

RULE_CHANGES = rule_changes(RULES)
configure_rule_changes(RULE_CHANGES)
