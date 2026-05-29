from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .analysis import (
    SourceFacts,
    infer_expr_type,
    indexed_key_type,
    indexed_value_type,
    is_integer_type,
    iterable_element_type,
    normalize_type,
    parse_source_facts,
    unwrap_type,
)
from .models import Config, Diagnostic, Fix
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
from .versions import MigrationContext, VyperVersion, infer_pragma


IMPORT_RENAMES = {
    "ERC20": "IERC20",
    "ERC20Detailed": "IERC20Detailed",
    "ERC165": "IERC165",
    "ERC4626": "IERC4626",
    "ERC721": "IERC721",
    "ERC1155": "IERC1155",
}


@dataclass
class RewriteResult:
    source: str
    fixes: list[Fix]
    diagnostics: list[Diagnostic]


@dataclass(frozen=True)
class RuleChange:
    introduced: VyperVersion
    mode: str = "crossing"


RULE_CHANGES = {
    "VY001": RuleChange(VyperVersion(0, 3, 10), "target"),
    "VY002": RuleChange(VyperVersion(0, 4, 0)),
    "VY010": RuleChange(VyperVersion(0, 4, 0)),
    "VY011": RuleChange(VyperVersion(0, 4, 0)),
    "VY012": RuleChange(VyperVersion(0, 4, 0)),
    "VY013": RuleChange(VyperVersion(0, 4, 0)),
    "VY014": RuleChange(VyperVersion(0, 4, 0)),
    "VY015": RuleChange(VyperVersion(0, 4, 0)),
    "VY016": RuleChange(VyperVersion(0, 4, 0)),
    "VY020": RuleChange(VyperVersion(0, 4, 0)),
    "VY030": RuleChange(VyperVersion(0, 4, 0)),
    "VY040": RuleChange(VyperVersion(0, 4, 0)),
    "VY041": RuleChange(VyperVersion(0, 4, 0)),
    "VY042": RuleChange(VyperVersion(0, 4, 0)),
    "VY050": RuleChange(VyperVersion(0, 4, 0)),
    "VY051": RuleChange(VyperVersion(0, 4, 0)),
    "VY052": RuleChange(VyperVersion(0, 4, 0)),
    "VY053": RuleChange(VyperVersion(0, 4, 0)),
    "VY054": RuleChange(VyperVersion(0, 4, 0)),
    "VY055": RuleChange(VyperVersion(0, 4, 0)),
    "VY056": RuleChange(VyperVersion(0, 4, 0)),
    "VY057": RuleChange(VyperVersion(0, 4, 0)),
    "VY058": RuleChange(VyperVersion(0, 4, 0)),
    "VY060": RuleChange(VyperVersion(0, 4, 0)),
    "VY070": RuleChange(VyperVersion(0, 4, 0)),
    "VY071": RuleChange(VyperVersion(0, 4, 0)),
    "VY080": RuleChange(VyperVersion(0, 4, 0)),
    "VY090": RuleChange(VyperVersion(0, 4, 0)),
    "VY100": RuleChange(VyperVersion(0, 4, 2)),
    "VY110": RuleChange(VyperVersion(0, 4, 2)),
    "VY111": RuleChange(VyperVersion(0, 4, 2)),
    "VY112": RuleChange(VyperVersion(0, 4, 1)),
    "VY201": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY202": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY203": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY204": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY205": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY206": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY207": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY208": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY209": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VY211": RuleChange(VyperVersion(0, 2, 8)),
    "VY210": RuleChange(VyperVersion(0, 2, 16)),
    "VY220": RuleChange(VyperVersion(0, 3, 7)),
    "VY230": RuleChange(VyperVersion(0, 3, 8)),
    "VY231": RuleChange(VyperVersion(0, 3, 8)),
    "VYD001": RuleChange(VyperVersion(0, 4, 0)),
    "VYD002": RuleChange(VyperVersion(0, 4, 0)),
    "VYD003": RuleChange(VyperVersion(0, 4, 0)),
    "VYD004": RuleChange(VyperVersion(0, 4, 0)),
    "VYD010": RuleChange(VyperVersion(0, 4, 0)),
    "VYD011": RuleChange(VyperVersion(0, 4, 0)),
    "VYD012": RuleChange(VyperVersion(0, 4, 2)),
    "VYD013": RuleChange(VyperVersion(0, 3, 8)),
    "VYD014": RuleChange(VyperVersion(0, 3, 10)),
    "VYD015": RuleChange(VyperVersion(0, 4, 1)),
    "VYD210": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VYD211": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VYD212": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VYD213": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VYD214": RuleChange(VyperVersion(0, 2, 1), "target"),
    "VYD215": RuleChange(VyperVersion(0, 2, 1), "target"),
}


def apply_rules(source: str, config: Config, path: Path | None = None) -> RewriteResult:
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    context = MigrationContext.from_specs(config.source_version or infer_pragma(source), config.target_version)

    current = source
    for rule in [
        _pragma,
        _legacy_decorators,
        _legacy_type_units,
        _legacy_events,
        _event_kwargs,
        _legacy_maps_and_interfaces,
        _legacy_dynamic_types,
        _legacy_diagnostics,
        _natspec_strictness,
        _legacy_builtin_calls,
        _not_in_comparator,
        _legacy_constructor_locks,
        _pre_04_expression_rewrites,
        _constructor_deploy,
        _abi_builtins,
        _legacy_constants,
        _immutable_accessor_collisions,
        _constant_accessor_collisions,
        _interface_view_mutability,
        _pure_immutable_reads,
        _interface_imports,
        _absolute_relative_imports(path),
        _enum_to_flag,
        _range_bound,
        _typed_range_loops,
        _external_call_keywords,
        _external_call_subscripts,
        _ignored_external_call_results,
        _integer_division,
        _constant_exponent_literals,
        _mixed_signed_unsigned_arithmetic,
        _unsigned_range_bound_signed_constants,
        _typed_external_call_arguments,
        _dynamic_pow_mod256,
        _redundant_integer_convert,
        _dynamic_bytes_hex_literals,
        _struct_kwargs,
        _create_from_blueprint,
        _nonreentrant,
        _sqrt,
        _bitwise,
        _decimal_diagnostic,
        _prevrandao_diagnostic,
        _missing_pragma_diagnostic,
    ]:
        current, rule_fixes, rule_diagnostics = rule(current, config, context)
        fixes.extend(rule_fixes)
        diagnostics.extend(rule_diagnostics)

    fixes = [fix for fix in fixes if _enabled(fix.rule, config, context)]
    diagnostics = [diag for diag in diagnostics if _enabled(diag.rule, config, context)]
    return RewriteResult(current, fixes, diagnostics)


def _enabled(rule: str, config: Config, context: MigrationContext) -> bool:
    if config.select and rule not in config.select:
        return False
    if rule in config.ignore:
        return False
    change = RULE_CHANGES.get(rule)
    if change is None:
        return True
    if change.mode == "target":
        return context.target_at_least(change.introduced)
    return context.crosses(change.introduced)


def _any_enabled(rules: set[str], config: Config, context: MigrationContext) -> bool:
    return any(_enabled(rule, config, context) for rule in rules)


def _innermost_non_overlapping(
    edits: list[TextEdit], fixes: list[Fix]
) -> tuple[list[TextEdit], list[Fix]]:
    selected: list[tuple[TextEdit, Fix]] = []
    for edit, fix in sorted(zip(edits, fixes, strict=True), key=lambda item: (item[0].end - item[0].start, item[0].start)):
        if any(edit.start < kept.end and kept.start < edit.end for kept, _fix in selected):
            continue
        selected.append((edit, fix))
    selected.sort(key=lambda item: item[0].start)
    return [edit for edit, _fix in selected], [fix for _edit, fix in selected]


def _pragma(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY001", config, context):
        return source, [], []
    fixes: list[Fix] = []
    pattern = re.compile(r"^(\s*)#\s*(?:@version|pragma\s+version)\s+(.+?)\s*$", re.MULTILINE)

    def repl(match: re.Match[str]) -> str:
        before = match.group(0)
        version = config.target_version if config.bump_pragma else match.group(2)
        after = f"{match.group(1)}#pragma version {version}"
        fixes.append(Fix("VY001", line_number(source, match.start()), "modernized version pragma", before, after))
        return after

    return pattern.sub(repl, source), fixes, []


def _legacy_decorators(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY201", config, context):
        return source, [], []
    fixes: list[Fix] = []
    replacements = {
        "public": "external",
        "private": "internal",
        "constant": "view",
    }
    pattern = re.compile(r"^(\s*)@(public|private|constant)(\s*(?:#.*)?$)", re.MULTILINE)

    def repl(match: re.Match[str]) -> str:
        before = match.group(0)
        after = f"{match.group(1)}@{replacements[match.group(2)]}{match.group(3)}"
        fixes.append(Fix("VY201", line_number(source, match.start()), "renamed legacy decorator", before, after))
        return after

    return pattern.sub(repl, source), fixes, []


def _legacy_type_units(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY202", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    type_re = re.compile(r"\b(u?int(?:8|16|32|64|128|256)?|decimal)\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)")
    for match in type_re.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        before = match.group(0)
        after = match.group(1)
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(Fix("VY202", line_number(source, match.start()), "removed legacy type unit", before, after))
    return apply_edits(source, edits), fixes, []


def _legacy_events(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    current = source
    if _enabled("VY203", config, context):
        current, event_fixes = _rewrite_legacy_event_declarations(current)
        fixes.extend(event_fixes)
    if _enabled("VY204", config, context):
        pattern = re.compile(r"\blog\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        edits: list[TextEdit] = []
        mask = code_mask(current)
        for match in pattern.finditer(current):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            replacement = f"log {match.group(1)}("
            edits.append(TextEdit(match.start(), match.end(), replacement))
            fixes.append(Fix("VY204", line_number(current, match.start()), "changed legacy log call to statement", match.group(0), replacement))
        current = apply_edits(current, edits)
    return current, fixes, []


def _event_kwargs(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY112", config, context):
        return source, [], []
    event_fields = _collect_event_fields(source)
    if not event_fields:
        return source, [], []

    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\blog\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        event_name = match.group(1)
        fields = event_fields.get(event_name)
        if fields is None:
            continue
        open_index = source.find("(", match.start(), match.end())
        close = find_matching(source, open_index)
        if close is None:
            continue
        raw_args = source[open_index + 1 : close]
        args = split_top_level_args(_strip_arg_comments(raw_args))
        if args is None or len(args) != len(fields) or any("=" in arg for arg in args):
            continue
        kwargs = [f"{field}={arg}" for field, arg in zip(fields, args, strict=True)]
        if "\n" in raw_args:
            indent = source[source.rfind("\n", 0, match.start()) + 1 : match.start()]
            child_indent = indent + "    "
            joined = ",\n".join(f"{child_indent}{kwarg}" for kwarg in kwargs)
            replacement = f"log {event_name}(\n{joined}\n{indent})"
        else:
            replacement = f"log {event_name}({', '.join(kwargs)})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY112",
                line_number(source, match.start()),
                "changed positional event log to keyword arguments",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _strip_arg_comments(raw_args: str) -> str:
    lines: list[str] = []
    for line in raw_args.splitlines():
        mask = code_mask(line)
        comment_start = next(
            (
                index
                for index, char in enumerate(line)
                if char == "#" and (index == 0 or mask[index - 1])
            ),
            None,
        )
        if comment_start is not None:
            line = line[:comment_start]
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _split_top_level_arg_spans(text: str) -> list[tuple[int, int, str]] | None:
    spans: list[tuple[int, int, str]] = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == "\\":
                continue
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif char == "," and depth == 0:
            _append_arg_span(spans, text, start, index)
            start = index + 1
    if depth != 0 or quote is not None:
        return None
    _append_arg_span(spans, text, start, len(text))
    return spans


def _append_arg_span(spans: list[tuple[int, int, str]], text: str, start: int, end: int) -> None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        spans.append((start, end, text[start:end]))


def _legacy_maps_and_interfaces(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    current = source
    if _enabled("VY205", config, context):
        current, map_fixes = _rewrite_map_types(current)
        fixes.extend(map_fixes)
    if _enabled("VY206", config, context):
        pattern = re.compile(r"^(\s*)contract\s+([A-Za-z_][A-Za-z0-9_]*\s*:)", re.MULTILINE)

        def repl(match: re.Match[str]) -> str:
            before = match.group(0)
            after = f"{match.group(1)}interface {match.group(2)}"
            fixes.append(Fix("VY206", line_number(current, match.start()), "changed contract interface declaration", before, after))
            return after

        current = pattern.sub(repl, current)
    return current, fixes, []


def _legacy_dynamic_types(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY207", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\b(bytes|string)(\s*\[)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        after = "Bytes" if match.group(1) == "bytes" else "String"
        edits.append(TextEdit(match.start(1), match.end(1), after))
        fixes.append(Fix("VY207", line_number(source, match.start()), f"capitalized legacy {match.group(1)} type", match.group(1), after))
    return apply_edits(source, edits), fixes, []


def _legacy_diagnostics(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    if _enabled("VYD210", config, context):
        diagnostics.extend(_byte_string_literal_diagnostics(source))
    if _enabled("VYD211", config, context):
        diagnostics.extend(_reserved_value_parameter_diagnostics(source))
    if _enabled("VYD212", config, context):
        diagnostics.extend(_slice_uint256_diagnostics(source))
    if _enabled("VYD213", config, context):
        diagnostics.extend(_len_uint256_diagnostics(source))
    if _enabled("VYD214", config, context):
        diagnostics.extend(_call_kwarg_uint256_diagnostics(source))
    if _enabled("VYD215", config, context):
        mask = code_mask(source)
        diagnostics.extend(
            Diagnostic("VYD215", line_number(source, match.start()), "RLPList was removed; rewrite this data model manually")
            for match in re.finditer(r"\bRLPList\b", source)
            if span_is_code(mask, match.start(), match.end())
        )
    return source, [], diagnostics


def _natspec_strictness(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY058", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    in_docstring = False
    quote = ""
    doc_function_start: int | None = None
    offset = 0
    for line_no, raw_line in enumerate(source.splitlines(keepends=True), start=1):
        line = raw_line.rstrip("\n")
        stripped = line.lstrip()
        if not in_docstring:
            if stripped.startswith(('"""', "'''")):
                quote = stripped[:3]
                in_docstring = True
                doc_function_start = _function_start_at_line(facts, line_no)
                if stripped.count(quote) >= 2:
                    in_docstring = False
                    doc_function_start = None
            offset += len(raw_line)
            continue

        if stripped.startswith(quote):
            in_docstring = False
            doc_function_start = None
            offset += len(raw_line)
            continue

        params = _function_param_names_at_start(facts, doc_function_start)
        replacement = _natspec_line_replacement(line, params)
        if replacement is None:
            edits.append(TextEdit(offset, offset + len(raw_line), ""))
            fixes.append(Fix("VY058", line_no, "removed NatSpec line for unknown function parameter", line, ""))
        elif replacement != line:
            edits.append(TextEdit(offset, offset + len(line), replacement))
            fixes.append(Fix("VY058", line_no, "updated NatSpec tag syntax", line, replacement))
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


def _function_start_at_line(facts: SourceFacts, line_no: int) -> int | None:
    for start, end in sorted(facts.function_ends.items()):
        if start < line_no <= end:
            return start
    return None


def _function_param_names_at_start(facts: SourceFacts, start: int | None) -> set[str] | None:
    if start is None:
        return None
    name = facts.function_names.get(start)
    if name is None:
        return None
    return set(facts.function_params.get(name, {}))


def _natspec_line_replacement(line: str, params: set[str] | None) -> str | None:
    param_match = re.match(r"^(\s*)@param\s+([A-Za-z_][A-Za-z0-9_]*)(:)?(\s+.*)?$", line)
    if param_match is not None and params is not None:
        name = param_match.group(2)
        if name not in params or not (param_match.group(4) or "").strip():
            return None
        if param_match.group(3):
            return f"{param_match.group(1)}@param {name}{param_match.group(4) or ''}"
        return line
    fork_match = re.match(r"^(\s*)@fork(\s+.*)?$", line)
    if fork_match is not None:
        return f"{fork_match.group(1)}@custom:fork{fork_match.group(2) or ''}"
    return line


def _legacy_builtin_calls(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY208", "VY209"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    if _enabled("VY208", config, context):
        current, new_fixes = _replace_call_keyword(current, "raw_call", "outsize", "max_outsize", "VY208")
        fixes.extend(new_fixes)
        current, new_fixes = _replace_call_keyword(current, "extract32", "type", "output_type", "VY208")
        fixes.extend(new_fixes)
        current, new_fixes = _replace_assert_modifiable(current)
        fixes.extend(new_fixes)
    if _enabled("VY209", config, context):
        current, new_fixes = _remove_call_keyword_arg(current, "method_id", "output_type", "bytes4", "VY209")
        fixes.extend(new_fixes)
    return current, fixes, []


def _not_in_comparator(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY211", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"\bnot\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s+in\s+([A-Za-z_][A-Za-z0-9_.]*)\s*\)")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = f"{match.group(1)} not in {match.group(2)}"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(Fix("VY211", line_number(source, match.start()), "changed negated membership test to not in", match.group(0), replacement))
    return apply_edits(source, edits), fixes, []


def _legacy_constructor_locks(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY210", config, context):
        return source, [], []
    current, fixes, insertions = _remove_constructor_decorators(
        source,
        {"@nonreentrant"},
        "VY210",
        "removed nonreentrant constructor decorator",
    )
    fixes.extend(insertions)
    return current, fixes, []


def _pre_04_expression_rewrites(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    current = source
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if _enabled("VY220", config, context):
        current, new_fixes = _replace_identifier_expr(current, "block.difficulty", "block.prevrandao", "VY220", "renamed block.difficulty to block.prevrandao")
        fixes.extend(new_fixes)
    if _enabled("VY230", config, context):
        current, new_fixes = _remove_unary_plus(current)
        fixes.extend(new_fixes)
    if _any_enabled({"VY231", "VYD013"}, config, context):
        current, new_fixes, new_diagnostics = _replace_numeric_not(current, config, context)
        fixes.extend(new_fixes)
        diagnostics.extend(new_diagnostics)
    return current, fixes, diagnostics


def _constructor_deploy(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
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


def _abi_builtins(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
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
            fixes.append(Fix(rule, line_number(current, edit.start), f"renamed {before} to {after}", before, after))
        current = next_source
    return current, fixes, []


def _legacy_constants(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
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
            fixes.append(Fix("VY012", line_number(current, edit.start), f"replaced legacy constant {before}", before, after))
    return current, fixes, []


def _immutable_accessor_collisions(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY013", config, context):
        return source, [], []
    immutable_names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*immutable\s*\(",
            source,
            re.MULTILINE,
        )
    }
    if not immutable_names:
        return source, [], []
    function_names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            source,
            re.MULTILINE,
        )
    }
    collisions = sorted(immutable_names & function_names)
    if not collisions:
        return source, [], []

    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    taken = immutable_names | function_names
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
            if _is_type_declaration_name(source, match.start(), match.end()) and not _is_immutable_declaration_name(source, match.start()):
                continue
            if _is_keyword_argument_name(source, match.start(), match.end()):
                continue
            name_edits.append(TextEdit(match.start(), match.end(), replacement))
        edits.extend(name_edits)
        fixes.extend(
            Fix(
                "VY013",
                line_number(source, edit.start),
                "renamed immutable backing variable that collides with accessor",
                name,
                replacement,
            )
            for edit in name_edits
        )
    return apply_edits(source, edits), fixes, []


def _constant_accessor_collisions(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY016", config, context):
        return source, [], []
    constant_names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\s*\(",
            source,
            re.MULTILINE,
        )
    }
    if not constant_names:
        return source, [], []
    function_names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            source,
            re.MULTILINE,
        )
    }
    collisions = sorted(constant_names & function_names)
    if not collisions:
        return source, [], []

    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    taken = _code_identifiers(source)
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
            if _is_type_declaration_name(source, match.start(), match.end()) and not _is_constant_declaration_name(source, match.start()):
                continue
            if _is_keyword_argument_name(source, match.start(), match.end()):
                continue
            name_edits.append(TextEdit(match.start(), match.end(), replacement))
        edits.extend(name_edits)
        fixes.extend(
            Fix(
                "VY016",
                line_number(source, edit.start),
                "renamed constant backing variable that collides with accessor",
                name,
                replacement,
            )
            for edit in name_edits
        )
    return apply_edits(source, edits), fixes, []


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


def _interface_view_mutability(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
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


def _pure_immutable_reads(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY015", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    immutable_names = _immutable_names(facts)
    if not immutable_names:
        return source, [], []

    mask = code_mask(source)
    line_offsets = _line_offsets(source)
    lines = source.splitlines(keepends=True)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for function_line, decorators in facts.function_decorators.items():
        if "pure" not in decorators:
            continue
        read_name = _function_read_name(source, mask, line_offsets, facts, function_line, immutable_names)
        if read_name is None:
            continue
        decorator_line = facts.function_decorator_lines.get(function_line, {}).get("pure")
        if decorator_line is None or decorator_line > len(lines):
            continue
        line_start = line_offsets[decorator_line - 1]
        decorator_match = re.search(r"@pure\b", lines[decorator_line - 1])
        if decorator_match is None:
            continue
        edits.append(TextEdit(line_start + decorator_match.start() + 1, line_start + decorator_match.end(), "view"))
        fixes.append(
            Fix(
                "VY015",
                decorator_line,
                f"relaxed pure function that reads immutable {read_name}",
                "@pure",
                "@view",
            )
        )
    return apply_edits(source, edits), fixes, []


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
    body_start = line_offsets[function_line] if function_line < len(line_offsets) else len(source)
    end_line = facts.function_ends.get(function_line, len(line_offsets))
    body_end = line_offsets[end_line] if end_line < len(line_offsets) else len(source)
    local_names = set(facts.function_params.get(facts.function_names.get(function_line, ""), {}))
    for name in sorted(names):
        if name in local_names:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        for match in pattern.finditer(source, body_start, body_end):
            if span_is_code(mask, match.start(), match.end()) and not _is_attribute_name(source, match.start()):
                return name
    return None


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", source):
        offsets.append(match.end())
    return offsets


def _redundant_integer_convert(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY051", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = parse_source_facts(source)
    mask = code_mask(source)
    for match in re.finditer(r"\bconvert\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = source.find("(", match.start())
        close = find_matching(source, open_index)
        if close is None:
            continue
        args = split_top_level_args(source[open_index + 1 : close])
        if args is None or len(args) != 2:
            continue
        expr, target = args[0].strip(), args[1].strip()
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        expr_type = infer_expr_type(expr, vars_for_line, facts)
        if (
            is_integer_type(target)
            and normalize_type(expr_type or "") == normalize_type(target)
            and _simple_nonliteral_expr(expr)
        ):
            replacement = _redundant_convert_replacement(expr)
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(
                Fix(
                    "VY051",
                    line_number(source, match.start()),
                    "removed redundant integer convert to the same type",
                    source[match.start() : close + 1],
                    replacement,
                )
            )
            continue
        if target != "uint256" or not re.search(r"[-+*/%]", expr):
            continue
        if _integerish_expression(expr, vars_for_line):
            replacement = f"({expr})"
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(Fix("VY051", line_number(source, match.start()), "removed redundant uint256 convert around integer expression", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes, []


def _redundant_convert_replacement(expr: str) -> str:
    return f"({expr})" if re.search(r"[-+*/%<>=|&]", expr) else expr


def _simple_nonliteral_expr(expr: str) -> bool:
    return bool(re.fullmatch(r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", expr))


def _dynamic_bytes_hex_literals(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY053", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*:\s*Bytes\[[^\]]+\]\s*=\s*(0x[0-9A-Fa-f]*)\b")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = _hex_literal_to_byte_string(match.group(1))
        if replacement is None:
            continue
        edits.append(TextEdit(match.start(1), match.end(1), replacement))
        fixes.append(
            Fix(
                "VY053",
                line_number(source, match.start()),
                "changed dynamic bytes hex literal to byte string literal",
                match.group(1),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _hex_literal_to_byte_string(literal: str) -> str | None:
    raw = literal.removeprefix("0x")
    if len(raw) % 2 != 0:
        return None
    return 'b"' + "".join(f"\\x{raw[index:index + 2].lower()}" for index in range(0, len(raw), 2)) + '"'


def _interface_imports(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY020", "VYD003"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    lines = source.splitlines(keepends=True)
    changed = False
    requested_rewrites: dict[str, str] = {}

    for i, line in enumerate(lines):
        match = re.match(r"(\s*)from\s+vyper\.interfaces\s+import\s+(.+?)(\s*(?:#.*)?)(\n?)$", line)
        if not match:
            continue
        imports = [part.strip() for part in match.group(2).split(",")]
        mapped = [IMPORT_RENAMES.get(name, name) for name in imports]
        if mapped != imports and _enabled("VY020", config, context):
            requested_rewrites.update({old: new for old, new in zip(imports, mapped, strict=True) if old != new})
            lines[i] = f"{match.group(1)}from ethereum.ercs import {', '.join(mapped)}{match.group(3)}{match.group(4)}"
            fixes.append(Fix("VY020", i + 1, "updated built-in interface import path", line.rstrip("\n"), lines[i].rstrip("\n")))
            changed = True
        elif "vyper.interfaces" in line:
            if _enabled("VYD003", config, context):
                diagnostics.append(Diagnostic("VYD003", i + 1, "unknown built-in interface import; review manually"))

    current = "".join(lines) if changed else source
    for old, new in requested_rewrites.items():
        next_source, edits = replace_identifier(current, old, new)
        for edit in edits:
            fixes.append(Fix("VY020", line_number(current, edit.start), f"renamed interface type {old} to {new}", old, new))
        current = next_source
    return current, fixes, diagnostics


def _absolute_relative_imports(path: Path | None):
    def rule(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
        if not _enabled("VYD015", config, context) or path is None or not _nested_under_config_path(path, config):
            return source, [], []
        diagnostics: list[Diagnostic] = []
        for match in re.finditer(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?\s*(?:#.*)?$", source, re.MULTILINE):
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


def _enum_to_flag(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY030"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if re.search(r"\benum\s+\w+:", source) is None:
        return source, fixes, diagnostics

    pattern = re.compile(r"^(\s*)enum\s+([A-Za-z_][A-Za-z0-9_]*):", re.MULTILINE)
    for match in pattern.finditer(source):
        diagnostics.append(Diagnostic("VY030", line_number(source, match.start()), f"enum {match.group(2)} should be reviewed for flag compatibility"))
    if not config.aggressive:
        return source, fixes, diagnostics

    def repl(match: re.Match[str]) -> str:
        before = match.group(0)
        after = f"{match.group(1)}flag {match.group(2)}:"
        fixes.append(Fix("VY030", line_number(source, match.start()), "changed enum to flag", before, after))
        return after

    return pattern.sub(repl, source), fixes, diagnostics


def _external_call_keywords(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY040", "VY041", "VYD003"}, config, context):
        return source, [], []
    current = source
    all_fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    for _ in range(3):
        current, fixes, diagnostics = _external_call_keywords_once(current, config, context)
        all_fixes.extend(fixes)
        if not fixes:
            break
    return current, all_fixes, diagnostics


def _external_call_keywords_once(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for start, end, target, method, cast_type in _all_external_call_matches(source, facts):
        if not span_is_code(mask, start, end):
            continue
        prefix = source[max(0, start - 16) : start]
        if target == "self" or method in {"append", "pop"}:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, start))
        if target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(target, vars_for_line, facts)
        else:
            target_type = cast_type or infer_expr_type(target, vars_for_line, facts)
        mutability = facts.interfaces.get(normalize_type(target_type or ""), {}).get(method)
        if mutability is None:
            if _enabled("VYD003", config, context):
                diagnostics.append(Diagnostic("VYD003", line_number(source, start), f"cannot infer mutability for external call {target}.{method}"))
            continue
        keyword = "staticcall" if mutability in {"view", "pure"} else "extcall"
        rule = "VY041" if keyword == "staticcall" else "VY040"
        if not _enabled(rule, config, context):
            continue
        existing_keyword = re.search(r"\b(?P<keyword>extcall|staticcall)\s+$", prefix)
        if existing_keyword is not None:
            if existing_keyword.group("keyword") == keyword:
                continue
            keyword_start = start - (len(prefix) - existing_keyword.start("keyword"))
            edits.append(TextEdit(keyword_start, keyword_start + len(existing_keyword.group("keyword")), keyword))
            fixes.append(
                Fix(
                    rule,
                    line_number(source, start),
                    f"changed external call keyword to {keyword}",
                    existing_keyword.group("keyword"),
                    keyword,
                )
            )
            continue
        edits.append(TextEdit(start, start, keyword + " "))
        fixes.append(
            Fix(
                rule,
                line_number(source, start),
                f"added {keyword} to {mutability} external call",
                source[start:end].rstrip(),
                keyword + " " + source[start:end].rstrip(),
            )
        )

    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, diagnostics


def _interface_cast_call_matches(
    source: str, interfaces: dict[str, dict[str, str]]
) -> list[tuple[int, int, str, str, str]]:
    matches: list[tuple[int, int, str, str, str]] = []
    mask = code_mask(source)
    for interface_name in sorted(interfaces, key=len, reverse=True):
        for match in re.finditer(rf"(?<![\w.]){re.escape(interface_name)}\s*\(", source):
            open_index = source.find("(", match.start())
            close = find_matching(source, open_index)
            if close is None or not span_is_code(mask, match.start(), min(close + 1, len(source))):
                continue
            tail = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source[close + 1 :])
            if tail is None:
                continue
            end = close + 1 + tail.end()
            matches.append((match.start(), end, source[match.start() : close + 1], tail.group(1), interface_name))
    return matches


def _external_call_subscripts(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY042", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\b(?:staticcall|extcall)\s+", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        expression_end = _external_call_expression_end(source, match.end())
        if expression_end is None:
            continue
        if expression_end >= len(source) or source[expression_end] not in "[.":
            continue
        before = source[match.start() : expression_end]
        after = f"({before})"
        edits.append(TextEdit(match.start(), expression_end, after))
        fixes.append(
            Fix(
                "VY042",
                line_number(source, match.start()),
                "parenthesized external call before subscript",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _external_call_expression_end(source: str, start: int) -> int | None:
    cast_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", source[start:])
    if cast_match is not None:
        cast_open = start + cast_match.end() - 1
        cast_close = find_matching(source, cast_open)
        if cast_close is not None:
            method_match = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source[cast_close + 1 :])
            if method_match is not None:
                method_open = cast_close + 1 + method_match.end() - 1
                method_close = find_matching(source, method_open)
                if method_close is not None:
                    return method_close + 1

    open_index = source.find("(", start)
    if open_index == -1:
        return None
    close = find_matching(source, open_index)
    return None if close is None else close + 1


def _ignored_external_call_results(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY057", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    taken_names = _code_identifiers(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        line_no = line_number(source, offset)
        code_part, comment_part = _split_inline_comment_preserving_strings(line)
        stripped = code_part.strip()
        if (
            not stripped.startswith("staticcall ")
            or _delimiter_depth_before(source, offset) != 0
            or _previous_code_line_continues(source, offset)
        ):
            offset += len(raw_line)
            continue
        indent = code_part[: len(code_part) - len(code_part.lstrip(" \t"))]
        expr_start = offset + len(indent)
        keyword_match = re.match(r"(?:staticcall|extcall)\s+", source[expr_start:])
        if keyword_match is None:
            offset += len(raw_line)
            continue
        expr_end = _external_call_expression_end(source, expr_start + keyword_match.end())
        if expr_end is None or source[expr_end : offset + len(code_part)].strip():
            offset += len(raw_line)
            continue
        expr = source[expr_start:expr_end]
        expr_type = infer_expr_type(expr, facts.vars_at_line(line_no), facts)
        if expr_type is None:
            offset += len(raw_line)
            continue
        name = _discard_assignment_name(line_no, taken_names)
        replacement = f"{indent}{name}: {unwrap_type(expr_type)} = {expr}{comment_part}"
        edits.append(TextEdit(offset, offset + len(line), replacement))
        fixes.append(
            Fix(
                "VY057",
                line_no,
                "assigned ignored external call result",
                line,
                replacement,
            )
        )
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


def _delimiter_depth_before(source: str, end: int) -> int:
    mask = code_mask(source[:end])
    depth = 0
    for index, char in enumerate(source[:end]):
        if not mask[index]:
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
    return depth


def _previous_code_line_continues(source: str, offset: int) -> bool:
    if offset <= 0 or source[offset - 1] != "\n":
        return False
    previous_end = offset - 1
    previous_start = source.rfind("\n", 0, previous_end) + 1
    code_part, _comment_part = _split_inline_comment_preserving_strings(source[previous_start:previous_end])
    return code_part.rstrip().endswith("\\")


def _code_identifiers(source: str) -> set[str]:
    mask = code_mask(source)
    return {
        match.group(0)
        for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", source)
        if span_is_code(mask, match.start(), match.end())
    }


def _discard_assignment_name(line_no: int, taken_names: set[str]) -> str:
    base = f"__vyupgrade_discard_{line_no}"
    candidate = base
    suffix = 2
    while candidate in taken_names:
        candidate = f"{base}_{suffix}"
        suffix += 1
    taken_names.add(candidate)
    return candidate


def _split_inline_comment_preserving_strings(line: str) -> tuple[str, str]:
    quote: str | None = None
    i = 0
    while i < len(line):
        char = line[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        if char == "#":
            code = line[:i].rstrip()
            spacer = "  " if code else ""
            return code, spacer + line[i:]
        i += 1
    return line, ""


def _integer_division(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY050", "VYD004"}, config, context):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"(?<!/)/(?!/)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        line_start = source.rfind("\n", 0, match.start()) + 1
        line_end = source.find("\n", match.end())
        if line_end == -1:
            line_end = len(source)
        line = source[line_start:line_end]
        if re.match(r"\s*(?:from|import)\b", line):
            continue
        left = _read_left_operand(source, match.start())
        right = _read_right_operand(source, match.end())
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        left_type = infer_expr_type(left, vars_for_line, facts)
        right_type = infer_expr_type(right, vars_for_line, facts)
        lhs_type = _lhs_declared_type(line)
        assigned_type = _lhs_assigned_type(line, vars_for_line)
        return_type = facts.return_type_at_line(line_number(source, match.start()))
        slash_col = match.start() - line_start
        if (
            (is_integer_type(left_type) and is_integer_type(right_type))
            or (_integerish_expression(left, vars_for_line, facts) and is_integer_type(right_type))
            or (is_integer_type(left_type) and _integerish_expression(right, vars_for_line, facts))
            or (_integerish_expression(left, vars_for_line, facts) and _integerish_expression(right, vars_for_line, facts))
            or is_integer_type(lhs_type)
            or is_integer_type(assigned_type)
            or (line.lstrip().startswith("return ") and is_integer_type(return_type))
            or (
                _integerish_expression(line[:slash_col], vars_for_line, facts)
                and _integerish_expression(line[slash_col + 1 :], vars_for_line, facts)
            )
            or (
                _integerish_expression(line[slash_col + 1 :], vars_for_line, facts)
                and _multiline_integer_division_context(source, line_start)
            )
            or _multiline_integer_division_assignment_context(source, line_start, vars_for_line)
            or (
                _integerish_expression(line[slash_col + 1 :], vars_for_line, facts)
                and line.lstrip().startswith("assert ")
                and "decimal" not in line
            )
        ):
            if not _enabled("VY050", config, context):
                continue
            edits.append(TextEdit(match.start(), match.end(), "//"))
            fixes.append(Fix("VY050", line_number(source, match.start()), "changed integer division to //", "/", "//"))
        else:
            if _enabled("VYD004", config, context):
                diagnostics.append(Diagnostic("VYD004", line_number(source, match.start()), "cannot prove / operands are integer typed"))
    return apply_edits(source, edits), fixes, diagnostics


def _constant_exponent_literals(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY054", config, context):
        return source, [], []
    constant_values = _integer_constant_values(source)
    if not constant_values:
        return source, [], []

    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for name, value in constant_values.items():
        if value < 0:
            continue
        name_re = re.compile(rf"\b{re.escape(name)}\b")
        for name_match in name_re.finditer(source):
            start = name_match.start()
            end = name_match.end()
            if not span_is_code(mask, start, end) or not _inside_exponent(source, start, end):
                continue
            replacement = str(value)
            edits.append(TextEdit(start, end, replacement))
            fixes.append(
                Fix(
                    "VY054",
                    line_number(source, start),
                    "folded integer constant in unsigned exponent expression",
                    name,
                    replacement,
                )
            )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


def _dynamic_pow_mod256(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY055", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    convert_operand = r"convert\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*uint256\s*\)"
    pattern = re.compile(rf"(?P<left>{convert_operand})\s*\*\*\s*(?P<right>{convert_operand})")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()) or _top_level_constant_line(source, match.start()):
            continue
        left = match.group("left")
        right = match.group("right")
        replacement = f"pow_mod256({left}, {right})"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY055",
                line_number(source, match.start()),
                "rewrote dynamic exponentiation to pow_mod256",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _eval_integer_constant_expr(expr: str, values: dict[str, int]) -> int | None:
    try:
        node = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        return None
    return _eval_integer_ast(node.body, values)


def _eval_integer_ast(node: ast.AST, values: dict[str, int]) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_integer_ast(node.operand, values)
        if operand is None:
            return None
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        return None
    if isinstance(node, ast.BinOp):
        left = _eval_integer_ast(node.left, values)
        right = _eval_integer_ast(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv) and right != 0:
            return left // right
        if isinstance(node.op, ast.Mod) and right != 0:
            return left % right
        if isinstance(node.op, ast.Pow) and right >= 0:
            return left**right
    return None


def _inside_exponent(source: str, start: int, end: int) -> bool:
    before = source[max(0, start - 8) : start]
    after = source[end : min(len(source), end + 8)]
    return bool(re.search(r"\*\*\s*$", before) or re.match(r"\s*\*\*", after))


def _top_level_constant_line(source: str, index: int) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    return bool(re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*:\s*constant\s*\(", source[line_start:]))


def _constant_range_iteration_bound(args: str, values: dict[str, int]) -> int | None:
    parts = split_top_level_args(args)
    if parts is None:
        return None
    if len(parts) == 1:
        stop = _eval_integer_constant_expr(parts[0], values)
        if stop is None or stop < 0:
            return None
        return stop
    if len(parts) != 2:
        return None
    start = _eval_integer_constant_expr(parts[0], values)
    stop = _eval_integer_constant_expr(parts[1], values)
    if start is None or stop is None or stop < start:
        return None
    return stop - start


def _mixed_signed_unsigned_arithmetic(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY052", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        code_line = line.split("#", 1)[0]
        if not code_line.startswith((" ", "\t")) or not (
            re.search(r"[-+*/%<>]=?|==|!=", code_line)
            or re.search(r":\s*[^=]+=", code_line)
            or re.search(r"\b(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\s*\[[^=]+\])?\s*=", code_line)
            or re.search(r"\b(?:self\.)?[A-Za-z_][A-Za-z0-9_]*\s*\(", code_line)
            or "[" in code_line
        ):
            offset += len(raw_line)
            continue
        line_no = line_number(source, offset)
        vars_for_line = facts.vars_at_line(line_no)
        lhs_type = _lhs_declared_type(code_line) or _lhs_assigned_type(code_line, vars_for_line)
        rhs_offset = _expression_start_offset(code_line)
        rhs_start = offset + rhs_offset
        rhs = code_line[rhs_offset:]
        loop_vars = facts.loop_vars_at_line(line_no)
        signed_names = sorted(
            (
                name
                for name, type_name in vars_for_line.items()
                if _is_signed_integer_type(type_name) and (name in facts.global_vars or name in loop_vars)
            ),
            key=len,
            reverse=True,
        )
        for name in signed_names:
            for match in re.finditer(rf"\b{re.escape(name)}\b", rhs):
                start = rhs_start + match.start()
                end = start + len(name)
                if (
                    _inside_attribute_access(source, start, end)
                    or _inside_convert_call(source, start)
                    or _inside_range_header(source, start)
                    or _inside_type_subscript(source, start)
                    or _signed_comparison_target_type_at(source, start, name, vars_for_line) is not None
                    or _signed_internal_call_arg_target_type(source, start, name, facts) is not None
                    or _signed_external_call_arg_target_type(source, start, name, facts, vars_for_line) is not None
                    or _signed_subscript_key_target_type(source, start, name, vars_for_line) is not None
                    or not _signed_name_has_unsigned_context(source, start, lhs_type, vars_for_line)
                ):
                    continue
                replacement = f"convert({name}, uint256)"
                edits.append(TextEdit(start, end, replacement))
                fixes.append(
                    Fix(
                        "VY052",
                        line_no,
                        "converted signed integer constant in uint256 arithmetic",
                        name,
                        replacement,
                    )
                )
        unsigned_loop_names = sorted(
            (
                name
                for name in loop_vars
                if _is_unsigned_integer_type(_nearest_loop_var_type(source, rhs_start, name) or vars_for_line.get(name))
            ),
            key=len,
            reverse=True,
        )
        for name in unsigned_loop_names:
            for match in re.finditer(rf"\b{re.escape(name)}\b", rhs):
                start = rhs_start + match.start()
                end = start + len(name)
                if (
                    _inside_attribute_access(source, start, end)
                    or _inside_convert_call(source, start)
                    or _inside_range_header(source, start)
                ):
                    continue
                target_type = _signed_comparison_target_type(
                    _local_expression(source, start), name, vars_for_line
                ) or _signed_internal_call_arg_target_type(
                    source, start, name, facts
                ) or _signed_external_call_arg_target_type(
                    source, start, name, facts, vars_for_line
                ) or _signed_subscript_key_target_type(source, start, name, vars_for_line)
                if target_type is None:
                    continue
                replacement = f"convert({name}, {target_type})"
                edits.append(TextEdit(start, end, replacement))
                fixes.append(
                    Fix(
                        "VY052",
                        line_no,
                        "converted unsigned loop variable in signed comparison",
                        name,
                        replacement,
                    )
                )
        unsigned_constant_names = sorted(
            (
                name
                for name, type_name in vars_for_line.items()
                if _is_unsigned_integer_type(type_name) and name in facts.global_vars
            ),
            key=len,
            reverse=True,
        )
        for name in unsigned_constant_names:
            for match in re.finditer(rf"\b{re.escape(name)}\b", rhs):
                start = rhs_start + match.start()
                end = start + len(name)
                if (
                    _inside_attribute_access(source, start, end)
                    or _inside_convert_call(source, start)
                    or _inside_range_header(source, start)
                    or _inside_type_subscript(source, start)
                    or _is_unsigned_integer_type(lhs_type)
                ):
                    continue
                target_type = _unsigned_name_signed_division_target_type(
                    _local_expression(source, start), name, vars_for_line, facts
                )
                if target_type is None:
                    continue
                replacement = f"convert({name}, {target_type})"
                edits.append(TextEdit(start, end, replacement))
                fixes.append(
                    Fix(
                        "VY052",
                        line_no,
                        "converted unsigned integer constant in signed division",
                        name,
                        replacement,
                    )
                )
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


def _unsigned_range_bound_signed_constants(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY056", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    constant_values = _integer_constant_values(source)
    for match in re.finditer(
        r"\bfor\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*uint(?:\d+)?\s+in\s+range\s*\(",
        source,
    ):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, match.end() - 1)
        if close is None:
            continue
        args_start = match.end()
        args = source[args_start:close]
        arg_spans = _split_top_level_arg_spans(args)
        if arg_spans is None:
            continue
        positional_spans = [
            (start, end, arg)
            for start, end, arg in arg_spans
            if not re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", arg)
        ]
        has_bound_keyword = any(re.match(r"bound\s*=", arg) for _start, _end, arg in arg_spans)
        line_no = line_number(source, match.start())
        vars_for_line = facts.vars_at_line(line_no)
        converted = False
        for name, type_name in sorted(vars_for_line.items(), key=lambda item: len(item[0]), reverse=True):
            if not _is_signed_integer_type(type_name):
                continue
            for name_match in re.finditer(rf"\b{re.escape(name)}\b", args):
                if not any(start <= name_match.start() and name_match.end() <= end for start, end, _arg in positional_spans):
                    continue
                start = args_start + name_match.start()
                end = args_start + name_match.end()
                if _inside_convert_call(source, start) or not span_is_code(mask, start, end):
                    continue
                replacement = f"convert({name}, uint256)"
                edits.append(TextEdit(start, end, replacement))
                converted = True
                fixes.append(
                    Fix(
                        "VY056",
                        line_no,
                        "converted signed range bound for unsigned loop variable",
                        name,
                        replacement,
                    )
                )
        if converted and not has_bound_keyword:
            bound = _constant_range_iteration_bound(", ".join(arg for _start, _end, arg in positional_spans), constant_values)
            if bound is not None:
                replacement = f", bound={bound}"
                edits.append(TextEdit(close, close, replacement))
                fixes.append(
                    Fix(
                        "VY056",
                        line_no,
                        "added literal bound for converted unsigned range",
                        "",
                        replacement,
                    )
                )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


def _typed_external_call_arguments(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY052", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for start, end, target, method, cast_type in _all_external_call_matches(source, facts):
        if not span_is_code(mask, start, end):
            continue
        open_index = end - 1
        close = find_matching(source, open_index)
        if close is None:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, start))
        if cast_type is not None:
            target_type = cast_type
        elif target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(target, vars_for_line, facts)
        else:
            target_type = infer_expr_type(target, vars_for_line, facts)
        params = facts.interface_params.get(normalize_type(target_type or ""), {}).get(method)
        if not params:
            continue
        args = split_top_level_args(source[open_index + 1 : close])
        if args is None:
            continue
        cursor = open_index + 1
        for index, arg in enumerate(args):
            if index >= len(params):
                break
            expected = list(params.values())[index]
            replacement = _cast_integer_arg_to_expected(arg, expected, vars_for_line, facts)
            if replacement == arg:
                cursor += len(arg) + 1
                continue
            arg_start = source.find(arg, cursor, close)
            if arg_start == -1:
                cursor += len(arg) + 1
                continue
            edits.append(TextEdit(arg_start, arg_start + len(arg), replacement))
            fixes.append(
                Fix(
                    "VY052",
                    line_number(source, arg_start),
                    "converted external call argument to expected integer type",
                    arg,
                    replacement,
                )
            )
            cursor = arg_start + len(arg) + 1
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


def _all_external_call_matches(
    source: str, facts: SourceFacts
) -> list[tuple[int, int, str, str, str | None]]:
    variable_call_re = re.compile(
        r"(?<![\w.])(?P<target>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\("
    )
    matches: list[tuple[int, int, str, str, str | None]] = []
    matches.extend(_interface_cast_call_matches(source, facts.interfaces))
    matches.extend(
        (match.start(), match.end(), match.group("target"), match.group("method"), None)
        for match in variable_call_re.finditer(source)
    )
    return sorted(matches)


def _signed_name_has_unsigned_context(
    source: str, index: int, lhs_type: str | None, vars_for_line: dict[str, str]
) -> bool:
    if _is_unsigned_integer_type(lhs_type):
        return True
    if _inside_array_subscript(source, index, vars_for_line):
        return True
    return _has_unsigned_context(_local_expression(source, index), vars_for_line)


def _has_unsigned_context(line: str, vars_for_line: dict[str, str]) -> bool:
    if re.search(r"\bconvert\s*\([^,\n]+,\s*uint(?:\d+)?\s*\)", line):
        return True
    if re.search(r"\b(?:block\.(?:timestamp|number|difficulty|basefee|prevhash)|chain\.id|msg\.value|max_value\s*\(\s*uint)", line):
        return True
    for name, type_name in vars_for_line.items():
        if _is_unsigned_integer_type(type_name) and re.search(rf"\b(?:self\.)?{re.escape(name)}\b", line):
            return True
    return False


def _signed_comparison_target_type(expr: str, name: str, vars_for_line: dict[str, str]) -> str | None:
    expr = expr.strip().removesuffix(":").strip()
    expr = re.sub(r"^(?:if|assert|return)\s+", "", expr)
    match = re.match(r"(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)\Z", expr)
    if match is None:
        return None
    left, _op, right = (part.strip() for part in match.groups())
    if left == name:
        other_type = infer_expr_type(right, vars_for_line)
    elif right == name:
        other_type = infer_expr_type(left, vars_for_line)
    else:
        return None
    return normalize_type(other_type) if _is_signed_integer_type(other_type) else None


def _unsigned_name_signed_division_target_type(
    expr: str, name: str, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
    expr = expr.strip()
    expr = re.sub(r"^(?:return|assert)\s+", "", expr)
    if "=" in expr.split("//", 1)[0]:
        expr = expr.split("=", 1)[1].strip()
    match = re.match(r"(.+?)\s*//\s*(.+)\Z", expr)
    if match is None:
        return None
    left, right = (part.strip() for part in match.groups())
    if left == name:
        other_type = infer_expr_type(right, vars_for_line, facts)
    elif right == name:
        other_type = infer_expr_type(left, vars_for_line, facts)
    else:
        return None
    return normalize_type(other_type) if _is_signed_integer_type(other_type) else None


def _signed_comparison_target_type_at(source: str, index: int, name: str, vars_for_line: dict[str, str]) -> str | None:
    other = _comparison_peer(_local_expression(source, index), name)
    if other is None:
        return None
    loop_type = _nearest_loop_var_type(source, index, other) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", other) else None
    other_type = loop_type or infer_expr_type(other, vars_for_line)
    return normalize_type(other_type) if _is_signed_integer_type(other_type) else None


def _comparison_peer(expr: str, name: str) -> str | None:
    expr = expr.strip().removesuffix(":").strip()
    expr = re.sub(r"^(?:if|assert|return)\s+", "", expr)
    match = re.match(r"(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)\Z", expr)
    if match is None:
        return None
    left, _op, right = (part.strip() for part in match.groups())
    if left == name:
        return right
    if right == name:
        return left
    return None


def _nearest_loop_var_type(source: str, index: int, name: str) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    current_line = source[line_start : source.find("\n", line_start) if source.find("\n", line_start) != -1 else len(source)]
    current_indent = len(current_line) - len(current_line.lstrip(" "))
    prefix = source[:line_start].splitlines()
    for line in reversed(prefix):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent >= current_indent:
            continue
        loop_match = re.match(rf"for\s+{re.escape(name)}\s*:\s*([^:]+?)\s+in\b", stripped)
        if loop_match:
            return loop_match.group(1).strip()
        if re.match(r"(?:@|\s*def\s+)", stripped) and indent < current_indent:
            return None
    return None


def _signed_internal_call_arg_target_type(source: str, index: int, name: str, facts: SourceFacts) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("(", line_start, index)
    if open_index == -1:
        return None
    close = find_matching(source, open_index)
    if close is None or not (open_index < index < close):
        return None
    func_match = re.search(r"(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*$", source[line_start:open_index])
    if func_match is None:
        return None
    params = facts.function_params.get(func_match.group(1))
    if not params:
        return None
    raw_args = source[open_index + 1 : close]
    arg_index = _top_level_arg_index(raw_args, index - open_index - 1)
    if arg_index is None or arg_index >= len(params):
        return None
    arg = split_top_level_args(raw_args)
    if arg is None or arg_index >= len(arg) or arg[arg_index].strip() != name:
        return None
    target_type = list(params.values())[arg_index]
    return normalize_type(target_type) if _is_signed_integer_type(target_type) else None


def _signed_external_call_arg_target_type(
    source: str, index: int, name: str, facts: SourceFacts, vars_for_line: dict[str, str]
) -> str | None:
    target_type = _external_call_arg_expected_type(source, index, name, facts, vars_for_line)
    return normalize_type(target_type) if _is_signed_integer_type(target_type) else None


def _external_call_arg_expected_type(
    source: str, index: int, arg: str, facts: SourceFacts, vars_for_line: dict[str, str]
) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("(", line_start, index)
    if open_index == -1:
        return None
    close = find_matching(source, open_index)
    if close is None or not (open_index < index < close):
        return None
    raw_args = source[open_index + 1 : close]
    arg_index = _top_level_arg_index(raw_args, index - open_index - 1)
    args = split_top_level_args(raw_args)
    if arg_index is None or args is None or arg_index >= len(args) or args[arg_index].strip() != arg:
        return None
    prefix = source[line_start:open_index]
    call_match = re.search(
        r"(?:\b(?:staticcall|extcall)\s+)?(?:(?P<cast>[A-Za-z_][A-Za-z0-9_]*)\s*\([^()\n]*\)|(?P<target>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?))\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*$",
        prefix,
    )
    if call_match is None:
        return None
    if call_match.group("cast"):
        target_type = call_match.group("cast")
    else:
        target = call_match.group("target") or ""
        target_type = facts.storage_vars.get(target.removeprefix("self.")) or infer_expr_type(target, vars_for_line, facts)
    params = facts.interface_params.get(normalize_type(target_type or ""), {}).get(call_match.group("method"))
    if not params or arg_index >= len(params):
        return None
    return list(params.values())[arg_index]


def _signed_subscript_key_target_type(
    source: str, index: int, name: str, vars_for_line: dict[str, str]
) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("[", line_start, index)
    if open_index == -1:
        return None
    close_index = source.find("]", index)
    if close_index == -1:
        return None
    if source[open_index + 1 : close_index].strip() != name:
        return None
    root_match = re.search(r"((?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\s*$", source[line_start:open_index])
    if root_match is None:
        return None
    root = root_match.group(1)
    root_name = root[5:] if root.startswith("self.") else root
    root_type = vars_for_line.get(root_name) or infer_expr_type(root, vars_for_line)
    key_type = indexed_key_type(root_type)
    return normalize_type(key_type) if _is_signed_integer_type(key_type) else None


def _top_level_arg_index(raw_args: str, offset: int) -> int | None:
    start = 0
    depth = 0
    quote: str | None = None
    arg_index = 0
    for index, char in enumerate(raw_args):
        if quote is not None:
            if char == "\\":
                continue
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif char == "," and depth == 0:
            if start <= offset < index:
                return arg_index
            start = index + 1
            arg_index += 1
    if start <= offset <= len(raw_args):
        return arg_index
    return None


def _expression_start_offset(line: str) -> int:
    for pattern in [r"\b(?:if|assert|return)\s+", r"=\s*"]:
        match = re.search(pattern, line)
        if match:
            return match.end()
    return 0


def _local_expression(source: str, index: int) -> str:
    line_start = source.rfind("\n", 0, index) + 1
    line_end = source.find("\n", index)
    if line_end == -1:
        line_end = len(source)
    start = max(source.rfind(",", line_start, index), source.rfind("(", line_start, index), line_start - 1) + 1
    end_candidates = [pos for pos in [source.find(",", index, line_end), source.find(")", index, line_end)] if pos != -1]
    end = min(end_candidates) if end_candidates else line_end
    expr = source[start:end]
    mask = code_mask(expr)
    comment_start = next(
        (
            pos
            for pos, char in enumerate(expr)
            if char == "#" and (pos == 0 or mask[pos - 1])
        ),
        None,
    )
    return expr[:comment_start] if comment_start is not None else expr


def _inside_array_subscript(source: str, index: int, vars_for_line: dict[str, str]) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("[", line_start, index)
    if open_index == -1:
        return False
    close_index = source.find("]", index)
    if close_index == -1:
        return False
    root_match = re.search(r"((?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\s*$", source[line_start:open_index])
    if root_match is None:
        return False
    root = root_match.group(1)
    return indexed_value_type(infer_expr_type(root, vars_for_line)) is not None


def _inside_type_subscript(source: str, index: int) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("[", line_start, index)
    if open_index == -1:
        return False
    close_index = source.find("]", index)
    if close_index == -1:
        return False
    return bool(
        re.search(
            r"(?:u?int(?:\d+)?|bool|address|bytes\d*|Bytes|String|DynArray|HashMap)\s*$",
            source[line_start:open_index],
        )
    )


def _inside_attribute_access(source: str, start: int, end: int) -> bool:
    return (start > 0 and source[start - 1] == ".") or (end < len(source) and source[end] == ".")


def _struct_kwargs(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
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


def _cast_integer_arg_to_expected(
    value: str, expected_type: str | None, vars_for_line: dict[str, str], facts: SourceFacts
) -> str:
    if not is_integer_type(expected_type) or value.strip().startswith("convert("):
        return value
    actual_type = infer_expr_type(value, vars_for_line, facts)
    if not is_integer_type(actual_type) or _same_integer_signedness(actual_type, expected_type):
        return value
    return f"convert({value}, {normalize_type(expected_type or '')})"


def _same_integer_signedness(left: str | None, right: str | None) -> bool:
    return (_is_signed_integer_type(left) and _is_signed_integer_type(right)) or (
        _is_unsigned_integer_type(left) and _is_unsigned_integer_type(right)
    )


def _typed_range_loops(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY070", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = parse_source_facts(source)
    pattern = re.compile(r"^(\s*)for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+?):", re.MULTILINE)
    inferred_loop_vars: dict[int, dict[str, str]] = {}

    for match in pattern.finditer(source):
        iterable = match.group(3).strip()
        if ":" in source[match.start() : match.end()].split(" in ", 1)[0]:
            continue
        line = line_number(source, match.start())
        function_start = _function_start_at_line(facts, line)
        vars_for_line = facts.vars_at_line(line)
        if function_start is not None:
            vars_for_line.update(inferred_loop_vars.get(function_start, {}))
        var_type = _loop_var_type(iterable, vars_for_line, facts)
        if var_type is None:
            continue
        before = match.group(0)
        after = f"{match.group(1)}for {match.group(2)}: {var_type} in {iterable}:"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(Fix("VY070", line_number(source, match.start()), f"added {var_type} loop variable type", before, after))
        if function_start is not None:
            inferred_loop_vars.setdefault(function_start, {})[match.group(2)] = var_type

    return apply_edits(source, edits), fixes, []


def _function_start_at_line(facts: SourceFacts, line: int) -> int | None:
    for start in sorted(facts.function_vars):
        end = facts.function_ends.get(start, 10**9)
        if start <= line <= end:
            return start
    return None


def _loop_var_type(iterable: str, vars_for_line: dict[str, str], facts: SourceFacts) -> str | None:
    if iterable.startswith("range("):
        return _range_loop_var_type(iterable, vars_for_line)
    literal_type = _literal_list_element_type(iterable, vars_for_line, facts)
    if literal_type is not None:
        return literal_type
    iterable_type = vars_for_line.get(iterable)
    if iterable_type is None and re.fullmatch(r"self\.[A-Za-z_][A-Za-z0-9_]*", iterable):
        iterable_type = vars_for_line.get(iterable.removeprefix("self."))
    if iterable_type is None:
        iterable_type = infer_expr_type(iterable, vars_for_line, facts)
    return iterable_element_type(iterable_type)


def _literal_list_element_type(iterable: str, vars_for_line: dict[str, str], facts: SourceFacts) -> str | None:
    if not (iterable.startswith("[") and iterable.endswith("]")):
        return None
    args = split_top_level_args(iterable[1:-1])
    if not args:
        return None
    types = [infer_expr_type(arg, vars_for_line, facts) for arg in args]
    if any(type_name is None for type_name in types):
        return None
    clean_types = [type_name.strip() for type_name in types if type_name is not None]
    if len(set(clean_types)) == 1:
        return clean_types[0]
    normalized_types = [normalize_type(type_name) for type_name in clean_types]
    return normalized_types[0] if len(set(normalized_types)) == 1 else None


def _range_loop_var_type(iterable: str, vars_for_line: dict[str, str]) -> str:
    match = re.match(r"range\s*\((.*)\)\s*$", iterable)
    if match is None:
        return "uint256"
    args = split_top_level_args(match.group(1))
    if not args:
        return "uint256"
    positional = [arg for arg in args if not re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", arg)]
    if not positional:
        return "uint256"
    start_type = infer_expr_type(positional[0], vars_for_line)
    if len(positional) > 1 and is_integer_type(start_type) and not _literal_integer(positional[0]):
        return start_type
    bound = positional[1] if len(positional) > 1 else positional[0]
    bound_type = infer_expr_type(bound, vars_for_line)
    return bound_type if is_integer_type(bound_type) else "uint256"


def _range_bound(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY071", "VYD011", "VYD014"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\brange\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = source.find("(", match.start())
        close = find_matching(source, open_index)
        if close is None:
            continue
        raw_args = source[open_index + 1 : close]
        if "bound" in raw_args:
            continue
        args = split_top_level_args(raw_args)
        if args is None:
            continue
        if len(args) == 1:
            if not _literal_integer(args[0]) and _enabled("VYD014", config, context):
                diagnostics.append(
                    Diagnostic(
                        "VYD014",
                        line_number(source, match.start()),
                        "range(stop) has a runtime bound; add bound=... manually",
                    )
                )
            continue
        if len(args) != 2:
            continue
        if _literal_integer(args[0]) and _literal_integer(args[1]):
            continue
        bound = _infer_range_bound(args[0], args[1], _integer_constant_values(source))
        if bound is None:
            diagnostics.append(
                Diagnostic(
                    "VYD011",
                    line_number(source, match.start()),
                    "range(start, stop) has runtime bounds; add bound=... manually",
                )
            )
            continue
        edits.append(TextEdit(close, close, f", bound={bound}"))
        fixes.append(
            Fix(
                "VY071",
                line_number(source, match.start()),
                "added range bound keyword",
                f"range({raw_args})",
                f"range({raw_args}, bound={bound})",
            )
        )
    return apply_edits(source, edits), fixes, diagnostics


def _create_from_blueprint(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
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
        diagnostics.append(Diagnostic("VY080", line_number(source, match.start()), "create_from_blueprint default code_offset changed from 0 to 3"))
        if config.aggressive:
            edits.append(TextEdit(close, close, ", code_offset=0"))
            fixes.append(Fix("VY080", line_number(source, match.start()), "added code_offset=0 to preserve 0.3.x behavior", "", "code_offset=0"))
    return apply_edits(source, edits), fixes, diagnostics


def _nonreentrant(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
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
        diagnostics.append(Diagnostic("VYD002", line_number(source, first.start() if first else 0), "multiple named reentrancy locks found; 0.4.x uses a global lock"))
    if not _enabled("VY090", config, context):
        return source, fixes, diagnostics
    diagnostics.extend(Diagnostic("VY090", line_number(source, match.start()), "single named nonreentrant lock rewritten; review callback assumptions") for match in pattern.finditer(source))

    def repl(match: re.Match[str]) -> str:
        fixes.append(Fix("VY090", line_number(source, match.start()), "removed named nonreentrant lock", match.group(0), "@nonreentrant"))
        return "@nonreentrant"

    return pattern.sub(repl, source), fixes, diagnostics


def _sqrt(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY100", config, context):
        return source, [], []
    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for match in re.finditer(r"(?<!\.)\bsqrt\s*\(", source):
        if span_is_code(mask, match.start(), match.end()):
            edits.append(TextEdit(match.start(), match.start() + 4, "math.sqrt"))
            fixes.append(Fix("VY100", line_number(source, match.start()), "moved sqrt to math module", "sqrt", "math.sqrt"))
    next_source = apply_edits(source, edits)
    if edits and not re.search(r"^\s*import\s+math\s*$", next_source, re.MULTILINE):
        next_source = _insert_import(next_source, "import math\n")
        fixes.append(Fix("VY100", 1, "added math import", "", "import math"))
    return next_source, fixes, []


def _bitwise(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY110", "VY111", "VYD012"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    current = source
    if _enabled("VY110", config, context):
        for name, operator, unary in [
            ("bitwise_and", "&", False),
            ("bitwise_or", "|", False),
            ("bitwise_xor", "^", False),
            ("bitwise_not", "~", True),
        ]:
            current, new_fixes = _replace_builtin_call(current, name, operator, unary, "VY110")
            fixes.extend(new_fixes)
    if _any_enabled({"VY111", "VYD012"}, config, context):
        current, new_fixes, new_diagnostics = _replace_shift_builtin(current, config, context)
        fixes.extend(new_fixes)
        diagnostics.extend(new_diagnostics)
    return current, fixes, diagnostics


def _decimal_diagnostic(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VYD001", config, context):
        return source, [], []
    if re.search(r"\bdecimal\b", source) and not config.enable_decimals:
        return source, [], [Diagnostic("VYD001", 1, "decimal type is used; target compile may require --enable-decimals")]
    return source, [], []


def _prevrandao_diagnostic(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VYD010", config, context):
        return source, [], []
    diagnostics = [
        Diagnostic("VYD010", line_number(source, match.start()), "block.prevrandao signature changed in 0.4.0; review manually")
        for match in re.finditer(r"\bblock\.prevrandao\b", source)
    ]
    return source, [], diagnostics


def _missing_pragma_diagnostic(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VYD005", config, context):
        return source, [], []
    if infer_pragma(source) is None and config.source_version is None:
        return source, [], [Diagnostic("VYD005", 1, "source has no version pragma and no --source-version")]
    return source, [], []


def _read_left_operand(source: str, index: int) -> str:
    i = index - 1
    while i >= 0 and source[i].isspace():
        i -= 1
    if i >= 0 and source[i] == ")":
        open_index = _find_matching_open(source, i)
        if open_index is not None:
            return source[open_index : i + 1]
    if i >= 0 and source[i] == "]":
        open_index = _find_matching_open_bracket(source, i)
        if open_index is not None:
            start = _read_indexed_expression_start(source, open_index)
            return source[start : i + 1].replace("self.", "")
    end = i + 1
    while i >= 0 and re.match(r"[A-Za-z0-9_.$]", source[i]):
        i -= 1
    return source[i + 1 : end].replace("self.", "")


def _find_matching_open(source: str, close_index: int) -> int | None:
    depth = 0
    for i in range(close_index, -1, -1):
        if source[i] == ")":
            depth += 1
        elif source[i] == "(":
            depth -= 1
            if depth == 0:
                return i
    return None


def _find_matching_open_bracket(source: str, close_index: int) -> int | None:
    depth = 0
    for i in range(close_index, -1, -1):
        if source[i] == "]":
            depth += 1
        elif source[i] == "[":
            depth -= 1
            if depth == 0:
                return i
    return None


def _read_indexed_expression_start(source: str, open_index: int) -> int:
    i = open_index - 1
    while i >= 0 and re.match(r"[A-Za-z0-9_.$]", source[i]):
        i -= 1
    return i + 1


def _read_right_operand(source: str, index: int) -> str:
    i = index
    while i < len(source) and source[i].isspace():
        i += 1
    start = i
    if source.startswith(("staticcall ", "extcall "), i):
        i = source.find(" ", i) + 1
        while i < len(source) and source[i].isspace():
            i += 1
    while i < len(source) and re.match(r"[A-Za-z0-9_.$\[]", source[i]):
        i += 1
    if i < len(source) and source[i] == "(":
        close = find_matching(source, i)
        if close is not None:
            i = close + 1
    return source[start:i].replace("self.", "")


def _lhs_declared_type(line: str) -> str | None:
    match = re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*([^=]+)=", line)
    return match.group(1).strip() if match else None


def _lhs_assigned_type(line: str, vars_for_line: dict[str, str]) -> str | None:
    match = re.match(r"\s*(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)(\s*\[[^=]+\])?\s*(?:[-+*/%]?=)", line)
    if not match:
        return None
    type_name = vars_for_line.get(match.group(1))
    if match.group(2):
        return indexed_value_type(type_name)
    return type_name


def _is_unsigned_integer_type(type_name: str | None) -> bool:
    if type_name is None:
        return False
    wrapper = re.match(r"(?:public|constant|immutable)\((.+)\)$", type_name.strip())
    if wrapper:
        type_name = wrapper.group(1).strip()
    return bool(re.fullmatch(r"uint(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?", type_name))


def _is_signed_integer_type(type_name: str | None) -> bool:
    if type_name is None:
        return False
    wrapper = re.match(r"(?:public|constant|immutable)\((.+)\)$", type_name.strip())
    if wrapper:
        type_name = wrapper.group(1).strip()
    return bool(re.fullmatch(r"int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?", type_name))


def _inside_convert_call(source: str, index: int) -> bool:
    prefix = source[max(0, index - 24) : index]
    return bool(re.search(r"\bconvert\s*\([^,\n]*$", prefix))


def _inside_range_header(source: str, index: int) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    prefix = source[line_start:index]
    return bool(re.search(r"\bfor\s+[A-Za-z_][A-Za-z0-9_]*(?::[^:]+)?\s+in\s+range\s*\([^)]*$", prefix))


def _integerish_expression(expr: str, vars_for_line: dict[str, str], facts=None) -> bool:
    expr = expr.split("#", 1)[0]
    if facts is not None:
        expr = _replace_integerish_subexpressions(expr, vars_for_line, facts)
    expr = expr.replace("self.", "")
    expr = re.sub(r"\b(?:block\.(?:timestamp|number|difficulty|basefee|prevhash)|chain\.id|msg\.value)\b", "1", expr)
    expr = re.sub(r"^\s*(?:return|assert)\s+", "", expr)
    if "=" in expr:
        expr = expr.rsplit("=", 1)[-1]
    if re.search(r"\bdecimal\b|\d+\.\d+", expr):
        return False
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)
    if not tokens:
        return bool(re.search(r"\d", expr))
    typed = False
    for token in tokens:
        if token in {
            "convert",
            "max",
            "min",
            "pow_mod256",
            "unsafe_add",
            "unsafe_div",
            "unsafe_mul",
            "unsafe_sub",
            "uint256",
            "uint128",
            "uint64",
            "uint8",
        }:
            typed = True
            continue
        token_type = vars_for_line.get(token)
        if token_type is None:
            if token.isupper():
                typed = True
                continue
            return False
        if not is_integer_type(token_type):
            return False
        typed = True
    return typed


def _replace_integerish_subexpressions(expr: str, vars_for_line: dict[str, str], facts) -> str:
    edits: list[TextEdit] = []
    for pattern in [
        r"(?:staticcall|extcall)\s+(?:[A-Za-z_][A-Za-z0-9_]*\s*\([^()\n]*(?:\([^()\n]*\)[^()\n]*)*\)|(?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\s*\(",
        r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])+(?:\.[A-Za-z_][A-Za-z0-9_]*)?",
        r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*",
    ]:
        for match in re.finditer(pattern, expr):
            end = match.end()
            if expr[end - 1] == "(":
                close = find_matching(expr, end - 1)
                if close is None:
                    continue
                end = close + 1
            candidate = expr[match.start() : end]
            if is_integer_type(infer_expr_type(candidate, vars_for_line, facts)):
                edits.append(TextEdit(match.start(), end, "1"))
    return apply_edits(expr, _innermost_non_overlapping(edits, [Fix("VY050", 1, "", "", "") for _ in edits])[0])


def _multiline_integer_division_context(source: str, line_start: int) -> bool:
    prefix = source[:line_start].splitlines()[-8:]
    block = "\n".join(prefix)
    if re.search(r"\bdecimal\b|\d+\.\d+", block):
        return False
    return bool(re.search(r"return\s*\($|:\s*u?int(?:\d+)?\s*=\s*\($", block, re.MULTILINE))


def _multiline_integer_division_assignment_context(
    source: str, line_start: int, vars_for_line: dict[str, str]
) -> bool:
    line_end = source.find("\n", line_start)
    if line_end == -1:
        line_end = len(source)
    if source[line_start:line_end].strip() != "/":
        return False
    prefix = source[:line_start].splitlines()[-8:]
    block = "\n".join(prefix)
    if re.search(r"\bdecimal\b|\d+\.\d+", block):
        return False
    for line in reversed(prefix):
        match = re.match(r"\s*((?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\s*=\s*\(\s*$", line)
        if match is None:
            continue
        target_type = infer_expr_type(match.group(1), vars_for_line)
        return is_integer_type(target_type)
    return False


def _infer_range_bound(start: str, stop: str, values: dict[str, int] | None = None) -> str | None:
    start = start.strip()
    stop = stop.strip()
    values = values or {}
    escaped = re.escape(start)
    plus_match = re.fullmatch(rf"{escaped}\s*\+\s*([A-Za-z_][A-Za-z0-9_]*|(?:\d|_)+)", stop)
    if plus_match:
        return _range_bound_literal(plus_match.group(1), values)
    minus_match = re.fullmatch(rf"{escaped}\s*-\s*([A-Za-z_][A-Za-z0-9_]*|(?:\d|_)+)", stop)
    if minus_match:
        return _range_bound_literal(minus_match.group(1), values)
    return None


def _range_bound_literal(value: str, values: dict[str, int]) -> str | None:
    value = value.strip()
    if _literal_integer(value):
        return value
    constant = values.get(value)
    if constant is None or constant < 0:
        return None
    return str(constant)


def _literal_integer(value: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:\d|_)+\s*", value))


def _insert_import(source: str, line: str) -> str:
    lines = source.splitlines(keepends=True)
    insert_at = 0
    while insert_at < len(lines) and (
        lines[insert_at].startswith("#pragma")
        or lines[insert_at].startswith("# @version")
        or lines[insert_at].strip() == ""
        or lines[insert_at].startswith('"""')
    ):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].startswith("import "):
        insert_at += 1
    lines.insert(insert_at, line)
    return "".join(lines)


def _remove_constructor_decorators(
    source: str,
    decorators_to_remove: set[str],
    rule: str,
    message: str,
    add_deploy: bool = False,
) -> tuple[str, list[Fix], list[Fix]]:
    lines = source.splitlines(keepends=True)
    fixes: list[Fix] = []
    insertions: list[Fix] = []
    out = list(lines)
    offset = 0
    for index, line in enumerate(lines):
        if not re.match(r"\s*def\s+__init__\s*\(", line):
            continue
        start = index
        while start > 0 and re.match(r"\s*@[A-Za-z_][A-Za-z0-9_]*(?:\(.*\))?\s*(?:#.*)?$", lines[start - 1]):
            start -= 1
        decorators = [decor.strip() for decor in lines[start:index]]
        insert_at = start + offset
        remove_indices: list[int] = []
        has_deploy = any(decor.startswith("@deploy") for decor in decorators)
        for rel, decor in enumerate(decorators):
            decor_name = decor.split("(", 1)[0].split("#", 1)[0].strip()
            if decor_name in decorators_to_remove:
                remove_indices.append(start + rel)
        for original_index in sorted(remove_indices, reverse=True):
            before = out[original_index + offset].rstrip("\n")
            del out[original_index + offset]
            offset -= 1
            fixes.append(Fix(rule, original_index + 1, message, before, ""))
        if add_deploy and not has_deploy:
            indent = re.match(r"(\s*)", line).group(1)
            out.insert(insert_at, f"{indent}@deploy\n")
            offset += 1
            insertions.append(Fix(rule, index + 1, "added @deploy to constructor", "", f"{indent}@deploy"))
    return "".join(out), fixes, insertions


def _nested_under_config_path(path: Path, config: Config) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in config.paths:
        try:
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return resolved.parent != root.resolve()
    return path.parent != Path(".")


def _replace_identifier_expr(
    source: str,
    before: str,
    after: str,
    rule: str,
    message: str,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(rf"(?<![\w.]){re.escape(before)}(?![\w.])", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(Fix(rule, line_number(source, match.start()), message, before, after))
    return apply_edits(source, edits), fixes


def _remove_unary_plus(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"(?P<prefix>(?:^|[=(,\[\{]\s*))\+(?P<expr>[A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE)
    for match in pattern.finditer(source):
        start = match.start("expr") - 1
        if not span_is_code(mask, start, match.end("expr")):
            continue
        edits.append(TextEdit(start, start + 1, ""))
        fixes.append(Fix("VY230", line_number(source, start), "removed disabled unary plus", "+", ""))
    return apply_edits(source, edits), fixes


def _byte_string_literal_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    patterns = [
        (re.compile(r"\bBytes\s*\[[^\]]+\]\s*=\s*(?=\")"), "byte arrays require byte literals such as b\"...\""),
        (re.compile(r"\bString\s*\[[^\]]+\]\s*=\s*(?=b\")"), "strings require string literals, not byte literals"),
    ]
    for pattern, message in patterns:
        for match in pattern.finditer(source):
            if span_is_code(mask, match.start(), match.end()):
                diagnostics.append(Diagnostic("VYD210", line_number(source, match.start()), message))
    return diagnostics


def _reserved_value_parameter_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for match in re.finditer(r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<args>[^)]*)\)", source, re.MULTILINE):
        args = split_top_level_args(match.group("args"))
        if args is None:
            continue
        for arg in args:
            name = arg.split(":", 1)[0].split("=", 1)[0].strip()
            if name == "value":
                diagnostics.append(Diagnostic("VYD211", line_number(source, match.start()), "function parameter name 'value' became reserved; rename it and update references"))
                break
    return diagnostics


def _slice_uint256_diagnostics(source: str) -> list[Diagnostic]:
    facts = parse_source_facts(source)
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bslice\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None or len(args) < 3:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        if not _is_uint256_expr(args[1], vars_for_line) or not _is_uint256_expr(args[2], vars_for_line):
            diagnostics.append(Diagnostic("VYD212", line_number(source, match.start()), "slice start and length must be uint256"))
    return diagnostics


def _len_uint256_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    pattern = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?P<typ>i?nt(?:8|16|32|64|128|256)?)\s*=\s*len\s*\(", re.MULTILINE)
    for match in pattern.finditer(source):
        if match.group("typ") != "uint256":
            diagnostics.append(Diagnostic("VYD213", line_number(source, match.start()), "len() returns uint256; update the receiving type"))
    return diagnostics


def _call_kwarg_uint256_diagnostics(source: str) -> list[Diagnostic]:
    facts = parse_source_facts(source)
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    for match in re.finditer(r"(?<![\w.])(?:[A-Za-z_][A-Za-z0-9_]*\([^)\n]*\)|(?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        for arg in args:
            name, sep, value = arg.partition("=")
            if not sep or name.strip() not in {"gas", "value"}:
                continue
            if not _is_uint256_expr(value, vars_for_line):
                diagnostics.append(Diagnostic("VYD214", line_number(source, match.start()), f"external-call {name.strip()} kwarg must be uint256"))
    return diagnostics


def _is_uint256_expr(expr: str, vars_for_line: dict[str, str]) -> bool:
    expr = expr.strip()
    if _literal_integer(expr):
        return True
    expr_type = infer_expr_type(expr, vars_for_line)
    return expr_type == "uint256"


def _replace_numeric_not(
    source: str,
    config: Config,
    context: MigrationContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"\bnot\s+((?:self\.)?[A-Za-z_][A-Za-z0-9_]*)")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        line = line_number(source, match.start())
        vars_for_line = facts.vars_at_line(line)
        expr = match.group(1)
        expr_type = facts.storage_vars.get(expr[5:]) if expr.startswith("self.") else None
        expr_type = expr_type or infer_expr_type(expr, vars_for_line)
        if expr_type is None:
            if _enabled("VYD013", config, context):
                diagnostics.append(Diagnostic("VYD013", line, f"cannot infer whether 'not {expr}' is numeric or boolean"))
            continue
        if not is_integer_type(expr_type):
            continue
        replacement = f"{expr} == 0"
        if not _enabled("VY231", config, context):
            continue
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(Fix("VY231", line, "changed numeric boolean negation to equality check", match.group(0), replacement))
    return apply_edits(source, edits), fixes, diagnostics


def _rewrite_legacy_event_declarations(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*event\s*\(\s*\{", re.MULTILINE)
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_brace = source.rfind("{", match.start(), match.end())
        close_brace = find_matching(source, open_brace, "{", "}")
        if close_brace is None:
            continue
        close_paren = source.find(")", close_brace)
        if close_paren == -1:
            continue
        fields = split_top_level_args(source[open_brace + 1 : close_brace])
        if fields is None:
            continue
        lines = [f"{match.group(1)}event {match.group(2)}:"]
        child_indent = f"{match.group(1)}    "
        ok = True
        for field in fields:
            if ":" not in field:
                ok = False
                break
            name, typ = field.split(":", 1)
            lines.append(f"{child_indent}{name.strip()}: {typ.strip()}")
        if not ok:
            continue
        replacement = "\n".join(lines)
        edits.append(TextEdit(match.start(), close_paren + 1, replacement))
        fixes.append(Fix("VY203", line_number(source, match.start()), "changed legacy event declaration", source[match.start() : close_paren + 1], replacement))
    return apply_edits(source, edits), fixes


def _collect_event_fields(source: str) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {}
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(?P<indent>\s*)event\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:#.*)?$", line)
        if match is None:
            index += 1
            continue
        indent = len(match.group("indent"))
        fields: list[str] = []
        index += 1
        while index < len(lines):
            child = lines[index]
            if not child.strip() or child.lstrip().startswith("#"):
                index += 1
                continue
            child_indent = len(child) - len(child.lstrip())
            if child_indent <= indent:
                break
            field = re.match(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:", child)
            if field is not None:
                fields.append(field.group("name"))
            index += 1
        if fields:
            events[match.group("name")] = fields
    return events


def _rewrite_map_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    last_end = -1
    for match in re.finditer(r"\bmap\s*\(", source):
        if match.start() < last_end or not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None or len(args) != 2:
            continue
        replacement = f"HashMap[{args[0].strip()}, {args[1].strip()}]"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(Fix("VY205", line_number(source, match.start()), "changed legacy map type to HashMap", source[match.start() : close + 1], replacement))
        last_end = close + 1
    return apply_edits(source, edits), fixes


def _replace_call_keyword(
    source: str,
    call_name: str,
    before: str,
    after: str,
    rule: str,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(rf"\b{re.escape(call_name)}\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = source[match.end() : close]
        keyword_match = re.search(rf"(?<!\w){re.escape(before)}\s*=", args)
        if keyword_match is None:
            continue
        start = match.end() + keyword_match.start()
        end = start + len(before)
        edits.append(TextEdit(start, end, after))
        fixes.append(Fix(rule, line_number(source, start), f"renamed {call_name} keyword {before}", before, after))
    return apply_edits(source, edits), fixes


def _replace_assert_modifiable(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bassert_modifiable\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None or len(args) != 1:
            continue
        replacement = f"assert {args[0].strip()}"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(Fix("VY208", line_number(source, match.start()), "replaced assert_modifiable builtin", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes


def _remove_call_keyword_arg(
    source: str,
    call_name: str,
    keyword: str,
    value: str | None,
    rule: str,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(rf"\b{re.escape(call_name)}\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = source.find("(", match.start())
        close = find_matching(source, open_index)
        if close is None:
            continue
        raw_args = source[open_index + 1 : close]
        args = split_top_level_args(raw_args)
        if args is None:
            continue
        kept: list[str] = []
        removed: str | None = None
        for arg in args:
            name, sep, raw_value = arg.partition("=")
            if sep and name.strip() == keyword and (value is None or raw_value.strip() == value):
                removed = arg
                continue
            kept.append(arg)
        if removed is None:
            continue
        replacement = f"{call_name}({', '.join(kept)})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(Fix(rule, line_number(source, match.start()), f"removed redundant {call_name} {keyword} keyword", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes


def _replace_builtin_call(source: str, name: str, operator: str, unary: bool, rule: str) -> tuple[str, list[Fix]]:
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match in re.finditer(rf"\b{re.escape(name)}\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None:
            continue
        if unary and len(args) == 1:
            replacement = f"(~{args[0]})"
        elif not unary and len(args) == 2:
            replacement = f"({args[0]} {operator} {args[1]})"
        else:
            continue
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(Fix(rule, line_number(source, match.start()), f"replaced {name} builtin", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes


def _replace_shift_builtin(
    source: str,
    config: Config,
    context: MigrationContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    current = source
    all_fixes: list[Fix] = []
    all_diagnostics: list[Diagnostic] = []
    while True:
        mask = code_mask(current)
        constant_values = _integer_constant_values(current)
        facts = parse_source_facts(current)
        fixes: list[Fix] = []
        diagnostics: list[Diagnostic] = []
        edits: list[TextEdit] = []
        for match in re.finditer(r"\bshift\s*\(", current):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            close = find_matching(current, current.find("(", match.start()))
            if close is None:
                continue
            args = split_top_level_args(current[match.end() : close])
            if args is None or len(args) != 2:
                continue
            value = args[0].strip()
            shift_by = args[1].strip()
            negative = re.fullmatch(r"-\s*((?:\d|_)+)", shift_by)
            negative_constant = re.fullmatch(r"-\s*([A-Za-z_][A-Za-z0-9_]*)", shift_by)
            negative_expr = re.fullmatch(r"-\s*(.+)", shift_by)
            positive = re.fullmatch(r"\+?\s*((?:\d|_)+)", shift_by)
            positive_constant = re.fullmatch(r"\+?\s*([A-Za-z_][A-Za-z0-9_]*)", shift_by)
            convert_constant = re.fullmatch(
                r"convert\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*u?int(?:\d+)?\s*\)",
                shift_by,
            )
            positive_convert = re.fullmatch(r"convert\s*\((.+),\s*int128\s*\)", shift_by)
            if negative is not None:
                replacement = f"({value} >> {negative.group(1)})"
            elif negative_constant is not None and negative_constant.group(1) in constant_values:
                amount = -constant_values[negative_constant.group(1)]
                operator = "<<" if amount >= 0 else ">>"
                replacement = f"({value} {operator} {abs(amount)})"
            elif negative_expr is not None:
                vars_for_line = facts.vars_at_line(line_number(current, match.start()))
                replacement = f"({value} >> ({_unsigned_shift_amount_expr(negative_expr.group(1).strip(), vars_for_line)}))"
            elif positive is not None:
                replacement = f"({value} << {positive.group(1)})"
            elif positive_constant is not None and positive_constant.group(1) in constant_values:
                amount = constant_values[positive_constant.group(1)]
                operator = "<<" if amount >= 0 else ">>"
                replacement = f"({value} {operator} {abs(amount)})"
            elif convert_constant is not None and convert_constant.group(1) in constant_values:
                amount = constant_values[convert_constant.group(1)]
                operator = "<<" if amount >= 0 else ">>"
                replacement = f"({value} {operator} {abs(amount)})"
            elif positive_convert is not None and not positive_convert.group(1).lstrip().startswith("-"):
                replacement = f"({value} << convert({positive_convert.group(1).strip()}, uint256))"
            else:
                if _enabled("VYD012", config, context):
                    diagnostics.append(
                        Diagnostic(
                            "VYD012",
                            line_number(current, match.start()),
                            "shift() with non-literal amount needs manual << or >> review",
                        )
                    )
                continue
            if not _enabled("VY111", config, context):
                continue
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(
                Fix(
                    "VY111",
                    line_number(current, match.start()),
                    "replaced shift builtin",
                    current[match.start() : close + 1],
                    replacement,
                )
            )
        all_diagnostics.extend(diagnostics)
        if not edits:
            return current, all_fixes, all_diagnostics
        selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
        all_fixes.extend(selected_fixes)
        current = apply_edits(current, selected_edits)


def _unsigned_shift_amount_expr(expr: str, vars_for_line: dict[str, str]) -> str:
    return re.sub(
        r"\bconvert\s*\(([^,\n]+),\s*int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?\s*\)",
        lambda match: _unsigned_shift_convert_replacement(match.group(1).strip(), vars_for_line),
        expr,
    )


def _unsigned_shift_convert_replacement(expr: str, vars_for_line: dict[str, str]) -> str:
    if _unsigned_integer_expression(expr, vars_for_line):
        return f"({expr})" if re.search(r"[-+*/%<>=|&]", expr) else expr
    return f"convert({expr}, uint256)"


def _unsigned_integer_expression(expr: str, vars_for_line: dict[str, str]) -> bool:
    identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr)
    if not identifiers:
        return _integerish_expression(expr, vars_for_line)
    return all(_is_unsigned_integer_type(infer_expr_type(identifier, vars_for_line)) for identifier in identifiers)


def _integer_constant_values(source: str) -> dict[str, int]:
    values: dict[str, int] = {}
    constant_re = re.compile(
        r"^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\s*\([^#\n=]+\)\s*=\s*(?P<expr>[^\n#]+)",
        re.MULTILINE,
    )
    mask = code_mask(source)
    for match in constant_re.finditer(source):
        if span_is_code(mask, match.start(), match.end()):
            value = _eval_integer_constant_expr(match.group("expr"), values)
            if value is not None:
                values[match.group("name")] = value
    return values
