from __future__ import annotations

import re

from ..models import Diagnostic, Fix
from ..rule_helpers import (
    find_matching_open as _find_matching_open,
    insert_import as _insert_import,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    pre_021_context as _pre_021_context,
)
from ..rule_registry import Rule, RuleContext, target_floor
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
    split_top_level_args,
    span_is_code,
)


IMPORT_RENAMES = {
    "ERC20": "IERC20",
    "ERC20Detailed": "IERC20Detailed",
    "ERC165": "IERC165",
    "ERC4626": "IERC4626",
    "ERC721": "IERC721",
    "ERC1155": "IERC1155",
}


def _legacy_maps_and_interfaces(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    context = rule_context.migration
    fixes: list[Fix] = []
    current = source
    if rule_context.is_enabled("VY205"):
        current, map_fixes = _rewrite_map_types(current)
        fixes.extend(map_fixes)
    if rule_context.is_enabled("VY206"):
        legacy_source = _pre_021_context(context)
        if legacy_source:
            current, address_interface_fixes = _rewrite_legacy_address_interface_types(
                current, rule_context
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
    rule_context: RuleContext,
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
        if new != old and rule_context.is_enabled("VY020"):
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



RULES = (
    Rule(
        "legacy_maps_and_interfaces",
        context_runner=_legacy_maps_and_interfaces,
        changes=(
            target_floor("VY205", (0, 2, 1)),
            target_floor("VY206", (0, 2, 1)),
        ),
    ),
)
