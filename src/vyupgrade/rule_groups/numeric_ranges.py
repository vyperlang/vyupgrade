from __future__ import annotations

import re
from dataclasses import dataclass

from ..analysis import (
    SourceFacts,
    infer_expr_type,
    is_integer_type,
    iterable_element_type,
    normalize_type,
)
from ..models import Diagnostic, Fix
from ..rule_helpers import (
    function_start_at_line as _function_start_at_line,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    literal_integer as _literal_integer,
)
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
from .numeric_constant_helpers import eval_integer_constant_expr, integer_constant_values
from .numeric_types import (
    is_signed_integer_type as _is_signed_integer_type,
    is_unsigned_integer_type as _is_unsigned_integer_type,
)


@dataclass(frozen=True)
class _FlagMemberList:
    type_name: str
    replacement: str


def _typed_range_loops(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = rule_context.facts
    mask = rule_context.code_mask
    flag_member_values = _flag_or_enum_member_values(source)
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
        flag_member_list = _literal_flag_member_list(iterable, flag_member_values)
        if flag_member_list is not None:
            var_type = "uint256"
            annotated_iterable = flag_member_list.replacement
        else:
            var_type = _loop_var_type(iterable, vars_for_line, facts)
            annotated_iterable = iterable
        if var_type is None:
            continue
        if var_type == "uint256" and _range_iterable_has_literal_bounds(iterable):
            var_type = _narrow_unsigned_range_loop_operand_type(
                source[match.end() :], match.group(2), vars_for_line
            ) or var_type
        if var_type == "int256" and _range_iterable_has_leading_negative_start(iterable):
            var_type = _negative_range_loop_operand_type(
                source[match.end() :], match.group(2), vars_for_line
            ) or var_type
        before = match.group(0)
        after = f"{match.group(1)}for {match.group(2)}: {var_type} in {annotated_iterable}:"
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


def _flag_or_enum_return_casts(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    pattern = re.compile(
        r"^(?P<indent>[ \t]*)return[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<comment>[ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start("name"), match.end("name")):
            continue
        line = line_number(source, match.start())
        return_type = normalize_type(facts.return_type_at_line(line) or "")
        if return_type not in facts.flags_or_enums:
            continue
        var_type = normalize_type(facts.vars_at_line(line).get(match.group("name"), ""))
        if not is_integer_type(var_type):
            continue
        before = match.group(0)
        after = (
            f"{match.group('indent')}return convert({match.group('name')}, {return_type})"
            f"{match.group('comment')}"
        )
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY052",
                line,
                "converted integer return value to enum or flag type",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _signed_negative_range_bounds(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    config = rule_context.config
    facts = rule_context.facts
    constant_values = integer_constant_values(source, config.source_ast)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    for match in re.finditer(r"\brange\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = source.find("(", match.start())
        close = find_matching(source, open_index)
        if close is None:
            continue
        raw_args = source[open_index + 1 : close]
        arg_spans = split_top_level_arg_spans(raw_args)
        if arg_spans is None or len(arg_spans) != 2:
            continue
        first_start, _first_end, first = arg_spans[0]
        second_start, _second_end, second = arg_spans[1]
        name = _negative_range_bound_name(first)
        if name is None:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        if not _is_unsigned_integer_type(vars_for_line.get(name)):
            continue
        line_start = source.rfind("\n", 0, match.start()) + 1
        loop_var_match = re.search(
            r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)(?::[^:]+)?\s+in\s+$",
            source[line_start : match.start()],
        )
        target_type = (
            _negative_range_loop_operand_type(
                source[close:], loop_var_match.group(1), vars_for_line
            )
            if loop_var_match is not None
            else None
        ) or "int256"
        bound = str(constant_values.get(name, name))
        if not any(arg.partition("=")[0].strip() == "bound" for _s, _e, arg in arg_spans):
            edits.append(TextEdit(close, close, f", bound={bound}"))
            fixes.append(
                Fix(
                    "VY071",
                    line_number(source, match.start()),
                    "added signed negative range bound",
                    f"range({raw_args})",
                    f"range({raw_args}, bound={bound})",
                )
            )
        for arg_start, arg in ((first_start, first), (second_start, second)):
            if not _range_bound_uses_name(arg, name):
                continue
            name_match = re.search(rf"\b{re.escape(name)}\b", arg)
            if name_match is None:
                continue
            start = open_index + 1 + arg_start + name_match.start()
            end = start + len(name)
            replacement = f"convert({name}, {target_type})"
            edits.append(TextEdit(start, end, replacement))
            fixes.append(
                Fix(
                    "VY052",
                    line_number(source, start),
                    "converted unsigned negative range bound to signed type",
                    name,
                    replacement,
                )
            )
    return apply_edits(source, edits), fixes, []


def _integer_assignment_casts(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
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
    if re.match(r"range\s*\(", iterable):
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


def _literal_flag_member_list(
    iterable: str, flag_member_values: dict[str, dict[str, int]]
) -> _FlagMemberList | None:
    if not (iterable.startswith("[") and iterable.endswith("]")):
        return None
    args = split_top_level_args(iterable[1:-1])
    if not args:
        return None
    type_name: str | None = None
    values: list[str] = []
    for arg in args:
        match = re.fullmatch(
            r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", arg.strip()
        )
        if match is None:
            return None
        current_type, member = match.groups()
        if type_name is None:
            type_name = current_type
        elif type_name != current_type:
            return None
        value = flag_member_values.get(current_type, {}).get(member)
        if value is None:
            return None
        values.append(str(value))
    if type_name is None:
        return None
    return _FlagMemberList(type_name, f"[{', '.join(values)}]")


def _flag_or_enum_member_values(source: str) -> dict[str, dict[str, int]]:
    values: dict[str, dict[str, int]] = {}
    current_name: str | None = None
    current_indent = 0
    next_value = 1
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" \t"))
        if current_name is not None and indent <= current_indent:
            current_name = None
        header = re.match(r"(?:enum|flag)\s+([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?$", stripped)
        if header is not None:
            current_name = header.group(1)
            current_indent = indent
            next_value = 1
            values.setdefault(current_name, {})
            continue
        if current_name is None:
            continue
        member_line = stripped.split("#", 1)[0].strip()
        member = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)", member_line)
        if member is None:
            continue
        values[current_name][member.group(1)] = next_value
        next_value *= 2
    return values


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
    if _leading_negative_expression(positional[0]):
        return "int256"
    start_type = infer_expr_type(positional[0], vars_for_line)
    if len(positional) > 1 and is_integer_type(start_type) and not _literal_integer(positional[0]):
        return start_type
    bound = positional[1] if len(positional) > 1 else positional[0]
    bound_type = infer_expr_type(bound, vars_for_line)
    return bound_type if is_integer_type(bound_type) else "uint256"


def _range_bound(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    config = rule_context.config
    facts = rule_context.facts
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
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
        arg_spans = split_top_level_arg_spans(raw_args)
        if arg_spans is None:
            continue
        args = [arg for _start, _end, arg in arg_spans]
        if len(args) == 1:
            if not _literal_integer(args[0]) and rule_context.is_enabled("VYD014"):
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
            args[0], args[1], integer_constant_values(source, config.source_ast)
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
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        for cast_edit, before, after in _range_stop_max_value_cast_edits(
            args[0], arg_spans[1], open_index + 1, vars_for_line
        ):
            edits.append(cast_edit)
            fixes.append(
                Fix(
                    "VY071",
                    line_number(source, cast_edit.start),
                    "converted max_value range delta to start type",
                    before,
                    after,
                )
            )
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


def _range_stop_max_value_cast_edits(
    start: str,
    stop_span: tuple[int, int, str],
    args_offset: int,
    vars_for_line: dict[str, str],
) -> list[tuple[TextEdit, str, str]]:
    start_type = normalize_type(infer_expr_type(start, vars_for_line) or "")
    if not start_type.startswith("uint"):
        return []
    stop_start, _stop_end, stop = stop_span
    edits: list[tuple[TextEdit, str, str]] = []
    for match in re.finditer(r"\bmax_value\s*\(\s*(uint\d+)\s*\)", stop):
        value_type = normalize_type(match.group(1))
        if value_type == start_type:
            continue
        before = match.group(0)
        after = f"convert({before}, {start_type})"
        start_index = args_offset + stop_start + match.start()
        edits.append((TextEdit(start_index, start_index + len(before), after), before, after))
    return edits


def _infer_range_bound(start: str, stop: str, values: dict[str, int] | None = None) -> str | None:
    start = start.strip()
    stop = stop.strip()
    values = values or {}
    escaped = re.escape(start)
    plus_match = re.fullmatch(rf"{escaped}\s*\+\s*(.+)", stop)
    if plus_match:
        return _range_bound_literal(plus_match.group(1), values)
    minus_match = re.fullmatch(rf"{escaped}\s*-\s*(.+)", stop)
    if minus_match:
        return _range_bound_literal(minus_match.group(1), values)
    return None


def _range_bound_literal(value: str, values: dict[str, int]) -> str | None:
    value = value.strip()
    if _literal_integer(value):
        return value
    constant = eval_integer_constant_expr(value, values)
    if constant is None or constant < 0:
        return None
    return str(constant)


def _negative_range_bound_name(expr: str) -> str | None:
    match = re.fullmatch(
        r"\(?\s*-1\s*\*\s*([A-Za-z_][A-Za-z0-9_]*)\s*/{1,2}\s*[^()]+\s*\)?",
        expr.strip(),
    )
    return match.group(1) if match is not None else None


def _range_bound_uses_name(expr: str, name: str) -> bool:
    return bool(
        re.fullmatch(
            rf"\(?\s*(?:-1\s*\*\s*)?{re.escape(name)}\s*/{{1,2}}\s*[^()]+\s*\)?",
            expr.strip(),
        )
    )


def _leading_negative_expression(expr: str) -> bool:
    return bool(re.match(r"\s*\(*\s*-", expr))


def _range_iterable_has_leading_negative_start(iterable: str) -> bool:
    match = re.match(r"range\s*\((.*)\)\s*$", iterable)
    if match is None:
        return _leading_negative_expression(iterable)
    args = split_top_level_args(match.group(1))
    return bool(args and _leading_negative_expression(args[0]))


def _range_iterable_has_literal_bounds(iterable: str) -> bool:
    match = re.match(r"range\s*\((.*)\)\s*$", iterable)
    if match is None:
        return False
    args = split_top_level_args(match.group(1))
    return bool(args) and all(_literal_integer(arg.strip()) for arg in args)


def _narrow_unsigned_range_loop_operand_type(
    following_source: str, loop_var: str, vars_for_line: dict[str, str]
) -> str | None:
    candidate_types: set[str] = set()
    for name, type_name in vars_for_line.items():
        normalized = normalize_type(type_name)
        if not _is_narrow_unsigned_integer_type(normalized):
            continue
        name_ref = rf"(?:self\.)?{re.escape(name)}"
        if re.search(
            rf"\b(?:{re.escape(loop_var)}\s*(?:[<>]=?|==|!=)\s*{name_ref}|{name_ref}\s*(?:[<>]=?|==|!=)\s*{re.escape(loop_var)}|{name_ref}\s*[-+]\s*{re.escape(loop_var)}|{re.escape(loop_var)}\s*[-+]\s*{name_ref})\b",
            following_source,
        ):
            candidate_types.add(normalized)
    return next(iter(candidate_types)) if len(candidate_types) == 1 else None


def _is_narrow_unsigned_integer_type(type_name: str) -> bool:
    match = re.fullmatch(r"uint(\d+)", normalize_type(type_name))
    return match is not None and int(match.group(1)) < 256


def _negative_range_loop_operand_type(
    following_source: str, loop_var: str, vars_for_line: dict[str, str]
) -> str | None:
    for name, type_name in vars_for_line.items():
        normalized = normalize_type(type_name)
        if not _is_signed_integer_type(normalized):
            continue
        if re.search(
            rf"\b(?:{re.escape(loop_var)}\s*\*\s*{re.escape(name)}|{re.escape(name)}\s*\*\s*{re.escape(loop_var)})\b",
            following_source,
        ):
            return normalized
    return None


RULES = (
    Rule(
        "signed_negative_range_bounds",
        runner=_signed_negative_range_bounds,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
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
    Rule(
        "flag_or_enum_return_casts",
        runner=_flag_or_enum_return_casts,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
    Rule("integer_assignment_casts", runner=_integer_assignment_casts, changes=(crossing("VY052", (0, 4, 0)),)),
)
