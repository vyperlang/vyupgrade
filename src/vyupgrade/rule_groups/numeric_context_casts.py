from __future__ import annotations

import re
from dataclasses import dataclass

from ..analysis import (
    SourceFacts,
    infer_expr_type,
    indexed_key_type,
    indexed_value_type,
    is_integer_type,
    iterable_element_type,
    normalize_type,
)
from ..constant_evaluation import integer_constant_values
from ..models import Diagnostic, Fix
from ..rule_helpers import innermost_non_overlapping as _innermost_non_overlapping
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    find_matching,
    line_number,
    split_top_level_arg_spans,
    split_top_level_args,
    span_is_code,
)
from .external_call_helpers import external_call_matches
from .legacy_call_helpers import iter_calls
from .numeric_casts import (
    cast_integer_arg_to_exact_expected,
    cast_integer_arg_to_expected,
    inside_convert_call,
)
from .numeric_constant_helpers import constant_range_iteration_bound
from .numeric_types import (
    is_signed_integer_type as _is_signed_integer_type,
    is_unsigned_integer_type as _is_unsigned_integer_type,
)


def _signed_integer_array_constant_types(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
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
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    mask = rule_context.code_mask
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
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    mask = rule_context.code_mask
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    constant_values = integer_constant_values(source, rule_context.compiler_facts.ast)
    for match in re.finditer(
        r"\bfor\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?P<loop_type>uint(?:\d+)?)\s+in\s+range\s*\(",
        source,
    ):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        loop_type = normalize_type(match.group("loop_type"))
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
            normalized_type = normalize_type(type_name)
            if not (
                _is_signed_integer_type(normalized_type)
                or _is_narrow_unsigned_integer_type(normalized_type)
            ):
                continue
            if normalized_type == loop_type:
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
                replacement = f"convert({name}, {loop_type})"
                edits.append(TextEdit(start, end, replacement))
                converted = True
                fixes.append(
                    Fix(
                        "VY056",
                        line_no,
                        "converted integer range bound for unsigned loop variable",
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


def _is_narrow_unsigned_integer_type(type_name: str) -> bool:
    match = re.fullmatch(r"uint(\d+)", normalize_type(type_name))
    return match is not None and int(match.group(1)) < 256


def _typed_external_call_arguments(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
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
            bytes_replacement = (
                _dynamic_bytes_hex_arg_replacement(arg, expected)
                if rule_context.is_enabled("VY053")
                else None
            )
            if bytes_replacement is not None:
                edits.append(TextEdit(arg_start, arg_start + len(arg), bytes_replacement))
                fixes.append(
                    Fix(
                        "VY053",
                        line_number(source, arg_start),
                        "changed dynamic bytes call argument to byte string literal",
                        arg,
                        bytes_replacement,
                    )
                )
                cursor = arg_start + len(arg) + 1
                continue
            if not rule_context.is_enabled("VY052"):
                cursor = arg_start + len(arg) + 1
                continue
            replacement = cast_integer_arg_to_expected(arg, expected, vars_for_line, facts)
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


def _dynamic_bytes_hex_arg_replacement(arg: str, expected: str) -> str | None:
    if re.fullmatch(r"Bytes\s*\[\s*\d+\s*\]", expected.strip()) is None:
        return None
    literal = arg.strip()
    if re.fullmatch(r"0x[0-9A-Fa-f]*", literal) is None:
        return None
    raw = literal.removeprefix("0x")
    if len(raw) % 2 != 0:
        return None
    return 'b"' + "".join(f"\\x{raw[index : index + 2].lower()}" for index in range(0, len(raw), 2)) + '"'


def _unsafe_subscript_index_arguments(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    whole_indexes = {index.start: index for index in whole_unsafe_sub_indexes(source, facts, mask)}
    for match, open_index, _close, raw_args in iter_calls(source, "unsafe_sub", mask):
        line_no = line_number(source, open_index)
        whole_index = whole_indexes.get(match.start())
        if whole_index is not None:
            if whole_index.converted or not whole_index.signed:
                continue
            call = source[whole_index.start : whole_index.end]
            replacement = f"convert({call}, {whole_index.target_type})"
            edits.append(TextEdit(whole_index.start, whole_index.end, replacement))
            fixes.append(
                Fix(
                    "VY052",
                    line_no,
                    "converted signed unsafe_sub array index to unsigned type",
                    call,
                    replacement,
                )
            )
            continue
        vars_for_line = facts.vars_at_line(line_no)
        target_type = _unsigned_subscript_index_type(
            source, open_index, vars_for_line, facts, preserve_shape=False
        )
        if target_type is None:
            continue
        arg_spans = split_top_level_arg_spans(raw_args)
        if arg_spans is None or len(arg_spans) != 2:
            continue
        for start, end, arg in arg_spans:
            arg_type = infer_expr_type(arg, vars_for_line, facts)
            if not _is_signed_integer_type(arg_type) or inside_convert_call(
                source, open_index + 1 + start
            ):
                continue
            replacement = f"convert({arg.strip()}, {target_type})"
            edits.append(TextEdit(open_index + 1 + start, open_index + 1 + end, replacement))
            fixes.append(
                Fix(
                    "VY052",
                    line_no,
                    "converted unsafe_sub array index operand to unsigned type",
                    arg,
                    replacement,
                )
            )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


_INDEX_OPEN = re.compile(r"\[(?P<groups>[\s(]*?)(?P<convert>convert\s*\(\s*)?$")
_INDEX_GAP = r"(?:\s|#[^\n]*)*"
_INTEGER_LITERAL = re.compile(r"[\s(]*-?\s*\d(?:_?\d)*[\s)]*")


@dataclass(frozen=True)
class UnsafeSubIndex:
    """An `unsafe_sub` call that forms a whole unsigned array index."""

    start: int
    end: int
    target_type: str
    converted: bool
    signed: bool
    operands: tuple[tuple[int, int], ...]

    def encloses(self, start: int, end: int) -> bool:
        return self.start < start and end < self.end

    def is_operand(self, start: int, end: int) -> bool:
        """Whether `start:end` is a whole operand, ignoring grouping parentheses."""
        return (start, end) in self.operands


def whole_unsafe_sub_indexes(
    source: str, facts: SourceFacts, mask: list[bool]
) -> list[UnsafeSubIndex]:
    """Find `unsafe_sub` calls with no known unsigned operand that form a whole unsigned index.

    The call may be grouped in parentheses or already sit in a uint `convert`.
    Vyper gives both operands one type, so a known signed operand makes the call
    `signed`. Only its result may be converted: converting a negative operand
    reverts even when the difference is a valid index. A call whose operands are
    untyped is left alone, since Vyper accepts a signed index unchanged.
    """
    calls: list[UnsafeSubIndex] = []
    for match, open_index, close, raw_args in iter_calls(source, "unsafe_sub", mask):
        line_start = source.rfind("\n", 0, match.start()) + 1
        index_open = _INDEX_OPEN.search(source[line_start : match.start()])
        if index_open is None:
            continue
        converted = index_open.group("convert") is not None
        index_close = (
            _INDEX_GAP
            + (r",\s*uint\d*\s*\)" + _INDEX_GAP if converted else "")
            + (r"\)" + _INDEX_GAP) * index_open.group("groups").count("(")
            + r"\]"
        )
        if re.compile(index_close).match(source, close + 1) is None:
            continue
        arg_spans = split_top_level_arg_spans(raw_args)
        if arg_spans is None or len(arg_spans) != 2:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, open_index))
        operand_types = [
            infer_expr_type(arg, vars_for_line, facts)
            for _start, _end, arg in arg_spans
            if _INTEGER_LITERAL.fullmatch(arg) is None
        ]
        if any(_is_unsigned_integer_type(t) for t in operand_types):
            continue
        target_type = _unsigned_subscript_index_type(
            source, open_index, vars_for_line, facts, preserve_shape=True
        )
        if target_type is not None:
            signed = any(_is_signed_integer_type(t) for t in operand_types)
            operands = tuple(
                _ungrouped_span(source, open_index + 1 + start, open_index + 1 + end)
                for start, end, _arg in arg_spans
            )
            calls.append(
                UnsafeSubIndex(match.start(), close + 1, target_type, converted, signed, operands)
            )
    return calls


def _ungrouped_span(source: str, start: int, end: int) -> tuple[int, int]:
    while True:
        while start < end and source[start].isspace():
            start += 1
        while end > start and source[end - 1].isspace():
            end -= 1
        if source[start : start + 1] != "(" or find_matching(source, start) != end - 1:
            return start, end
        start, end = start + 1, end - 1


def _ungrouped_type(type_name: str | None) -> str | None:
    while (
        type_name is not None
        and type_name.startswith("(")
        and find_matching(type_name, 0) == len(type_name) - 1
    ):
        inner = type_name[1:-1].strip()
        if inner.endswith(",") or len(split_top_level_args(inner) or []) != 1:
            return type_name  # a tuple type
        type_name = inner
    return type_name


def _unsigned_subscript_index_type(
    source: str,
    index: int,
    vars_for_line: dict[str, str],
    facts: SourceFacts,
    *,
    preserve_shape: bool,
) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    open_index = source.rfind("[", line_start, index)
    if open_index == -1:
        return None
    close_index = find_matching(source, open_index, "[", "]")
    if close_index is None or not (open_index < index < close_index):
        return None
    root_match = re.search(
        r"((?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*$",
        source[line_start:open_index],
    )
    if root_match is None:
        return None
    root = root_match.group(1)
    root_type = _ungrouped_type(
        infer_expr_type(root, vars_for_line, facts, preserve_shape=preserve_shape)
    )
    key_type = indexed_key_type(root_type)
    if key_type is not None:
        return normalize_type(key_type) if _is_unsigned_integer_type(key_type) else None
    if indexed_value_type(root_type) is not None:
        return "uint256"
    return None


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
        changes=(crossing("VY052", (0, 4, 0)), crossing("VY053", (0, 4, 0))),
    ),
    Rule(
        "unsafe_subscript_index_arguments",
        runner=_unsafe_subscript_index_arguments,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
)
