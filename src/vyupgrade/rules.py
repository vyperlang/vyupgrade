from __future__ import annotations

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
    "VY020": RuleChange(VyperVersion(0, 4, 0)),
    "VY030": RuleChange(VyperVersion(0, 4, 0)),
    "VY040": RuleChange(VyperVersion(0, 4, 0)),
    "VY041": RuleChange(VyperVersion(0, 4, 0)),
    "VY042": RuleChange(VyperVersion(0, 4, 0)),
    "VY050": RuleChange(VyperVersion(0, 4, 0)),
    "VY051": RuleChange(VyperVersion(0, 4, 0)),
    "VY052": RuleChange(VyperVersion(0, 4, 0)),
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
        _legacy_builtin_calls,
        _not_in_comparator,
        _legacy_constructor_locks,
        _pre_04_expression_rewrites,
        _constructor_deploy,
        _abi_builtins,
        _legacy_constants,
        _interface_imports,
        _absolute_relative_imports(path),
        _enum_to_flag,
        _range_bound,
        _typed_range_loops,
        _external_call_keywords,
        _external_call_subscripts,
        _integer_division,
        _mixed_signed_unsigned_arithmetic,
        _redundant_integer_convert,
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
        if target != "uint256" or not re.search(r"[-+*/%]", expr):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        if not _integerish_expression(expr, vars_for_line):
            continue
        replacement = f"({expr})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(Fix("VY051", line_number(source, match.start()), "removed redundant uint256 convert around integer expression", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes, []


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
    call_matches: list[tuple[int, int, str, str, str | None]] = []
    variable_call_re = re.compile(
        r"(?<![\w.])(?P<target>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\("
    )
    call_matches.extend(_interface_cast_call_matches(source, facts.interfaces))
    call_matches.extend(
        (match.start(), match.end(), match.group("target"), match.group("method"), None)
        for match in variable_call_re.finditer(source)
    )

    for start, end, target, method, cast_type in sorted(call_matches):
        if not span_is_code(mask, start, end):
            continue
        prefix = source[max(0, start - 16) : start]
        if re.search(r"\b(?:extcall|staticcall)\s+$", prefix):
            continue
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
        edits.append(TextEdit(start, start, keyword + " "))
        fixes.append(Fix(rule, line_number(source, start), f"added {keyword} to {mutability} external call", source[start:end].rstrip(), keyword + " " + source[start:end].rstrip()))

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
                if (
                    _inside_convert_call(source, start)
                    or _inside_range_header(source, start)
                    or _inside_type_subscript(source, start)
                    or _signed_internal_call_arg_target_type(source, start, name, facts) is not None
                    or _signed_subscript_key_target_type(source, start, name, vars_for_line) is not None
                    or not _signed_name_has_unsigned_context(source, start, lhs_type, vars_for_line)
                ):
                    continue
                replacement = f"convert({name}, uint256)"
                edits.append(TextEdit(start, start + len(name), replacement))
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
                if _inside_convert_call(source, start) or _inside_range_header(source, start):
                    continue
                target_type = _signed_comparison_target_type(
                    _local_expression(source, start), name, vars_for_line
                ) or _signed_internal_call_arg_target_type(
                    source, start, name, facts
                ) or _signed_subscript_key_target_type(source, start, name, vars_for_line)
                if target_type is None:
                    continue
                replacement = f"convert({name}, {target_type})"
                edits.append(TextEdit(start, start + len(name), replacement))
                fixes.append(
                    Fix(
                        "VY052",
                        line_no,
                        "converted unsigned loop variable in signed comparison",
                        name,
                        replacement,
                    )
                )
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


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
    return source[start:end]


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
                replacement_inner = _ordered_struct_args(raw_inner, field_order)
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


def _ordered_struct_args(raw_inner: str, field_order: list[str]) -> str | None:
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
        return _ordered_kwarg_string(pairs, field_order)

    args = split_top_level_args(_strip_arg_comments(raw_inner))
    if args is None:
        return None
    pairs = []
    for arg in args:
        pair = _split_struct_pair(arg, "=")
        if pair is None:
            return None
        pairs.append(pair)
    ordered = _ordered_kwarg_string(pairs, field_order)
    return ordered if ordered != ", ".join(arg.strip() for arg in args) else None


def _split_struct_pair(raw: str, sep: str) -> tuple[str, str] | None:
    if sep == "=":
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)\Z", raw, re.DOTALL)
    else:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)\Z", raw, re.DOTALL)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def _ordered_kwarg_string(pairs: list[tuple[str, str]], field_order: list[str]) -> str:
    by_name = dict(pairs)
    ordered_names = [name for name in field_order if name in by_name]
    ordered_names.extend(name for name, _value in pairs if name not in field_order)
    return ", ".join(f"{name}={by_name[name]}" for name in ordered_names)


def _typed_range_loops(source: str, config: Config, context: MigrationContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY070", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = parse_source_facts(source)
    pattern = re.compile(r"^(\s*)for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+?):", re.MULTILINE)

    for match in pattern.finditer(source):
        iterable = match.group(3).strip()
        if ":" in source[match.start() : match.end()].split(" in ", 1)[0]:
            continue
        if iterable.startswith("range("):
            var_type = _range_loop_var_type(iterable, facts.vars_at_line(line_number(source, match.start())))
        else:
            vars_for_line = facts.vars_at_line(line_number(source, match.start()))
            iterable_name = iterable.replace("self.", "")
            var_type = iterable_element_type(vars_for_line.get(iterable_name) or infer_expr_type(iterable_name, vars_for_line))
        if var_type is None:
            continue
        before = match.group(0)
        after = f"{match.group(1)}for {match.group(2)}: {var_type} in {iterable}:"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(Fix("VY070", line_number(source, match.start()), f"added {var_type} loop variable type", before, after))

    return apply_edits(source, edits), fixes, []


def _range_loop_var_type(iterable: str, vars_for_line: dict[str, str]) -> str:
    match = re.match(r"range\s*\((.*)\)\s*$", iterable)
    if match is None:
        return "uint256"
    args = split_top_level_args(match.group(1))
    if not args:
        return "uint256"
    bound = args[1] if len(args) > 1 else args[0]
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
        bound = _infer_range_bound(args[0], args[1])
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
    end = i + 1
    while i >= 0 and re.match(r"[A-Za-z0-9_.$\[\]]", source[i]):
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
        if token in {"convert", "max", "min", "unsafe_add", "unsafe_sub", "uint256", "uint128", "uint64", "uint8"}:
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


def _infer_range_bound(start: str, stop: str) -> str | None:
    start = start.strip()
    stop = stop.strip()
    escaped = re.escape(start)
    plus_match = re.fullmatch(rf"{escaped}\s*\+\s*((?:\d|_)+)", stop)
    if plus_match:
        return plus_match.group(1)
    minus_match = re.fullmatch(rf"{escaped}\s*-\s*((?:\d|_)+)", stop)
    if minus_match:
        return minus_match.group(1)
    return None


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
            positive = re.fullmatch(r"\+?\s*((?:\d|_)+)", shift_by)
            if negative is not None:
                replacement = f"({value} >> {negative.group(1)})"
            elif positive is not None:
                replacement = f"({value} << {positive.group(1)})"
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
