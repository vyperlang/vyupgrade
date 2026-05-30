from __future__ import annotations

import re

from ..analysis import (
    SourceFacts,
    infer_expr_type,
    indexed_key_type,
    indexed_value_type,
    normalize_type,
)
from ..models import Diagnostic, Fix
from ..rule_helpers import (
    innermost_non_overlapping as _innermost_non_overlapping,
    lhs_assigned_type as _lhs_assigned_type,
    lhs_declared_type as _lhs_declared_type,
)
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    find_matching,
    line_number,
    split_top_level_arg_spans,
    split_top_level_args,
    span_is_code,
)
from .numeric_casts import inside_convert_call
from .numeric_constant_helpers import integer_constant_values
from .numeric_scope import (
    nearest_loop_var_type as _nearest_loop_var_type,
)
from .numeric_types import (
    is_signed_integer_type as _is_signed_integer_type,
    is_unsigned_integer_type as _is_unsigned_integer_type,
)


def _mixed_signed_unsigned_arithmetic(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    config = rule_context.config
    facts = rule_context.facts
    constant_values = integer_constant_values(source, config.source_ast)
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
        negative_assignment = re.fullmatch(
            r"(?P<prefix>\s*)\(?\s*-\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)?(?P<suffix>\s*)", rhs
        )
        if negative_assignment is not None and _is_unsigned_integer_type(lhs_type):
            name = negative_assignment.group("name")
            if _is_signed_integer_type(vars_for_line.get(name)):
                replacement = f"{negative_assignment.group('prefix')}convert(-{name}, {normalize_type(lhs_type or 'uint256')}){negative_assignment.group('suffix')}"
                edits.append(TextEdit(rhs_start, rhs_start + len(rhs), replacement))
                fixes.append(
                    Fix(
                        "VY052",
                        line_no,
                        "converted signed negation assigned to unsigned integer",
                        rhs,
                        replacement,
                    )
                )
                offset += len(raw_line)
                continue
        loop_vars = facts.loop_vars_at_line(line_no)
        signed_names = sorted(
            (
                name
                for name, type_name in vars_for_line.items()
                if _is_signed_integer_type(
                    _nearest_loop_var_type(source, rhs_start, name)
                    if name in loop_vars
                    else type_name
                )
                and (name in facts.global_vars or name in loop_vars)
            ),
            key=len,
            reverse=True,
        )
        for name in signed_names:
            for match in re.finditer(rf"\b{re.escape(name)}\b", rhs):
                start = rhs_start + match.start()
                end = start + len(name)
                comparison_target = _unsigned_comparison_target_type_at(
                    source, start, name, vars_for_line, facts
                )
                if (
                    _inside_attribute_access(source, start, end)
                    or inside_convert_call(source, start)
                    or _inside_range_header(source, start)
                    or (name in constant_values and _inside_shift_amount(source, start))
                    or _inside_type_subscript(source, start)
                    or _signed_comparison_target_type_at(source, start, name, vars_for_line)
                    is not None
                    or _signed_internal_call_arg_target_type(source, start, name, facts) is not None
                    or _signed_external_call_arg_target_type(
                        source, start, name, facts, vars_for_line
                    )
                    is not None
                    or _signed_subscript_key_target_type(source, start, name, vars_for_line, facts)
                    is not None
                    or (
                        comparison_target is None
                        and not _signed_name_has_unsigned_context(
                            source, start, name, lhs_type, vars_for_line, facts
                        )
                    )
                ):
                    continue
                replacement = f"convert({name}, {comparison_target or 'uint256'})"
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
                if _is_unsigned_integer_type(
                    _nearest_loop_var_type(source, rhs_start, name) or vars_for_line.get(name)
                )
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
                    or inside_convert_call(source, start)
                    or _inside_range_header(source, start)
                ):
                    continue
                target_type = (
                    _signed_comparison_target_type(
                        _local_expression(source, start), name, vars_for_line
                    )
                    or _signed_internal_call_arg_target_type(source, start, name, facts)
                    or _signed_external_call_arg_target_type(
                        source, start, name, facts, vars_for_line
                    )
                    or _signed_subscript_key_target_type(source, start, name, vars_for_line, facts)
                )
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
                    or inside_convert_call(source, start)
                    or _inside_any_convert_call(source, start)
                    or _inside_range_header(source, start)
                    or _inside_type_subscript(source, start)
                    or _is_unsigned_integer_type(lhs_type)
                ):
                    continue
                target_type = (
                    _signed_comparison_target_type_at(source, start, name, vars_for_line)
                    or _unsigned_name_signed_division_target_type(
                        _local_expression(source, start), name, vars_for_line, facts
                    )
                    or _unsigned_name_signed_arithmetic_target_type(
                        _local_expression(source, start), name, lhs_type, vars_for_line, facts
                    )
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
    for match in re.finditer(r"\bconvert\s*\(", source):
        close = find_matching(source, match.end() - 1)
        if close is None:
            continue
        arg_spans = split_top_level_arg_spans(source[match.end() : close])
        if arg_spans is None or len(arg_spans) != 2:
            continue
        expr_start, _expr_end, expr = arg_spans[0]
        _target_start, _target_end, target_type = arg_spans[1]
        if not _is_signed_integer_type(target_type):
            continue
        line_no = line_number(source, match.start())
        vars_for_line = facts.vars_at_line(line_no)
        if not _has_unsigned_context(expr, vars_for_line):
            continue
        absolute_expr_start = match.end() + expr_start
        for name, type_name in sorted(
            vars_for_line.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if not (_is_signed_integer_type(type_name) and name in facts.global_vars):
                continue
            for name_match in re.finditer(rf"\b{re.escape(name)}\b", expr):
                start = absolute_expr_start + name_match.start()
                end = absolute_expr_start + name_match.end()
                if (
                    _inside_attribute_access(source, start, end)
                    or _inside_nested_convert_call(source, start, match.end() - 1)
                    or _inside_type_subscript(source, start)
                ):
                    continue
                replacement = f"convert({name}, uint256)"
                edits.append(TextEdit(start, end, replacement))
                fixes.append(
                    Fix(
                        "VY052",
                        line_no,
                        "converted signed integer constant inside uint arithmetic before signed cast",
                        name,
                        replacement,
                    )
                )
    mask = rule_context.code_mask
    for bracket in re.finditer(r"\[", source):
        if not span_is_code(mask, bracket.start(), bracket.end()):
            continue
        close = find_matching(source, bracket.start(), "[", "]")
        if close is None:
            continue
        expr = source[bracket.end() : close]
        line_no = line_number(source, bracket.start())
        vars_for_line = facts.vars_at_line(line_no)
        index_expects_unsigned = _subscript_index_expects_unsigned(
            source, bracket.start(), vars_for_line
        )
        if not (_has_unsigned_context(expr, vars_for_line) or index_expects_unsigned):
            continue
        loop_vars = facts.loop_vars_at_line(line_no)
        for name, type_name in sorted(
            vars_for_line.items(), key=lambda item: len(item[0]), reverse=True
        ):
            name_type = (
                _nearest_loop_var_type(source, bracket.start(), name)
                if name in loop_vars
                else type_name
            )
            if not _is_signed_integer_type(name_type):
                continue
            for name_match in re.finditer(rf"\b{re.escape(name)}\b", expr):
                start = bracket.end() + name_match.start()
                end = bracket.end() + name_match.end()
                if (
                    _inside_attribute_access(source, start, end)
                    or inside_convert_call(source, start)
                    or _inside_type_subscript(source, start)
                    or not _inside_array_subscript(source, start, vars_for_line)
                ):
                    continue
                replacement = f"convert({name}, uint256)"
                edits.append(TextEdit(start, end, replacement))
                fixes.append(
                    Fix(
                        "VY052",
                        line_no,
                        "converted signed integer inside uint array index",
                        name,
                        replacement,
                    )
                )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


def _signed_name_has_unsigned_context(
    source: str,
    index: int,
    name: str,
    lhs_type: str | None,
    vars_for_line: dict[str, str],
    facts: SourceFacts,
) -> bool:
    if _is_signed_integer_type(lhs_type):
        return False
    peer = _comparison_peer(_local_expression(source, index), name)
    if (
        peer is not None
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", peer)
        and peer in facts.global_vars
        and _is_unsigned_integer_type(vars_for_line.get(peer))
    ):
        return False
    if _is_unsigned_integer_type(lhs_type):
        return True
    if _inside_array_subscript(source, index, vars_for_line):
        return True
    return _has_unsigned_context(
        _local_expression(source, index), vars_for_line
    ) or _enclosing_argument_has_unsigned_context(source, index, vars_for_line)


def _has_unsigned_context(line: str, vars_for_line: dict[str, str]) -> bool:
    if re.search(r"\bconvert\s*\([^,\n]+,\s*uint(?:\d+)?\s*\)", line):
        return True
    if re.search(
        r"\b(?:block\.(?:timestamp|number|difficulty|basefee|prevhash)|chain\.id|msg\.value|max_value\s*\(\s*uint)",
        line,
    ):
        return True
    for name, type_name in vars_for_line.items():
        if _is_unsigned_integer_type(type_name) and re.search(
            rf"\b(?:self\.)?{re.escape(name)}\b", line
        ):
            return True
    return False


def _enclosing_argument_has_unsigned_context(
    source: str, index: int, vars_for_line: dict[str, str]
) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    line_end = source.find("\n", index)
    if line_end == -1:
        line_end = len(source)
    opens = [match.start() for match in re.finditer(r"\(", source[line_start:index])]
    for relative_open in reversed(opens):
        open_index = line_start + relative_open
        close = find_matching(source, open_index)
        if close is None or close < index or close > line_end:
            continue
        raw_args = source[open_index + 1 : close]
        offset = index - open_index - 1
        spans = split_top_level_arg_spans(raw_args)
        if spans is None:
            continue
        for start, end, arg in spans:
            if start <= offset <= end and _has_unsigned_context(arg, vars_for_line):
                return True
    return False


def _signed_comparison_target_type(
    expr: str, name: str, vars_for_line: dict[str, str]
) -> str | None:
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


def _unsigned_name_signed_arithmetic_target_type(
    expr: str, name: str, lhs_type: str | None, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
    expr = expr.strip()
    if not re.search(rf"\b{re.escape(name)}\b", expr):
        return None
    if _is_signed_integer_type(lhs_type):
        return normalize_type(lhs_type)
    comparison_type = _unsigned_name_signed_comparison_expression_type(
        expr, name, vars_for_line, facts
    )
    if comparison_type is not None:
        return comparison_type
    return None


def _unsigned_name_signed_comparison_expression_type(
    expr: str, name: str, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
    expr = expr.strip().removesuffix(":").strip()
    expr = re.sub(r"^(?:if|assert|return)\s+", "", expr)
    for separator in (" and ", " or "):
        if separator in expr:
            for part in expr.split(separator):
                if re.search(rf"\b{re.escape(name)}\b", part):
                    target_type = _unsigned_name_signed_comparison_expression_type(
                        part, name, vars_for_line, facts
                    )
                    if target_type is not None:
                        return target_type
    match = re.match(r"(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)\Z", expr)
    if match is None:
        return None
    left, _op, right = (part.strip() for part in match.groups())
    if re.search(rf"\b{re.escape(name)}\b", left):
        candidate = right
    elif re.search(rf"\b{re.escape(name)}\b", right):
        candidate = left
    else:
        return None
    candidate_type = infer_expr_type(candidate, vars_for_line, facts)
    return normalize_type(candidate_type) if _is_signed_integer_type(candidate_type) else None


def _signed_comparison_target_type_at(
    source: str, index: int, name: str, vars_for_line: dict[str, str]
) -> str | None:
    other = _comparison_peer(_local_expression(source, index), name)
    if other is None:
        return None
    loop_type = (
        _nearest_loop_var_type(source, index, other)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", other)
        else None
    )
    other_type = loop_type or infer_expr_type(other, vars_for_line)
    return normalize_type(other_type) if _is_signed_integer_type(other_type) else None


def _unsigned_comparison_target_type_at(
    source: str, index: int, name: str, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
    other = _comparison_peer(_local_expression(source, index), name)
    if other is None:
        return None
    loop_type = (
        _nearest_loop_var_type(source, index, other)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", other)
        else None
    )
    if (
        loop_type is None
        and other in facts.global_vars
        and _is_unsigned_integer_type(vars_for_line.get(other))
    ):
        return None
    other_type = loop_type or infer_expr_type(other, vars_for_line, facts)
    return normalize_type(other_type) if _is_unsigned_integer_type(other_type) else None


def _comparison_peer(expr: str, name: str) -> str | None:
    expr = expr.strip().removesuffix(":").strip()
    expr = re.sub(r"^(?:if|assert|return)\s+", "", expr)
    for separator in (" and ", " or "):
        if separator in expr:
            for part in expr.split(separator):
                if re.search(rf"\b{re.escape(name)}\b", part):
                    peer = _comparison_peer(part, name)
                    if peer is not None:
                        return peer
    match = re.match(r"(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)\Z", expr)
    if match is None:
        return None
    left, _op, right = (part.strip() for part in match.groups())
    if left == name:
        return right
    if right == name:
        return left
    return None


def _signed_internal_call_arg_target_type(
    source: str, index: int, name: str, facts: SourceFacts
) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("(", line_start, index)
    if open_index == -1:
        return None
    close = find_matching(source, open_index)
    if close is None or not (open_index < index < close):
        return None
    func_match = re.search(
        r"(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*$", source[line_start:open_index]
    )
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
    if (
        arg_index is None
        or args is None
        or arg_index >= len(args)
        or args[arg_index].strip() != arg
    ):
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
        target_type = facts.storage_vars.get(target.removeprefix("self.")) or infer_expr_type(
            target, vars_for_line, facts
        )
    params = facts.interface_params.get(normalize_type(target_type or ""), {}).get(
        call_match.group("method")
    )
    if not params or arg_index >= len(params):
        return None
    return list(params.values())[arg_index]


def _signed_subscript_key_target_type(
    source: str, index: int, name: str, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("[", line_start, index)
    if open_index == -1:
        return None
    close_index = find_matching(source, open_index, "[", "]")
    if close_index is None or not (open_index < index < close_index):
        return None
    if source[open_index + 1 : close_index].strip() != name:
        return None
    root = _subscript_root(source, line_start, open_index)
    if root is None:
        return None
    root_type = _root_type(root, vars_for_line, facts)
    key_type = indexed_key_type(root_type)
    return normalize_type(key_type) if _is_signed_integer_type(key_type) else None


def _top_level_arg_index(raw_args: str, offset: int) -> int | None:
    spans = split_top_level_arg_spans(raw_args)
    if spans is None:
        return None
    for arg_index, (start, end, _arg) in enumerate(spans):
        if start <= offset <= end:
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
    start = (
        max(
            source.rfind(",", line_start, index),
            source.rfind("(", line_start, index),
            line_start - 1,
        )
        + 1
    )
    end_candidates = [
        pos
        for pos in [source.find(",", index, line_end), source.find(")", index, line_end)]
        if pos != -1
    ]
    end = min(end_candidates) if end_candidates else line_end
    expr = source[start:end]
    mask = code_mask(expr)
    comment_start = next(
        (pos for pos, char in enumerate(expr) if char == "#" and (pos == 0 or mask[pos - 1])),
        None,
    )
    return expr[:comment_start] if comment_start is not None else expr


def _inside_array_subscript(source: str, index: int, vars_for_line: dict[str, str]) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("[", line_start, index)
    if open_index == -1:
        return False
    close_index = find_matching(source, open_index, "[", "]")
    if close_index is None or not (open_index < index < close_index):
        return False
    root = _subscript_root(source, line_start, open_index)
    if root is None:
        return False
    return _subscript_expects_unsigned(root, vars_for_line)


def _subscript_index_expects_unsigned(
    source: str, open_index: int, vars_for_line: dict[str, str]
) -> bool:
    line_start = source.rfind("\n", 0, open_index) + 1
    root = _subscript_root(source, line_start, open_index)
    if root is None:
        return False
    return _subscript_expects_unsigned(root, vars_for_line)


def _subscript_root(source: str, line_start: int, open_index: int) -> str | None:
    root_match = re.search(
        r"((?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*$",
        source[line_start:open_index],
    )
    return root_match.group(1) if root_match is not None else None


def _subscript_expects_unsigned(root: str, vars_for_line: dict[str, str]) -> bool:
    root_type = _root_type(root, vars_for_line)
    key_type = indexed_key_type(root_type)
    if key_type is not None:
        return _is_unsigned_integer_type(key_type)
    return indexed_value_type(root_type) is not None


def _root_type(
    root: str, vars_for_line: dict[str, str], facts: SourceFacts | None = None
) -> str | None:
    root_name = _strip_self(root)
    root_type = facts.storage_vars.get(root_name) if facts and root.startswith("self.") else None
    return root_type or vars_for_line.get(root_name) or infer_expr_type(root, vars_for_line, facts)


def _strip_self(name: str) -> str:
    return name[5:] if name.startswith("self.") else name


def _inside_any_convert_call(source: str, index: int) -> bool:
    for match in re.finditer(r"\bconvert\s*\(", source[:index]):
        open_index = match.end() - 1
        close = find_matching(source, open_index)
        if close is not None and open_index < index < close:
            return True
    return False


def _inside_nested_convert_call(source: str, index: int, outer_open: int) -> bool:
    for match in re.finditer(r"\bconvert\s*\(", source[:index]):
        open_index = match.end() - 1
        if open_index == outer_open:
            continue
        close = find_matching(source, open_index)
        if close is not None and open_index < index < close:
            return True
    return False


def _inside_range_header(source: str, index: int) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    prefix = source[line_start:index]
    return bool(
        re.search(r"\bfor\s+[A-Za-z_][A-Za-z0-9_]*(?::[^:]+)?\s+in\s+range\s*\([^)]*$", prefix)
    )


def _inside_shift_amount(source: str, index: int) -> bool:
    for match in re.finditer(r"\bshift\s*\(", source):
        open_index = match.end() - 1
        if open_index >= index:
            break
        close = find_matching(source, open_index)
        if close is None or not (open_index < index < close):
            continue
        arg_spans = split_top_level_arg_spans(source[open_index + 1 : close])
        if arg_spans is None or len(arg_spans) != 2:
            continue
        start, end, _arg = arg_spans[1]
        return open_index + 1 + start <= index < open_index + 1 + end
    return False


def _inside_type_subscript(source: str, index: int) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("[", line_start, index)
    if open_index == -1:
        return False
    close_index = find_matching(source, open_index, "[", "]")
    if close_index is None or not (open_index < index < close_index):
        return False
    return bool(
        re.search(
            r"(?:u?int(?:\d+)?|bool|address|bytes\d*|Bytes|String|DynArray|HashMap)\s*$",
            source[line_start:open_index],
        )
    )


def _inside_attribute_access(source: str, start: int, end: int) -> bool:
    return (start > 0 and source[start - 1] == ".") or (end < len(source) and source[end] == ".")




RULES = (
    Rule(
        "mixed_signed_unsigned_arithmetic",
        runner=_mixed_signed_unsigned_arithmetic,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
)
