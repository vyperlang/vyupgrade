from __future__ import annotations

import re

from ..analysis import infer_expr_type, is_integer_type, iterable_element_type, normalize_type, parse_source_facts
from ..models import Config, Diagnostic, Fix
from ..rule_helpers import innermost_non_overlapping as _innermost_non_overlapping
from ..rule_registry import Rule, crossing
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
from .external_call_helpers import external_call_matches
from .numeric_casts import (
    cast_integer_arg_to_exact_expected,
    cast_integer_arg_to_expected,
    inside_convert_call,
)
from .numeric_constant_helpers import constant_range_iteration_bound, integer_constant_values
from .numeric_scope import vars_for_argument as _vars_for_argument
from .numeric_types import (
    is_signed_integer_type as _is_signed_integer_type,
    is_unsigned_integer_type as _is_unsigned_integer_type,
)


def _signed_integer_array_constant_types(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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
            replacement = cast_integer_arg_to_exact_expected(
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
    facts = parse_source_facts(source)
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    constant_values = integer_constant_values(source, config.source_ast)
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
                if inside_convert_call(source, start) or not span_is_code(mask, start, end):
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
            bound = constant_range_iteration_bound(
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
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for start, end, target, method, cast_type in external_call_matches(source, facts):
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
            replacement = cast_integer_arg_to_expected(arg, expected, vars_for_arg, facts)
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


RULES = (
    Rule(
        "signed_integer_array_constant_types",
        runner=_signed_integer_array_constant_types,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
    Rule(
        "typed_array_literal_arguments",
        runner=_typed_array_literal_arguments,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
    Rule(
        "unsigned_range_bound_signed_constants",
        runner=_unsigned_range_bound_signed_constants,
        changes=(crossing("VY056", (0, 4, 0)),),
    ),
    Rule(
        "typed_external_call_arguments",
        runner=_typed_external_call_arguments,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
)
