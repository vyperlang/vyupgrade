from __future__ import annotations

import re

from ..models import Diagnostic, Fix
from .legacy_call_helpers import iter_calls
from ..rule_registry import Rule, RuleContext, crossing, target_floor
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    line_number,
    split_top_level_arg_spans,
    split_top_level_args,
)


def _legacy_builtin_calls(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    current = source
    diagnostics: list[Diagnostic] = []
    if rule_context.migration.crosses("0.1.0b9") and rule_context.any_enabled({"VY208", "VYD021"}):
        proxy = (
            "create_minimal_proxy_to" if rule_context.migration.target_at_least("0.3.4")
            else "create_forwarder_to"
        )
        edits: list[TextEdit] = []
        for match, _open, _close, _args in iter_calls(source, "create_with_code_of"):
            line = line_number(source, match.start())
            diagnostics.append(
                Diagnostic(
                    "VYD021", line,
                    f"review proxy bytecode and returndata before switching to {proxy} with --aggressive",
                )
            )
            if rule_context.config.aggressive and rule_context.is_enabled("VY208"):
                old = "create_with_code_of"
                edits.append(TextEdit(match.start(), match.start() + len(old), proxy))
                fixes.append(Fix("VY208", line, "updated legacy forwarder factory", old, proxy))
        current = apply_edits(source, edits)
    if rule_context.is_enabled("VY208") and rule_context.migration.target_at_least("0.2.1"):
        current, new_fixes = _replace_call_keyword(
            current, "raw_call", "outsize", "max_outsize", "VY208"
        )
        fixes.extend(new_fixes)
        current, new_fixes = _rewrite_empty_bytes32_raw_call_data(current)
        fixes.extend(new_fixes)
        current, new_fixes = _rewrite_raw_call_max_outsize_uint_bounds(current)
        fixes.extend(new_fixes)
        current, new_fixes = _remove_delegate_raw_call_value(current)
        fixes.extend(new_fixes)
        current, new_fixes = _replace_call_keyword(
            current, "extract32", "type", "output_type", "VY208"
        )
        fixes.extend(new_fixes)
        current, new_fixes = _replace_assert_modifiable(current)
        fixes.extend(new_fixes)
        current, new_fixes = _unwrap_legacy_builtin(current, "as_unitless_number", "VY208")
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY209"):
        current, new_fixes = _rewrite_method_id_bytes32(current)
        fixes.extend(new_fixes)
    return current, fixes, diagnostics


def _argument_code(arg: str) -> str:
    """Keep code offsets while hiding strings, comments and line continuations."""
    masked = "".join(c if visible else " " for c, visible in zip(arg, code_mask(arg), strict=True))
    return masked.replace("\\\n", "  ")


def _replace_call_keyword(
    source: str,
    call_name: str,
    before: str,
    after: str,
    rule: str,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, _close, args in iter_calls(source, call_name):
        for start, _end, arg in split_top_level_arg_spans(args) or ():
            keyword = re.match(rf"\s*({before})\s*=(?!=)", _argument_code(arg))
            if keyword is None:
                continue
            offset = match.end() + start + keyword.start(1)
            edits.append(TextEdit(offset, offset + len(before), after))
            fixes.append(
                Fix(
                    rule, line_number(source, offset), f"renamed {call_name} keyword {before}",
                    before, after,
                )
            )
    return apply_edits(source, edits), fixes


def _rewrite_empty_bytes32_raw_call_data(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for _match, open_index, _close, raw_args in iter_calls(source, "raw_call"):
        spans = split_top_level_arg_spans(raw_args)
        if spans is None or len(spans) < 2:
            continue
        start, end, arg = spans[1]
        if re.fullmatch(r"empty\s*\(\s*bytes32\s*\)", arg.strip()) is None:
            continue
        edit_start = open_index + 1 + start
        edit_end = open_index + 1 + end
        replacement = 'b""'
        edits.append(TextEdit(edit_start, edit_end, replacement))
        fixes.append(
            Fix(
                "VY208",
                line_number(source, edit_start),
                "rewrote empty bytes32 raw_call calldata",
                source[edit_start:edit_end],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_raw_call_max_outsize_uint_bounds(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for _match, open_index, _close, raw_args in iter_calls(source, "raw_call"):
        spans = split_top_level_arg_spans(raw_args)
        if spans is None:
            continue
        for start, _end, arg in spans:
            name, sep, value = arg.partition("=")
            if not sep or name.strip() != "max_outsize":
                continue
            value_offset = arg.index(value) + len(value) - len(value.lstrip())
            value_start = open_index + 1 + start + value_offset
            value_text = value.strip()
            replacement = _uint_max_value_literal(value_text)
            if replacement is None:
                continue
            edits.append(TextEdit(value_start, value_start + len(value_text), replacement))
            fixes.append(
                Fix(
                    "VY208",
                    line_number(source, value_start),
                    "folded raw_call max_outsize uint bound",
                    value_text,
                    replacement,
                )
            )
    return apply_edits(source, edits), fixes


def _uint_max_value_literal(expr: str) -> str | None:
    match = re.fullmatch(r"max_value\s*\(\s*uint(?P<bits>\d+)\s*\)", expr)
    if match is None:
        return None
    bits = int(match.group("bits"))
    if bits >= 256 or bits % 8 != 0:
        return None
    return str(2**bits - 1)


def _remove_delegate_raw_call_value(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for _match, open_index, _close, raw_args in iter_calls(source, "raw_call"):
        spans = split_top_level_arg_spans(raw_args)
        if spans is None:
            continue
        if not any(
            re.fullmatch(r"is_delegate_call\s*=\s*True", _normalized_raw_call_arg(arg))
            for _start, _end, arg in spans
        ):
            continue
        value_index = next(
            (
                index
                for index, (_start, _end, arg) in enumerate(spans)
                if re.match(r"value\s*=", _normalized_raw_call_arg(arg))
            ),
            None,
        )
        if value_index is None:
            continue
        start, end, _arg = spans[value_index]
        if value_index + 1 < len(spans):
            remove_start = open_index + 1 + start
            remove_end = open_index + 1 + spans[value_index + 1][0]
        elif value_index > 0:
            remove_start = open_index + 1 + spans[value_index - 1][1]
            remove_end = open_index + 1 + end
        else:
            remove_start = open_index + 1 + start
            remove_end = open_index + 1 + end
        edits.append(TextEdit(remove_start, remove_end, ""))
        fixes.append(
            Fix(
                "VY208",
                line_number(source, remove_start),
                "removed value kwarg from delegate raw_call",
                source[remove_start:remove_end],
                "",
            )
        )
    return apply_edits(source, edits), fixes


def _normalized_raw_call_arg(arg: str) -> str:
    return arg.strip().lstrip("\\").strip()


def _replace_assert_modifiable(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in iter_calls(source, "assert_modifiable"):
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
    for match, _open_index, close, raw_args in iter_calls(source, call_name):
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
    for match, _open_index, close, raw_args in iter_calls(source, call_name):
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


def _rewrite_method_id_bytes32(source: str) -> tuple[str, list[Fix]]:
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for match, _open, close, args in iter_calls(source, "method_id"):
        for start, _end, arg in split_top_level_arg_spans(args) or ():
            keyword = re.fullmatch(r"\s*output_type\s*=\s*(bytes32)\s*", _argument_code(arg))
            if keyword is None:
                continue
            type_start = match.end() + start + keyword.start(1)
            before = source[match.start():close + 1]
            call = (
                source[match.start():type_start] + "bytes4"
                + source[type_start + len("bytes32"):close + 1]
            )
            # Legacy method_id(..., output_type=bytes32) left-padded the selector.
            after = f"convert(convert({call}, uint256), bytes32)"
            edits.append(TextEdit(match.start(), close + 1, after))
            fixes.append(
                Fix(
                    "VY209", line_number(source, match.start()), "preserved left-padded method ID",
                    before, after,
                )
            )
            break
    return apply_edits(source, edits), fixes


RULES = (
    Rule(
        "legacy_builtin_calls",
        runner=_legacy_builtin_calls,
        changes=(
            target_floor("VY208", "0.1.0b9"),
            crossing("VYD021", "0.1.0b9"),
            target_floor("VY209", (0, 3, 4)),
        ),
    ),
)
