from __future__ import annotations

import ast
import re

from ..analysis import (
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
from ..ast_facts import integer_constants as ast_integer_constants
from ..models import Config, Diagnostic, Fix
from ..rule_groups.external_calls import _all_external_call_matches
from ..rule_helpers import (
    find_matching_open as _find_matching_open,
    function_start_at_line as _function_start_at_line,
    innermost_non_overlapping as _innermost_non_overlapping,
    insert_import as _insert_import,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    literal_integer as _literal_integer,
    replace_identifier_expr as _replace_identifier_expr,
)
from ..rule_registry import Rule, RuleContext, any_enabled as _any_enabled, crossing, is_enabled as _enabled
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
from ..versions import MigrationContext


def _pre_04_expression_rewrites(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    current = source
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if _enabled("VY220", config, context):
        current, new_fixes = _replace_identifier_expr(
            current,
            "block.difficulty",
            "block.prevrandao",
            "VY220",
            "renamed block.difficulty to block.prevrandao",
        )
        fixes.extend(new_fixes)
    if _enabled("VY230", config, context):
        current, new_fixes = _remove_unary_plus(current)
        fixes.extend(new_fixes)
    if _any_enabled({"VY231", "VYD013"}, config, context):
        current, new_fixes, new_diagnostics = _replace_numeric_not(current, config, context)
        fixes.extend(new_fixes)
        diagnostics.extend(new_diagnostics)
    return current, fixes, diagnostics


def _integer_division(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
        slash_col = match.start() - line_start
        left_is_integer = is_integer_type(left_type) or _integerish_expression(
            left, vars_for_line, facts
        )
        right_is_integer = is_integer_type(right_type) or _integerish_expression(
            right, vars_for_line, facts
        )
        if (
            (left_is_integer and right_is_integer)
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
            fixes.append(
                Fix(
                    "VY050",
                    line_number(source, match.start()),
                    "changed integer division to //",
                    "/",
                    "//",
                )
            )
        else:
            if _enabled("VYD004", config, context):
                diagnostics.append(
                    Diagnostic(
                        "VYD004",
                        line_number(source, match.start()),
                        "cannot prove / operands are integer typed",
                    )
                )
    return apply_edits(source, edits), fixes, diagnostics


def _constant_integer_decl_casts(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY052", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    integer_type = r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)"
    pattern = re.compile(
        rf"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\(\s*(?P<type>{integer_type})\s*\)\s*=\s*(?P<value>[^\n#]+)(?P<comment>[ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start("name"), match.end("value")):
            continue
        expected_type = normalize_type(match.group("type"))
        if expected_type == "uint256":
            continue
        value = match.group("value").strip()
        if value.startswith("convert(") or _literal_integer(value):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        actual_type = infer_expr_type(value, vars_for_line, facts)
        if actual_type is not None and normalize_type(actual_type) == expected_type:
            continue
        folded = _eval_integer_constant_expr(
            value, _integer_constant_values(source, config.source_ast)
        )
        if folded is None or not _integer_value_fits_type(folded, expected_type):
            continue
        before = match.group(0)
        after = f"{match.group('indent')}{match.group('name')}: constant({expected_type}) = {folded}{match.group('comment')}"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY052",
                line_number(source, match.start()),
                "folded integer constant initializer to declared type",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _integer_value_fits_type(value: int, type_name: str) -> bool:
    match = re.fullmatch(r"(u?)int(\d+)", type_name)
    if match is None:
        return False
    bits = int(match.group(2))
    if match.group(1):
        return 0 <= value < 2**bits
    return -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)


def _constant_exponent_literals(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    return _constant_exponent_literals_context(RuleContext(source, config, context))


def _constant_exponent_literals_context(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    config = rule_context.config
    context = rule_context.migration
    if not _enabled("VY054", config, context):
        return source, [], []
    facts = rule_context.facts
    mask = rule_context.code_mask
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    max_int128_re = re.compile(r"(?<![\w])(?:\(\s*)?2\s*\*\*\s*127\s*-\s*1(?:\s*\))?")
    for match in max_int128_re.finditer(source):
        if not span_is_code(mask, match.start(), match.end()) or not _int128_literal_context(
            source, match.start(), facts
        ):
            continue
        replacement = "max_value(int128)"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY054",
                line_number(source, match.start()),
                "replaced signed int128 max literal",
                match.group(0),
                replacement,
            )
        )
    constant_values = _integer_constant_values(source, config.source_ast)
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


def _int128_literal_context(source: str, index: int, facts: SourceFacts) -> bool:
    line_no = line_number(source, index)
    return_type = facts.return_type_at_line(line_no)
    if normalize_type(return_type or "") == "int128":
        return True
    line_start = source.rfind("\n", 0, index) + 1
    line_end = source.find("\n", index)
    if line_end == -1:
        line_end = len(source)
    line = source[line_start:line_end]
    vars_for_line = facts.vars_at_line(line_no)
    return (
        normalize_type(_lhs_declared_type(line) or _lhs_assigned_type(line, vars_for_line) or "")
        == "int128"
    )


def _dynamic_pow_mod256(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY055", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    convert_operand = r"convert\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*uint256\s*\)"
    pattern = re.compile(rf"(?P<left>{convert_operand})\s*\*\*\s*(?P<right>{convert_operand})")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()) or _top_level_constant_line(
            source, match.start()
        ):
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


def _mixed_signed_unsigned_arithmetic(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY052", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    constant_values = _integer_constant_values(source, config.source_ast)
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
                    or _inside_convert_call(source, start)
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
                    or _inside_convert_call(source, start)
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
                    or _inside_convert_call(source, start)
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
    mask = code_mask(source)
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
                    or _inside_convert_call(source, start)
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


def _signed_integer_array_constant_types(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY052", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    decl_pattern = re.compile(
        r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\(\s*"
        r"(?P<signed>int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?)"
        r"\s*\[\s*(?P<length>[^\]]+?)\s*\]\s*\)\s*=\s*\[",
        re.MULTILINE,
    )
    for match in decl_pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, match.end() - 1, "[", "]")
        if close is None:
            continue
        elements = split_top_level_args(source[match.end() : close])
        if elements is None or any(element.strip().startswith("-") for element in elements):
            continue
        target_element_type = _unsigned_array_assignment_element_type(
            source, match.group("name"), mask
        )
        if target_element_type is None:
            continue
        start = match.start("signed")
        end = match.end("length") + 1
        replacement = f"{target_element_type}[{match.group('length').strip()}]"
        edits.append(TextEdit(start, end, replacement))
        fixes.append(
            Fix(
                "VY052",
                line_number(source, match.start()),
                "changed signed integer array constant to unsigned array type",
                source[start:end],
                replacement,
            )
        )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


def _unsigned_array_assignment_element_type(source: str, name: str, mask: list[bool]) -> str | None:
    unsigned_types: set[str] = set()
    signed_assignment = False
    assignment_pattern = re.compile(
        rf"(?P<type>\b(?:u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?\s*\[[^\]\n]+\]))\s*=\s*{re.escape(name)}\b"
    )
    for match in assignment_pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        element_type = iterable_element_type(match.group("type").replace(" ", ""))
        if _is_unsigned_integer_type(element_type):
            unsigned_types.add(normalize_type(element_type or "uint256"))
        elif _is_signed_integer_type(element_type):
            signed_assignment = True
    if signed_assignment or not unsigned_types:
        return None
    return _widest_unsigned_integer_type(unsigned_types)


def _widest_unsigned_integer_type(type_names: set[str]) -> str:
    widths = [
        int(match.group(1) or "256")
        for type_name in type_names
        if (match := re.fullmatch(r"uint(\d*)", type_name))
    ]
    return f"uint{max(widths) if widths else 256}"


def _typed_array_literal_arguments(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY052", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    mask = code_mask(source)
    pattern = re.compile(
        r"(?P<decl>\b[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?P<type>[^=\n]+?)\s*=\s*)\[",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        expected_type = iterable_element_type(match.group("type").strip())
        if not is_integer_type(expected_type):
            continue
        open_index = match.end() - 1
        close = find_matching(source, open_index, "[", "]")
        if close is None:
            continue
        arg_spans = split_top_level_arg_spans(source[open_index + 1 : close])
        if arg_spans is None:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        for start, end, arg in arg_spans:
            replacement = _cast_integer_arg_to_exact_expected(
                arg, expected_type, vars_for_line, facts
            )
            if replacement == arg:
                continue
            edits.append(TextEdit(open_index + 1 + start, open_index + 1 + end, replacement))
            fixes.append(
                Fix(
                    "VY052",
                    line_number(source, open_index + 1 + start),
                    "converted array literal element to declared integer type",
                    arg,
                    replacement,
                )
            )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


def _unsigned_range_bound_signed_constants(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY056", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    constant_values = _integer_constant_values(source, config.source_ast)
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
        arg_spans = split_top_level_arg_spans(args)
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
        for name, type_name in sorted(
            vars_for_line.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if not _is_signed_integer_type(type_name):
                continue
            for name_match in re.finditer(rf"\b{re.escape(name)}\b", args):
                if not any(
                    start <= name_match.start() and name_match.end() <= end
                    for start, end, _arg in positional_spans
                ):
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
            bound = _constant_range_iteration_bound(
                ", ".join(arg for _start, _end, arg in positional_spans), constant_values
            )
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


def _typed_external_call_arguments(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(
                target, vars_for_line, facts
            )
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
            arg_start = source.find(arg, cursor, close)
            if arg_start == -1:
                cursor += len(arg) + 1
                continue
            vars_for_arg = _vars_for_argument(source, arg_start, arg, vars_for_line)
            replacement = _cast_integer_arg_to_expected(arg, expected, vars_for_arg, facts)
            if replacement == arg:
                cursor = arg_start + len(arg) + 1
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


def _vars_for_argument(
    source: str, arg_start: int, arg: str, vars_for_line: dict[str, str]
) -> dict[str, str]:
    name = arg.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return vars_for_line
    declared_type = _nearest_declared_var_type(source, arg_start, name)
    if declared_type is not None:
        scoped = dict(vars_for_line)
        scoped[name] = declared_type
        return scoped
    loop_type = _nearest_loop_var_type(source, arg_start, name)
    if loop_type is None:
        return vars_for_line
    scoped = dict(vars_for_line)
    scoped[name] = loop_type
    return scoped


def _nearest_declared_var_type(source: str, index: int, name: str) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    current_line = source[
        line_start : source.find("\n", line_start)
        if source.find("\n", line_start) != -1
        else len(source)
    ]
    current_indent = len(current_line) - len(current_line.lstrip(" "))
    for line in reversed(source[:line_start].splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent > current_indent:
            continue
        if re.match(rf"for\s+{re.escape(name)}(?::[^:]+)?\s+in\b", stripped):
            return None
        decl = re.match(rf"{re.escape(name)}\s*:\s*([^=]+?)\s*=", stripped)
        if decl:
            return decl.group(1).strip()
        if re.match(r"(?:@|\s*def\s+)", stripped) and indent < current_indent:
            return None
    return None


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


def _nearest_loop_var_type(source: str, index: int, name: str) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    current_line = source[
        line_start : source.find("\n", line_start)
        if source.find("\n", line_start) != -1
        else len(source)
    ]
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


def _cast_integer_arg_to_expected(
    value: str, expected_type: str | None, vars_for_line: dict[str, str], facts: SourceFacts
) -> str:
    if not is_integer_type(expected_type) or value.strip().startswith("convert("):
        return value
    actual_type = infer_expr_type(value, vars_for_line, facts)
    if not is_integer_type(actual_type) or _same_integer_signedness(actual_type, expected_type):
        return value
    return f"convert({value}, {normalize_type(expected_type or '')})"


def _cast_integer_arg_to_exact_expected(
    value: str, expected_type: str | None, vars_for_line: dict[str, str], facts: SourceFacts
) -> str:
    stripped = value.strip()
    if (
        not is_integer_type(expected_type)
        or stripped.startswith("convert(")
        or _literal_integer(stripped)
    ):
        return value
    actual_type = infer_expr_type(stripped, vars_for_line, facts)
    if not is_integer_type(actual_type) or normalize_type(actual_type or "") == normalize_type(
        expected_type or ""
    ):
        return value
    return f"convert({value}, {normalize_type(expected_type or '')})"


def _same_integer_signedness(left: str | None, right: str | None) -> bool:
    return (_is_signed_integer_type(left) and _is_signed_integer_type(right)) or (
        _is_unsigned_integer_type(left) and _is_unsigned_integer_type(right)
    )


def _typed_range_loops(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY070", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = parse_source_facts(source)
    mask = code_mask(source)
    pattern = re.compile(
        r"^([ \t]*)for[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+in[ \t]+(.+?):", re.MULTILINE
    )
    inferred_loop_vars: dict[int, dict[str, str]] = {}

    for match in pattern.finditer(source):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
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
        fixes.append(
            Fix(
                "VY070",
                line_number(source, match.start()),
                f"added {var_type} loop variable type",
                before,
                after,
            )
        )
        if function_start is not None:
            inferred_loop_vars.setdefault(function_start, {})[match.group(2)] = var_type

    return apply_edits(source, edits), fixes, []


def _integer_assignment_casts(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY052", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(
        r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>[^\n#]+)(?P<comment>[ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start("name"), match.end("value")):
            continue
        value = match.group("value").strip()
        if value.startswith("convert(") or _literal_integer(value):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        expected_type = normalize_type(vars_for_line.get(match.group("name"), ""))
        if not _is_signed_integer_type(expected_type):
            continue
        actual_type = infer_expr_type(value, vars_for_line, facts)
        if not _is_unsigned_integer_type(actual_type):
            continue
        before = match.group(0)
        after = f"{match.group('indent')}{match.group('name')} = convert({value}, {expected_type}){match.group('comment')}"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY052",
                line_number(source, match.start()),
                "converted unsigned integer assignment to signed type",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


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


def _literal_list_element_type(
    iterable: str, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
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


def _range_bound(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
        bound = _infer_range_bound(
            args[0], args[1], _integer_constant_values(source, config.source_ast)
        )
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


def _sqrt(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY100", config, context):
        return source, [], []
    facts = parse_source_facts(source)
    if _name_is_user_defined(facts, "sqrt") or _name_is_imported(source, "sqrt"):
        return source, [], []
    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for match in re.finditer(r"(?<!\.)\bsqrt\s*\(", source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        if re.search(r"\bdef\s*$", source[line_start : match.start()]):
            continue
        if not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(), match.start() + 4, "math.sqrt"))
        fixes.append(
            Fix(
                "VY100",
                line_number(source, match.start()),
                "moved sqrt to math module",
                "sqrt",
                "math.sqrt",
            )
        )
    next_source = apply_edits(source, edits)
    if edits and not re.search(r"^\s*import\s+math\s*$", next_source, re.MULTILINE):
        next_source = _insert_import(next_source, "import math\n")
        fixes.append(Fix("VY100", 1, "added math import", "", "import math"))
    return next_source, fixes, []


def _bitwise(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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


def _read_left_operand(source: str, index: int) -> str:
    i = index - 1
    while i >= 0 and source[i].isspace():
        i -= 1
    if i >= 0 and source[i] == ")":
        open_index = _find_matching_open(source, i)
        if open_index is not None:
            return source[open_index : i + 1]
    if i >= 0 and source[i] == "]":
        open_index = _find_matching_open(source, i, open_char="[", close_char="]")
        if open_index is not None:
            start = _read_indexed_expression_start(source, open_index)
            return source[start : i + 1].replace("self.", "")
    end = i + 1
    while i >= 0 and re.match(r"[A-Za-z0-9_.$]", source[i]):
        i -= 1
    return source[i + 1 : end].replace("self.", "")


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
    type_name = unwrap_type(type_name)
    return bool(
        re.fullmatch(
            r"uint(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?",
            type_name,
        )
    )


def _is_signed_integer_type(type_name: str | None) -> bool:
    if type_name is None:
        return False
    type_name = unwrap_type(type_name)
    return bool(
        re.fullmatch(
            r"int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?",
            type_name,
        )
    )


def _inside_convert_call(source: str, index: int) -> bool:
    prefix = source[max(0, index - 24) : index]
    return bool(re.search(r"\bconvert\s*\([^,\n]*$", prefix))


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


def _integerish_expression(expr: str, vars_for_line: dict[str, str], facts=None) -> bool:
    expr = expr.split("#", 1)[0]
    if facts is not None:
        expr = _replace_integerish_subexpressions(expr, vars_for_line, facts)
    expr = expr.replace("self.", "")
    expr = re.sub(
        r"\b(?:block\.(?:timestamp|number|difficulty|basefee|prevhash)|chain\.id|msg\.value)\b",
        "1",
        expr,
    )
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
    return apply_edits(
        expr, _innermost_non_overlapping(edits, [Fix("VY050", 1, "", "", "") for _ in edits])[0]
    )


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
        match = re.match(
            r"\s*((?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\s*=\s*\(\s*$", line
        )
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


def _remove_unary_plus(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(
        r"(?P<prefix>(?:^|[=(,\[\{]\s*))\+(?P<expr>[A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE
    )
    for match in pattern.finditer(source):
        start = match.start("expr") - 1
        if not span_is_code(mask, start, match.end("expr")):
            continue
        edits.append(TextEdit(start, start + 1, ""))
        fixes.append(
            Fix("VY230", line_number(source, start), "removed disabled unary plus", "+", "")
        )
    return apply_edits(source, edits), fixes


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
                diagnostics.append(
                    Diagnostic(
                        "VYD013", line, f"cannot infer whether 'not {expr}' is numeric or boolean"
                    )
                )
            continue
        if not is_integer_type(expr_type):
            continue
        replacement = f"{expr} == 0"
        if not _enabled("VY231", config, context):
            continue
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY231",
                line,
                "changed numeric boolean negation to equality check",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, diagnostics


def _replace_builtin_call(
    source: str, name: str, operator: str, unary: bool, rule: str
) -> tuple[str, list[Fix]]:
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
        fixes.append(
            Fix(
                rule,
                line_number(source, match.start()),
                f"replaced {name} builtin",
                source[match.start() : close + 1],
                replacement,
            )
        )
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
        constant_values = _integer_constant_values(current, config.source_ast)
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
                replacement = (
                    f"({value} >> "
                    f"({_unsigned_shift_amount_expr(negative_expr.group(1).strip(), vars_for_line, constant_values)}))"
                )
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
            elif positive_convert is not None and not positive_convert.group(1).lstrip().startswith(
                "-"
            ):
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


def _unsigned_shift_amount_expr(
    expr: str, vars_for_line: dict[str, str], constant_values: dict[str, int]
) -> str:
    nonnegative_constants = [
        name
        for name, value in sorted(
            constant_values.items(), key=lambda item: len(item[0]), reverse=True
        )
        if value >= 0
    ]
    if nonnegative_constants:
        constant_re = re.compile(
            rf"\b({'|'.join(re.escape(name) for name in nonnegative_constants)})\b"
        )
        expr = constant_re.sub(lambda match: str(constant_values[match.group(1)]), expr)
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
    return all(
        _is_unsigned_integer_type(infer_expr_type(identifier, vars_for_line))
        for identifier in identifiers
    )


def _integer_constant_values(
    source: str, source_ast: dict[str, object] | None = None
) -> dict[str, int]:
    values: dict[str, int] = ast_integer_constants(source_ast) if source_ast is not None else {}
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


def _dynamic_bytes_hex_literals(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
    return (
        'b"'
        + "".join(f"\\x{raw[index : index + 2].lower()}" for index in range(0, len(raw), 2))
        + '"'
    )


def _name_is_user_defined(facts: SourceFacts, name: str) -> bool:
    return (
        name in facts.global_vars
        or name in facts.function_return_names
        or any(name in vars_for_func for vars_for_func in facts.function_vars.values())
    )


def _name_is_imported(source: str, name: str) -> bool:
    mask = code_mask(source)
    for match in re.finditer(
        r"^[ \t]*from[ \t]+[A-Za-z0-9_.]+[ \t]+import[ \t]+(.+)$", source, re.MULTILINE
    ):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
        for part in match.group(1).split(","):
            imported = part.split("#", 1)[0].strip()
            imported_name, _sep, alias = imported.partition(" as ")
            bound_name = alias.strip() if alias else imported_name.strip()
            if bound_name == name:
                return True
    for match in re.finditer(r"^[ \t]*import[ \t]+(.+)$", source, re.MULTILINE):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
        for part in match.group(1).split(","):
            imported = part.split("#", 1)[0].strip()
            module, _sep, alias = imported.partition(" as ")
            bound_name = alias.strip() if alias else module.split(".", 1)[0].strip()
            if bound_name == name:
                return True
    return False


def _redundant_integer_convert(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
        vars_for_line = _vars_for_argument(
            source,
            open_index + 1 + source[open_index + 1 : close].find(args[0]),
            expr,
            facts.vars_at_line(line_number(source, match.start())),
        )
        if (
            is_integer_type(target)
            and _inside_constant_declaration_line(source, match.start())
            and _integerish_expression(expr, vars_for_line)
            and not _expression_has_signed_integer(expr, vars_for_line)
        ):
            edits.append(TextEdit(match.start(), close + 1, expr))
            fixes.append(
                Fix(
                    "VY051",
                    line_number(source, match.start()),
                    "removed convert from constant initializer",
                    source[match.start() : close + 1],
                    expr,
                )
            )
            continue
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
        if target != "uint256" or expr.lstrip().startswith("-") or not re.search(r"[-+*/%]", expr):
            continue
        if _integerish_expression(expr, vars_for_line) and not _expression_has_signed_integer(
            expr, vars_for_line
        ):
            replacement = f"({expr})"
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(
                Fix(
                    "VY051",
                    line_number(source, match.start()),
                    "removed redundant uint256 convert around integer expression",
                    source[match.start() : close + 1],
                    replacement,
                )
            )
    return apply_edits(source, edits), fixes, []


def _redundant_convert_replacement(expr: str) -> str:
    return f"({expr})" if re.search(r"[-+*/%<>=|&]", expr) else expr


def _inside_constant_declaration_line(source: str, start: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    return bool(re.search(r":\s*constant\s*\(", source[line_start:start]))


def _simple_nonliteral_expr(expr: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", expr
        )
    )


def _expression_has_signed_integer(expr: str, vars_for_line: dict[str, str]) -> bool:
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        if _is_signed_integer_type(vars_for_line.get(token)):
            return True
    return False


PRE_INTERFACE_RULES = (
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
)

RANGE_RULES = (
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
)

POST_EXTERNAL_RULES = (
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
)

LATE_RULES = (
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
)

