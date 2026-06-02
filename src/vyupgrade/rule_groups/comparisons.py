from __future__ import annotations

import re

from ..analysis import normalize_type
from ..models import Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..source import TextEdit, apply_edits, line_number, span_is_code


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


def fixed_array_empty_comparisons(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    pattern = re.compile(
        r"(?P<expr>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])*)"
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
        joiner = " and " if op == "==" else " or "
        parts = [
            f"{expr}[{index}] {op} empty({match.group('element')})" for index in range(size)
        ]
        replacement = f"({joiner.join(parts)})"
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
    return apply_edits(source, edits), fixes, []


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
)
