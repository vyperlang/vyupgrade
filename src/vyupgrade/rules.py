from __future__ import annotations

import re
from collections.abc import Iterator
from collections import Counter
from pathlib import Path

from .analysis import (
    SourceFacts,
    parse_source_facts,
)
from .models import Config, Diagnostic, Fix, RewriteResult
from .rule_helpers import (
    has_line_comment as _has_line_comment,
    innermost_non_overlapping as _innermost_non_overlapping,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    remove_constructor_decorators as _remove_constructor_decorators,
    strip_arg_comments as _strip_arg_comments,
)
from .rule_groups.comparisons import not_in_comparator
from .rule_groups.diagnostics import (
    decimal_diagnostic,
    missing_pragma_diagnostic,
    prevrandao_diagnostic,
)
from .rule_groups.interfaces import (
    _absolute_relative_imports,
    _constant_accessor_collisions,
    _immutable_accessor_collisions,
    _interface_imports,
    _interface_view_mutability,
    _legacy_constants,
    _pure_immutable_reads,
)
from .rule_groups.legacy import (
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
