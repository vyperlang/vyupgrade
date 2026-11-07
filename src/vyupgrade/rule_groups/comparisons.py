from __future__ import annotations

import re

from ..analysis import infer_expr_type, normalize_type
from ..models import Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..source import TextEdit, apply_edits, code_identifiers, line_number, span_is_code


def not_in_comparator(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    pattern = re.compile(
        r"\bnot\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s+in\s+([A-Za-z_][A-Za-z0-9_.]*)\s*\)"
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = f"{match.group(1)} not in {match.group(2)}"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY211",
                line_number(source, match.start()),
                "changed negated membership test to not in",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _empty_comparison(
    expr: str, type_name: str, op: str, fields: list[tuple[str, str]],
    source: str, helpers: dict[tuple[str, str], tuple[str, str]],
) -> str:
    def comparison(value: str) -> str:
        joiner = " and " if op == "==" else " or "
        return "(" + joiner.join(f"{value}{field} {op} empty({typ})" for field, typ in fields) + ")"

    if all(re.fullmatch(r"\w+", index.strip()) for index in re.findall(r"\[([^]]+)\]", expr)):
        return comparison(expr)

    # An internal call evaluates the operand once, at its original position
    # even inside a short-circuit condition. Reuse one helper per type/operator.
    key = (type_name, op)
    if key not in helpers:
        taken = code_identifiers(source) | {name for name, _ in helpers.values()}
        argument = "_item"
        while argument in taken:
            argument += "_"
        name = "_vyupgrade_compare_empty"
        while name in taken:
            name += "_"
        body = (
            f"@internal\n@pure\ndef {name}({argument}: {type_name}) -> bool:\n"
            f"    return {comparison(argument)}\n"
        )
        helpers[key] = (name, body)
    name, _ = helpers[key]
    return f"self.{name}({expr})"


def _apply_comparisons(
    source: str, edits: list[TextEdit], helpers: dict[tuple[str, str], tuple[str, str]]
) -> str:
    result = apply_edits(source, edits)
    if helpers:
        result = result.rstrip("\n") + "\n\n" + "\n".join(body for _, body in helpers.values())
    return result


def fixed_array_empty_comparisons(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    helpers: dict[tuple[str, str], tuple[str, str]] = {}
    mask = rule_context.code_mask
    pattern = re.compile(
        r"(?<![\w.])(?P<expr>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*)"
        r"(?P<left_space>\s*)(?P<op>==|!=)(?P<right_space>\s*)"
        r"empty\s*\(\s*(?P<element>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(?P<size>[0-9]+)\s*\]\s*\)"
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        size = int(match.group("size"))
        if not (0 < size <= 16):
            continue
        line_no = line_number(source, match.start())
        expr = match.group("expr")
        expected_type = f"{match.group('element')}[{size}]"
        actual_type = rule_context.facts.vars_at_line(line_no).get(expr.removeprefix("self."))
        if (
            actual_type is not None
            and normalize_type(actual_type) != match.group("element")
            and actual_type.strip() != expected_type
        ):
            continue
        op = match.group("op")
        fields = [(f"[{index}]", match.group("element")) for index in range(size)]
        replacement = _empty_comparison(expr, expected_type, op, fields, source, helpers)
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY213",
                line_no,
                "expanded fixed array empty comparison",
                match.group(0),
                replacement,
            )
        )
    return _apply_comparisons(source, edits, helpers), fixes, []


def struct_empty_comparisons(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    helpers: dict[tuple[str, str], tuple[str, str]] = {}
    mask = rule_context.code_mask
    pattern = re.compile(
        r"(?<![\w.])(?P<expr>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*)"
        r"(?P<left_space>\s*)(?P<op>==|!=)(?P<right_space>\s*)"
        r"empty\s*\(\s*(?P<struct>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        struct_name = match.group("struct")
        struct_fields = rule_context.facts.struct_fields.get(struct_name)
        if not struct_fields:
            continue
        line_no = line_number(source, match.start())
        expr = match.group("expr")
        vars_for_line = rule_context.facts.vars_at_line(line_no)
        expr_type = infer_expr_type(expr, vars_for_line, rule_context.facts)
        if normalize_type(expr_type or "") != struct_name:
            continue
        op = match.group("op")
        fields = [(f".{field}", typ) for field, typ in struct_fields.items()]
        replacement = _empty_comparison(expr, struct_name, op, fields, source, helpers)
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY214",
                line_no,
                "expanded struct empty comparison",
                match.group(0),
                replacement,
            )
        )
    return _apply_comparisons(source, edits, helpers), fixes, []


RULES = (
    Rule(
        "not_in_comparator",
        runner=not_in_comparator,
        changes=(crossing("VY211", (0, 2, 8)),),
    ),
    Rule(
        "fixed_array_empty_comparisons",
        runner=fixed_array_empty_comparisons,
        changes=(crossing("VY213", (0, 4, 0)),),
    ),
    Rule(
        "struct_empty_comparisons",
        runner=struct_empty_comparisons,
        changes=(crossing("VY214", (0, 4, 0)),),
    ),
)
