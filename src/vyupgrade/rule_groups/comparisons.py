from __future__ import annotations

import re

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


RULES = (
    Rule(
        "not_in_comparator",
        runner=not_in_comparator,
        changes=(crossing("VY211", (0, 2, 8)),),
    ),
)
