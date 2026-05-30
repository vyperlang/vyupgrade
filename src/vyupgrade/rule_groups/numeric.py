from __future__ import annotations

import re

from ..analysis import (
    SourceFacts,
    infer_expr_type,
    is_integer_type,
    iterable_element_type,
    normalize_type,
    parse_source_facts,
)
from ..models import Config, Diagnostic, Fix
from ..rule_helpers import (
    find_matching_open as _find_matching_open,
    function_start_at_line as _function_start_at_line,
    innermost_non_overlapping as _innermost_non_overlapping,
    insert_import as _insert_import,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    literal_integer as _literal_integer,
    replace_identifier_expr as _replace_identifier_expr,
)
from ..rule_registry import Rule, any_enabled as _any_enabled, crossing, is_enabled as _enabled
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    find_matching,
    line_number,
    split_top_level_args,
    span_is_code,
)
from ..versions import MigrationContext
from .numeric_constants import (
    _integer_constant_values,
)
from .numeric_signedness import _vars_for_argument
from .numeric_types import (
    is_signed_integer_type as _is_signed_integer_type,
    is_unsigned_integer_type as _is_unsigned_integer_type,
)


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

INTEGER_DIVISION_RULES = (
    Rule(
        "integer_division",
        runner=_integer_division,
        changes=(
            crossing("VY050", (0, 4, 0)),
            crossing("VYD004", (0, 4, 0)),
        ),
    ),
)



REDUNDANT_CONVERT_RULES = (
    Rule("redundant_integer_convert", runner=_redundant_integer_convert, changes=(crossing("VY051", (0, 4, 0)),)),
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
