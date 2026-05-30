from __future__ import annotations

import re
from collections.abc import Iterator

from ..analysis import SourceFacts, infer_expr_type, parse_source_facts
from ..models import Config, Diagnostic, Fix
from ..rule_helpers import (
    function_body_span as _function_body_span,
    find_matching_open as _find_matching_open,
    insert_import as _insert_import,
    is_attribute_name as _is_attribute_name,
    is_keyword_argument_name as _is_keyword_argument_name,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    line_offsets as _line_offsets,
    literal_integer as _literal_integer,
    pre_021_context as _pre_021_context,
    remove_constructor_decorators as _remove_constructor_decorators,
)
from ..rule_registry import any_enabled as _any_enabled, is_enabled as _enabled
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
    split_top_level_arg_spans,
    split_top_level_args,
    span_is_code,
)
from ..versions import MigrationContext, VyperVersion


IMPORT_RENAMES = {
    "ERC20": "IERC20",
    "ERC20Detailed": "IERC20Detailed",
    "ERC165": "IERC165",
    "ERC4626": "IERC4626",
    "ERC721": "IERC721",
    "ERC1155": "IERC1155",
}


def _pragma(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY001", config, context):
        return source, [], []
    fixes: list[Fix] = []
    mask = code_mask(source)
    pattern = re.compile(
        r"^([ \t]*)#[ \t]*(?:@version|pragma[ \t]+version)[ \t]+(.+?)[ \t]*$", re.MULTILINE
    )
    matched = False

    def repl(match: re.Match[str]) -> str:
        nonlocal matched
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        matched = True
        before = match.group(0)
        after = f"{match.group(1)}#pragma version {config.target_version}"
        if before != after:
            fixes.append(
                Fix(
                    "VY001",
                    line_number(source, match.start()),
                    "modernized version pragma",
                    before,
                    after,
                )
            )
        return after

    rewritten = pattern.sub(repl, source)
    if matched:
        return rewritten, fixes, []
    pragma = f"#pragma version {config.target_version}\n"
    fixes.append(Fix("VY001", 1, "added version pragma", "", pragma.rstrip()))
    return pragma + rewritten, fixes, []


def _legacy_decorators(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY201", config, context):
        return source, [], []
    fixes: list[Fix] = []
    replacements = {
        "public": "external",
        "private": "internal",
        "constant": "view",
    }
    mask = code_mask(source)
    pattern = re.compile(r"^([ \t]*)@(public|private|constant)([ \t]*(?:#.*)?$)", re.MULTILINE)

    def repl(match: re.Match[str]) -> str:
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        before = match.group(0)
        after = f"{match.group(1)}@{replacements[match.group(2)]}{match.group(3)}"
        fixes.append(
            Fix(
                "VY201",
                line_number(source, match.start()),
                "renamed legacy decorator",
                before,
                after,
            )
        )
        return after

    return pattern.sub(repl, source), fixes, []


def _legacy_type_units(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY202", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    type_re = re.compile(
        r"\b(u?int(?:8|16|32|64|128|256)?|decimal)\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)"
    )
    for match in type_re.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        before = match.group(0)
        after = match.group(1)
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY202",
                line_number(source, match.start()),
                "removed legacy type unit",
                before,
                after,
            )
        )
    for edit in _legacy_timestamp_type_edits(source, mask):
        edits.append(edit)
        fixes.append(
            Fix(
                "VY202",
                line_number(source, edit.start),
                "replaced legacy timestamp type",
                "timestamp",
                "uint256",
            )
        )
    return apply_edits(source, edits), fixes, []


def _legacy_timestamp_type_edits(source: str, mask: list[bool]) -> list[TextEdit]:
    edits: list[TextEdit] = []
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        code = line.split("#", 1)[0]
        if "timestamp" not in code:
            offset += len(raw_line)
            continue
        spans: list[tuple[int, int]] = []
        if re.match(r"\s*def\b", code):
            spans.append((0, len(code)))
        else:
            colon = code.find(":")
            if colon != -1:
                assignment = code.find("=", colon + 1)
                end = assignment if assignment != -1 else len(code)
                spans.append((colon + 1, end))
        for start, end in spans:
            for match in re.finditer(r"\btimestamp\b", code[start:end]):
                absolute_start = offset + start + match.start()
                absolute_end = offset + start + match.end()
                if absolute_start > 0 and source[absolute_start - 1] == ".":
                    continue
                if span_is_code(mask, absolute_start, absolute_end):
                    edits.append(TextEdit(absolute_start, absolute_end, "uint256"))
        offset += len(raw_line)
    return edits


def _legacy_events(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
            fixes.append(
                Fix(
                    "VY204",
                    line_number(current, match.start()),
                    "changed legacy log call to statement",
                    match.group(0),
                    replacement,
                )
            )
        current = apply_edits(current, edits)
    return current, fixes, []


def _event_kwargs(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
    for raw_line in raw_args.splitlines():
        line = raw_line
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


def _has_line_comment(text: str) -> bool:
    for line in text.splitlines():
        mask = code_mask(line)
        if any(char == "#" and (index == 0 or mask[index - 1]) for index, char in enumerate(line)):
            return True
    return False


def _legacy_maps_and_interfaces(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    current = source
    if _enabled("VY205", config, context):
        current, map_fixes = _rewrite_map_types(current)
        fixes.extend(map_fixes)
    if _enabled("VY206", config, context):
        legacy_source = _pre_021_context(context)
        if legacy_source:
            current, address_interface_fixes = _rewrite_legacy_address_interface_types(
                current, config, context
            )
            fixes.extend(address_interface_fixes)
        pattern = re.compile(
            r"^([ \t]*)contract[ \t]+([A-Za-z_][A-Za-z0-9_]*)(?:[ \t]*\([ \t]*\))?[ \t]*:",
            re.MULTILINE,
        )
        mask = code_mask(current)

        def repl(match: re.Match[str]) -> str:
            if not _line_match_starts_outside_string(current, mask, match.start()):
                return match.group(0)
            before = match.group(0)
            after = f"{match.group(1)}interface {match.group(2)}:"
            fixes.append(
                Fix(
                    "VY206",
                    line_number(current, match.start()),
                    "changed contract interface declaration",
                    before,
                    after,
                )
            )
            return after

        current = pattern.sub(repl, current)
        pattern = re.compile(
            r"^([ \t]*def[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*\([^#\n]*\)[ \t]*(?:->[ \t]*[^:#\n]+)?[ \t]*:[ \t]*)(constant|modifying)([ \t]*(?:#.*)?$)",
            re.MULTILINE,
        )
        mutability_mask = code_mask(current)

        def mutability_repl(match: re.Match[str]) -> str:
            if not _line_match_starts_outside_string(current, mutability_mask, match.start()):
                return match.group(0)
            before = match.group(0)
            after_keyword = "view" if match.group(2) == "constant" else "nonpayable"
            after = f"{match.group(1)}{after_keyword}{match.group(3)}"
            fixes.append(
                Fix(
                    "VY206",
                    line_number(current, match.start()),
                    "changed legacy interface mutability",
                    before,
                    after,
                )
            )
            return after

        current = pattern.sub(mutability_repl, current)
        if legacy_source:
            current, payable_fixes = _rewrite_value_call_interface_methods_payable(current)
            fixes.extend(payable_fixes)
            current, storage_fixes = _rewrite_legacy_interface_storage_vars(current)
            fixes.extend(storage_fixes)
    return current, fixes, []


def _rewrite_legacy_address_interface_types(
    source: str,
    config: Config,
    context: MigrationContext,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    imports: dict[str, str | None] = {}
    storage_interfaces: dict[str, str] = {}
    taken = code_identifiers(source)
    mask = code_mask(source)
    for match in re.finditer(r"\baddress\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        line_start = source.rfind("\n", 0, match.start()) + 1
        prefix = source[line_start : match.start()]
        declaration = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:public\s*\(\s*)?$", prefix)
        old = match.group(1)
        if declaration is None and not old[:1].isupper():
            continue
        new = IMPORT_RENAMES.get(old, old)
        interface_name = new
        alias: str | None = None
        if new != old and new in taken:
            interface_name = old
            alias = old
        if declaration is not None:
            storage_interfaces[declaration.group(1)] = interface_name
        edits.append(TextEdit(match.start(), match.end(), "address"))
        fixes.append(
            Fix(
                "VY206",
                line_number(source, match.start()),
                "changed legacy address interface type",
                match.group(0),
                "address",
            )
        )
        if new != old and _enabled("VY020", config, context):
            imports[new] = alias
    current = apply_edits(source, edits)
    for name, alias in sorted(imports.items()):
        import_line = f"from ethereum.ercs import {name}{f' as {alias}' if alias else ''}\n"
        if import_line.strip() not in current:
            current = _insert_import(current, import_line)
            fixes.append(
                Fix("VY020", 1, "added built-in interface import", "", import_line.rstrip("\n"))
            )
    current, cast_fixes = _cast_legacy_address_interface_calls(current, storage_interfaces)
    fixes.extend(cast_fixes)
    return current, fixes


def _cast_legacy_address_interface_calls(
    source: str, storage_interfaces: dict[str, str]
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for name, interface_name in storage_interfaces.items():
        assignment_pattern = re.compile(
            rf"\bself\.{re.escape(name)}\s*=\s*{re.escape(interface_name)}\s*\(([^()\n]+)\)"
        )
        for match in assignment_pattern.finditer(source):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            replacement = f"self.{name} = {match.group(1).strip()}"
            edits.append(TextEdit(match.start(), match.end(), replacement))
            fixes.append(
                Fix(
                    "VY206",
                    line_number(source, match.start()),
                    "removed legacy interface cast in address assignment",
                    match.group(0),
                    replacement,
                )
            )
        pattern = re.compile(rf"\bself\.{re.escape(name)}\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        for match in pattern.finditer(source):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            replacement = f"{interface_name}(self.{name}).{match.group(1)}("
            edits.append(TextEdit(match.start(), match.end(), replacement))
            fixes.append(
                Fix(
                    "VY206",
                    line_number(source, match.start()),
                    "cast legacy address interface call",
                    match.group(0),
                    replacement,
                )
            )
    return apply_edits(source, edits), fixes


def _rewrite_value_call_interface_methods_payable(source: str) -> tuple[str, list[Fix]]:
    methods = {
        match.group(1)
        for match in re.finditer(
            r"\b[A-Za-z_][A-Za-z0-9_]*\s*\([^()\n]*\)\.([A-Za-z_][A-Za-z0-9_]*)\s*\([^()\n]*\bvalue\s*=",
            source,
        )
    }
    if not methods:
        return source, []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match in re.finditer(
        r"^([ \t]*def[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\([^#\n]*\)[ \t]*(?:->[ \t]*[^:#\n]+)?[ \t]*:[ \t]*)nonpayable([ \t]*(?:#.*)?$)",
        source,
        re.MULTILINE,
    ):
        if match.group(2) not in methods:
            continue
        replacement = f"{match.group(1)}payable{match.group(3)}"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY206",
                line_number(source, match.start()),
                "changed value-receiving interface mutability",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_legacy_interface_storage_vars(source: str) -> tuple[str, list[Fix]]:
    interfaces = {
        match.group(1)
        for match in re.finditer(r"^interface\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", source, re.MULTILINE)
    }
    if not interfaces:
        return source, []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    storage_interfaces: dict[str, str] = {}
    mask = code_mask(source)
    pattern = re.compile(
        r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(public\s*\(\s*)?([A-Za-z_][A-Za-z0-9_]*)(\s*\))?([ \t]*(?:#.*)?$)",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(2), match.end(4)):
            continue
        interface_name = match.group(4)
        if interface_name not in interfaces:
            continue
        storage_interfaces[match.group(2)] = interface_name
        public_open = match.group(3) or ""
        public_close = match.group(5) or ""
        replacement = (
            f"{match.group(1)}{match.group(2)}: {public_open}address{public_close}{match.group(6)}"
        )
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY206",
                line_number(source, match.start()),
                "changed legacy interface storage type",
                match.group(0),
                replacement,
            )
        )
    current = apply_edits(source, edits)
    current, cast_fixes = _cast_legacy_address_interface_calls(current, storage_interfaces)
    fixes.extend(cast_fixes)
    return current, fixes


def _legacy_dynamic_types(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
        fixes.append(
            Fix(
                "VY207",
                line_number(source, match.start()),
                f"capitalized legacy {match.group(1)} type",
                match.group(1),
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _early_beta_syntax(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    rules = {"VY216", "VY217", "VY218", "VY219", "VY221"}
    if not _any_enabled(rules, config, context) or not _pre_021_context(context):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    if _enabled("VY216", config, context):
        current, new_fixes = _rewrite_early_beta_types(current)
        fixes.extend(new_fixes)
    if _enabled("VY217", config, context):
        current, new_fixes = _replace_identifier_call(current, "sha3", "keccak256", "VY217")
        fixes.extend(new_fixes)
    if _enabled("VY218", config, context):
        current, new_fixes = _rewrite_string_convert_types(current)
        fixes.extend(new_fixes)
    if _enabled("VY219", config, context):
        current, new_fixes = _rewrite_early_beta_clear(current)
        fixes.extend(new_fixes)
    if _enabled("VY221", config, context):
        current, new_fixes = _rewrite_early_beta_call_syntax(current)
        fixes.extend(new_fixes)
    return current, fixes, []


def _rewrite_early_beta_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bbytes\s*<=\s*([A-Za-z_][A-Za-z0-9_]*|\d+)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = f"bytes[{match.group(1)}]"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY216",
                line_number(source, match.start()),
                "changed early beta bytes bound syntax",
                match.group(0),
                replacement,
            )
        )
    type_replacements = {
        "num128": "int128",
        "num256": "uint256",
        "signed256": "int256",
        "num": "int128",
    }
    pattern = re.compile(r"(?P<prefix>(?::|->|\[)\s*)(?P<type>num128|num256|signed256|num)\b")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = type_replacements[match.group("type")]
        edits.append(TextEdit(match.start("type"), match.end("type"), replacement))
        fixes.append(
            Fix(
                "VY216",
                line_number(source, match.start()),
                "renamed early beta numeric type",
                match.group("type"),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_string_convert_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, "convert"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 2:
            continue
        target = args[1].strip()
        target_match = re.fullmatch(r"""(["'])([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\1""", target)
        if target_match is None:
            continue
        replacement = f"convert({args[0].strip()}, {target_match.group(2)})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY218",
                line_number(source, match.start()),
                "changed convert string type argument",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_early_beta_clear(source: str) -> tuple[str, list[Fix]]:
    current, fixes = _replace_identifier_call(source, "reset", "clear", "VY219")
    mask = code_mask(current)
    edits: list[TextEdit] = []
    for match in re.finditer(
        r"^(?P<indent>[ \t]*)del[ \t]+(?P<expr>[^#\n]+?)(?P<trailing>[ \t]*(?:#.*)?)(?=\n|$)",
        current,
        re.MULTILINE,
    ):
        if not _line_match_starts_outside_string(current, mask, match.start()):
            continue
        expr = match.group("expr").strip()
        replacement = f"{match.group('indent')}clear({expr}){match.group('trailing')}"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY219",
                line_number(current, match.start()),
                "changed early beta delete statement",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(current, edits), fixes


def _rewrite_early_beta_call_syntax(source: str) -> tuple[str, list[Fix]]:
    current, fixes = _rewrite_as_wei_value_units(source)
    current, slice_fixes = _rewrite_slice_keyword_args(current)
    fixes.extend(slice_fixes)
    return current, fixes


def _rewrite_as_wei_value_units(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, "as_wei_value"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 2:
            continue
        unit = args[1].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", unit) is None:
            continue
        replacement = f'as_wei_value({args[0].strip()}, "{unit}")'
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY221",
                line_number(source, match.start()),
                "quoted as_wei_value unit",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_slice_keyword_args(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, "slice"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 3:
            continue
        value_arg: str | None = None
        start_arg: str | None = None
        length_arg: str | None = None
        for arg in args:
            name, sep, raw_value = arg.partition("=")
            if not sep:
                if value_arg is not None:
                    value_arg = None
                    break
                value_arg = arg.strip()
                continue
            if name.strip() == "start":
                start_arg = raw_value.strip()
            elif name.strip() == "len":
                length_arg = raw_value.strip()
            else:
                value_arg = None
                break
        if value_arg is None or start_arg is None or length_arg is None:
            continue
        replacement = f"slice({value_arg}, {start_arg}, {length_arg})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY221",
                line_number(source, match.start()),
                "changed slice required keyword arguments",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _reserved_parameter_names(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY212", config, context):
        return source, [], []
    if context.source_floor is not None and context.source_floor > VyperVersion("0.2.1"):
        return source, [], []
    facts = parse_source_facts(source)
    line_offsets = _line_offsets(source)
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    pattern = re.compile(r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<args>[^)]*)\)", re.MULTILINE)
    for match in pattern.finditer(source):
        args = split_top_level_args(match.group("args"))
        if args is None:
            continue
        names = {arg.split(":", 1)[0].split("=", 1)[0].strip() for arg in args}
        if "value" not in names:
            continue
        replacement = "_value" if "_value" not in names else "value_"
        args_start = match.start("args")
        args_text = match.group("args")
        for name_match in re.finditer(r"\bvalue\b(?=\s*(?::|=|,|$))", args_text):
            start = args_start + name_match.start()
            edits.append(TextEdit(start, start + len("value"), replacement))
        function_line = line_number(source, match.start())
        body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
        for name_match in re.finditer(r"\bvalue\b", source[body_start:body_end]):
            start = body_start + name_match.start()
            end = body_start + name_match.end()
            if not span_is_code(mask, start, end):
                continue
            if _is_attribute_name(source, start) or _is_keyword_argument_name(source, start, end):
                continue
            edits.append(TextEdit(start, end, replacement))
        fixes.append(
            Fix(
                "VY212",
                function_line,
                "renamed reserved function parameter value",
                "value",
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _legacy_diagnostics(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    if _enabled("VYD210", config, context):
        diagnostics.extend(_byte_string_literal_diagnostics(source))
    if _enabled("VYD211", config, context) and (
        context.source_floor is None or context.source_floor <= VyperVersion("0.2.1")
    ):
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
            Diagnostic(
                "VYD215",
                line_number(source, match.start()),
                "RLPList was removed; rewrite this data model manually",
            )
            for match in re.finditer(r"\bRLPList\b", source)
            if span_is_code(mask, match.start(), match.end())
        )
    return source, [], diagnostics


def _natspec_strictness(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
            fixes.append(
                Fix(
                    "VY058",
                    line_no,
                    "removed NatSpec line for unknown function parameter",
                    line,
                    "",
                )
            )
        elif replacement != line:
            edits.append(TextEdit(offset, offset + len(line), replacement))
            fixes.append(Fix("VY058", line_no, "updated NatSpec tag syntax", line, replacement))
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


def _function_start_at_line(facts: SourceFacts, line_no: int) -> int | None:
    for start, end in sorted(facts.function_ends.items()):
        if start <= line_no <= end:
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


def _legacy_builtin_calls(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY208", "VY209"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    if _enabled("VY208", config, context):
        current, new_fixes = _replace_identifier_call(
            current, "create_with_code_of", "create_copy_of", "VY208"
        )
        fixes.extend(new_fixes)
        current, new_fixes = _replace_call_keyword(
            current, "raw_call", "outsize", "max_outsize", "VY208"
        )
        fixes.extend(new_fixes)
        current, new_fixes = _replace_call_keyword(
            current, "extract32", "type", "output_type", "VY208"
        )
        fixes.extend(new_fixes)
        current, new_fixes = _replace_assert_modifiable(current)
        fixes.extend(new_fixes)
        current, new_fixes = _unwrap_legacy_builtin(current, "as_unitless_number", "VY208")
        fixes.extend(new_fixes)
    if _enabled("VY209", config, context):
        current, new_fixes = _rewrite_method_id_bytes32_comparisons(current)
        fixes.extend(new_fixes)
        current, new_fixes = _rewrite_method_id_shift_output_type(current)
        fixes.extend(new_fixes)
        current, new_fixes = _remove_call_keyword_arg(
            current, "method_id", "output_type", "bytes4", "VY209"
        )
        fixes.extend(new_fixes)
    return current, fixes, []


def _replace_identifier_call(source: str, old: str, new: str, rule: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(rf"\b{re.escape(old)}\s*(?=\()", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(), match.start() + len(old), new))
        fixes.append(
            Fix(rule, line_number(source, match.start()), f"renamed legacy {old} builtin", old, new)
        )
    return apply_edits(source, edits), fixes


def _legacy_constructor_locks(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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


def _byte_string_literal_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    patterns = [
        (
            re.compile(r"\bBytes\s*\[[^\]]+\]\s*=\s*(?=\")"),
            'byte arrays require byte literals such as b"..."',
        ),
        (
            re.compile(r"\bString\s*\[[^\]]+\]\s*=\s*(?=b\")"),
            "strings require string literals, not byte literals",
        ),
    ]
    for pattern, message in patterns:
        for match in pattern.finditer(source):
            if span_is_code(mask, match.start(), match.end()):
                diagnostics.append(
                    Diagnostic("VYD210", line_number(source, match.start()), message)
                )
    return diagnostics


def _reserved_value_parameter_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for match in re.finditer(
        r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<args>[^)]*)\)", source, re.MULTILINE
    ):
        args = split_top_level_args(match.group("args"))
        if args is None:
            continue
        for arg in args:
            name = arg.split(":", 1)[0].split("=", 1)[0].strip()
            if name == "value":
                diagnostics.append(
                    Diagnostic(
                        "VYD211",
                        line_number(source, match.start()),
                        "function parameter name 'value' became reserved; rename it and update references",
                    )
                )
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
        if not _is_uint256_expr(args[1], vars_for_line) or not _is_uint256_expr(
            args[2], vars_for_line
        ):
            diagnostics.append(
                Diagnostic(
                    "VYD212",
                    line_number(source, match.start()),
                    "slice start and length must be uint256",
                )
            )
    return diagnostics


def _len_uint256_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    pattern = re.compile(
        r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?P<typ>i?nt(?:8|16|32|64|128|256)?)\s*=\s*len\s*\(",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if match.group("typ") != "uint256":
            diagnostics.append(
                Diagnostic(
                    "VYD213",
                    line_number(source, match.start()),
                    "len() returns uint256; update the receiving type",
                )
            )
    return diagnostics


def _call_kwarg_uint256_diagnostics(source: str) -> list[Diagnostic]:
    facts = parse_source_facts(source)
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    for match in re.finditer(
        r"(?<![\w.])(?:[A-Za-z_][A-Za-z0-9_]*\([^)\n]*\)|(?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\s*\(",
        source,
    ):
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
                diagnostics.append(
                    Diagnostic(
                        "VYD214",
                        line_number(source, match.start()),
                        f"external-call {name.strip()} kwarg must be uint256",
                    )
                )
    return diagnostics


def _is_uint256_expr(expr: str, vars_for_line: dict[str, str]) -> bool:
    expr = expr.strip()
    if _literal_integer(expr):
        return True
    expr_type = infer_expr_type(expr, vars_for_line)
    return expr_type == "uint256"


def _rewrite_legacy_event_declarations(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*event\s*\(\s*\{", re.MULTILINE)
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
        fixes.append(
            Fix(
                "VY203",
                line_number(source, match.start()),
                "changed legacy event declaration",
                source[match.start() : close_paren + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _collect_event_fields(source: str) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {}
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(
            r"^(?P<indent>\s*)event\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:#.*)?$", line
        )
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
    current = source
    current, call_fixes = _rewrite_map_call_types(current)
    fixes.extend(call_fixes)
    current, subscript_fixes = _rewrite_legacy_subscript_map_types(current)
    fixes.extend(subscript_fixes)
    return current, fixes


def _rewrite_map_call_types(source: str) -> tuple[str, list[Fix]]:
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
        replacement = _rewrite_legacy_map_type(source[match.start() : close + 1])
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY205",
                line_number(source, match.start()),
                "changed legacy map type to HashMap",
                source[match.start() : close + 1],
                replacement,
            )
        )
        last_end = close + 1
    return apply_edits(source, edits), fixes


def _rewrite_legacy_subscript_map_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    current = source
    while True:
        edit = _next_legacy_subscript_map_edit(current)
        if edit is None:
            return current, fixes
        text_edit, before, after = edit
        fixes.append(
            Fix(
                "VY205",
                line_number(current, text_edit.start),
                "changed legacy map type to HashMap",
                before,
                after,
            )
        )
        current = apply_edits(current, [text_edit])


def _next_legacy_subscript_map_edit(source: str) -> tuple[TextEdit, str, str] | None:
    mask = code_mask(source)
    for open_index, char in enumerate(source):
        if char != "[" or not span_is_code(mask, open_index, open_index + 1):
            continue
        close = find_matching(source, open_index, "[", "]")
        if close is None:
            continue
        key = source[open_index + 1 : close].strip()
        if not _legacy_map_key_type(key):
            continue
        value_start = _legacy_map_value_start(source, open_index)
        if value_start is None:
            continue
        value = source[value_start:open_index].strip()
        value = _strip_wrapping_parens(value)
        before = source[value_start : close + 1]
        after = f"HashMap[{key}, {value}]"
        return TextEdit(value_start, close + 1, after), before, after
    return None


def _legacy_map_key_type(text: str) -> bool:
    return bool(re.fullmatch(r"address|bool|bytes[0-9]+|u?int(?:8|16|32|64|128|256)?", text))


def _legacy_map_value_start(source: str, open_index: int) -> int | None:
    index = open_index - 1
    while index >= 0 and source[index].isspace():
        index -= 1
    if index < 0:
        return None
    if source[index] == ")":
        return _find_matching_open(source, index)
    if source[index] == "]":
        start = _find_matching_open(source, index, open_char="[", close_char="]")
        if start is None:
            return None
        return _legacy_map_value_start(source, start) or start
    if not (source[index].isalnum() or source[index] == "_"):
        return None
    while index >= 0 and (source[index].isalnum() or source[index] == "_"):
        index -= 1
    return index + 1


def _strip_wrapping_parens(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("(") or not stripped.endswith(")"):
        return stripped
    close = find_matching(stripped, 0)
    if close == len(stripped) - 1:
        return stripped[1:-1].strip()
    return stripped


def _rewrite_legacy_map_type(text: str) -> str:
    pieces: list[str] = []
    index = 0
    while match := re.search(r"\bmap\s*\(", text[index:]):
        start = index + match.start()
        open_paren = index + match.end() - 1
        close = find_matching(text, open_paren)
        if close is None:
            break
        args = split_top_level_args(text[open_paren + 1 : close])
        if args is None or len(args) != 2:
            break
        pieces.append(text[index:start])
        key = _rewrite_legacy_map_type(args[0].strip())
        value = _rewrite_legacy_map_type(args[1].strip())
        pieces.append(f"HashMap[{key}, {value}]")
        index = close + 1
    pieces.append(text[index:])
    return "".join(pieces)


def _replace_call_keyword(
    source: str,
    call_name: str,
    before: str,
    after: str,
    rule: str,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, _close, args in _iter_calls(source, call_name):
        keyword_match = re.search(rf"(?<!\w){re.escape(before)}\s*=", args)
        if keyword_match is None:
            continue
        start = match.end() + keyword_match.start()
        end = start + len(before)
        edits.append(TextEdit(start, end, after))
        fixes.append(
            Fix(
                rule,
                line_number(source, start),
                f"renamed {call_name} keyword {before}",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes


def _replace_assert_modifiable(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, "assert_modifiable"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 1:
            continue
        replacement = f"assert {args[0].strip()}"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY208",
                line_number(source, match.start()),
                "replaced assert_modifiable builtin",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _unwrap_legacy_builtin(source: str, call_name: str, rule: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, call_name):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 1:
            continue
        replacement = args[0].strip()
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                rule,
                line_number(source, match.start()),
                f"removed legacy {call_name} builtin",
                source[match.start() : close + 1],
                replacement,
            )
        )
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
    for match, _open_index, close, raw_args in _iter_calls(source, call_name):
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
        fixes.append(
            Fix(
                rule,
                line_number(source, match.start()),
                f"removed redundant {call_name} {keyword} keyword",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_method_id_bytes32_comparisons(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, open_index, close, raw_args in _iter_calls(source, "method_id"):
        arg_spans = split_top_level_arg_spans(raw_args)
        if arg_spans is None:
            continue
        output_value_span: tuple[int, int] | None = None
        for arg_start, _arg_end, arg in arg_spans:
            name, sep, raw_value = arg.partition("=")
            if not sep or name.strip() != "output_type" or raw_value.strip() != "bytes32":
                continue
            value_start = (
                arg_start + arg.index(raw_value) + len(raw_value) - len(raw_value.lstrip())
            )
            value_end = value_start + len(raw_value.strip())
            output_value_span = (open_index + 1 + value_start, open_index + 1 + value_end)
            break
        if output_value_span is None:
            continue
        comparison = _method_id_comparison_operand(source, match.start(), close)
        if comparison is None:
            continue
        expr_start, expr_end, expr = comparison
        replacement = f"convert({expr}, bytes4)"
        edits.append(TextEdit(expr_start, expr_end, replacement))
        edits.append(TextEdit(output_value_span[0], output_value_span[1], "bytes4"))
        fixes.append(
            Fix(
                "VY209",
                line_number(source, match.start()),
                "converted bytes32 method_id comparison to bytes4",
                source[expr_start : close + 1],
                f"{replacement} == {source[match.start() : close + 1].replace('output_type=bytes32', 'output_type=bytes4')}",
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_method_id_shift_output_type(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, open_index, close, raw_args in _iter_calls(source, "method_id"):
        line_start = source.rfind("\n", 0, match.start()) + 1
        line_end = source.find("\n", close)
        if line_end == -1:
            line_end = len(source)
        after_call = source[close:line_end]
        if "convert(" not in source[line_start : match.start()] or not (
            re.search(r"\)\s*(?:<<|>>)\s*\d+", after_call)
            or re.search(r",\s*uint256\s*\)\s*,\s*\d+\s*\)", after_call)
        ):
            continue
        arg_spans = split_top_level_arg_spans(raw_args)
        if arg_spans is None:
            continue
        output_value_span: tuple[int, int] | None = None
        for arg_start, _arg_end, arg in arg_spans:
            name, sep, raw_value = arg.partition("=")
            if not sep or name.strip() != "output_type" or raw_value.strip() != "bytes32":
                continue
            value_start = (
                arg_start + arg.index(raw_value) + len(raw_value) - len(raw_value.lstrip())
            )
            value_end = value_start + len(raw_value.strip())
            output_value_span = (open_index + 1 + value_start, open_index + 1 + value_end)
            break
        if output_value_span is None:
            continue
        edits.append(TextEdit(output_value_span[0], output_value_span[1], "bytes4"))
        before = source[line_start:line_end].strip()
        after = before.replace("output_type=bytes32", "output_type=bytes4").replace(
            "output_type = bytes32", "output_type = bytes4"
        )
        fixes.append(
            Fix(
                "VY209",
                line_number(source, match.start()),
                "changed shifted method_id output type to bytes4",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes


def _iter_calls(
    source: str, call_name: str, mask: list[bool] | None = None
) -> Iterator[tuple[re.Match[str], int, int, str]]:
    if mask is None:
        mask = code_mask(source)
    for match in re.finditer(rf"\b{re.escape(call_name)}\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = match.end() - 1
        close = find_matching(source, open_index)
        if close is not None:
            yield match, open_index, close, source[open_index + 1 : close]


def _method_id_comparison_operand(
    source: str, call_start: int, call_end: int
) -> tuple[int, int, str] | None:
    line_start = source.rfind("\n", 0, call_start) + 1
    line_end = source.find("\n", call_end)
    if line_end == -1:
        line_end = len(source)
    eq_left = source.rfind("==", line_start, call_start)
    if eq_left != -1:
        expr_start = line_start
        prefix_match = re.match(r"\s*(?:assert|return)\s+", source[line_start:eq_left])
        if prefix_match is not None:
            expr_start = line_start + prefix_match.end()
        while expr_start < eq_left and source[expr_start].isspace():
            expr_start += 1
        expr_end = eq_left
        while expr_end > expr_start and source[expr_end - 1].isspace():
            expr_end -= 1
        expr = source[expr_start:expr_end]
        if expr and not expr.startswith("convert("):
            return expr_start, expr_end, expr
    eq_right = source.find("==", call_end, line_end)
    if eq_right != -1:
        expr_start = eq_right + 2
        while expr_start < line_end and source[expr_start].isspace():
            expr_start += 1
        expr_end = line_end
        while expr_end > expr_start and source[expr_end - 1].isspace():
            expr_end -= 1
        expr = source[expr_start:expr_end]
        if expr and not expr.startswith("convert("):
            return expr_start, expr_end, expr
    return None

