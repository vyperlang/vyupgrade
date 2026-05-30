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
    function_start_at_line as _function_start_at_line,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    literal_integer as _literal_integer,
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
from .numeric_constants import _integer_constant_values
from .numeric_types import (
    is_signed_integer_type as _is_signed_integer_type,
    is_unsigned_integer_type as _is_unsigned_integer_type,
)


def _typed_range_loops(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
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


RULES = (
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
