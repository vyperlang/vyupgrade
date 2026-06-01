from __future__ import annotations

import re

from ..models import Diagnostic, Fix
from .legacy_call_helpers import iter_calls, replace_identifier_call
from ..rule_registry import Rule, RuleContext, target_floor
from ..source import (
    TextEdit,
    apply_edits,
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
    if rule_context.is_enabled("VY208"):
        current, new_fixes = replace_identifier_call(
            current, "create_with_code_of", "create_copy_of", "VY208"
        )
        fixes.extend(new_fixes)
        current, new_fixes = _replace_call_keyword(
            current, "raw_call", "outsize", "max_outsize", "VY208"
        )
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
        current, new_fixes = _rewrite_method_id_bytes32_comparisons(current)
        fixes.extend(new_fixes)
        current, new_fixes = _rewrite_method_id_shift_output_type(current)
        fixes.extend(new_fixes)
    return current, fixes, []


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


def _rewrite_method_id_bytes32_comparisons(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, open_index, close, raw_args in iter_calls(source, "method_id"):
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
    for match, open_index, close, raw_args in iter_calls(source, "method_id"):
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

RULES = (
    Rule(
        "legacy_builtin_calls",
        runner=_legacy_builtin_calls,
        changes=(
            target_floor("VY208", (0, 2, 1)),
            target_floor("VY209", (0, 2, 1)),
        ),
    ),
)
