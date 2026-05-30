from __future__ import annotations

import re

from ..analysis import infer_expr_type, is_integer_type
from ..models import Diagnostic, Fix
from ..rule_helpers import replace_identifier_expr as _replace_identifier_expr
from ..rule_registry import Rule, RuleContext, crossing
from ..source import TextEdit, apply_edits, code_mask, line_number, span_is_code


def _pre_04_expression_rewrites(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    current = source
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if rule_context.is_enabled("VY220"):
        current, new_fixes = _replace_identifier_expr(
            current,
            "block.difficulty",
            "block.prevrandao",
            "VY220",
            "renamed block.difficulty to block.prevrandao",
        )
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY230"):
        current, new_fixes = _remove_unary_plus(current)
        fixes.extend(new_fixes)
    if rule_context.any_enabled({"VY231", "VYD013"}):
        current_context = rule_context.with_source(current)
        current, new_fixes, new_diagnostics = _replace_numeric_not(current_context)
        fixes.extend(new_fixes)
        diagnostics.extend(new_diagnostics)
    return current, fixes, diagnostics


def _remove_unary_plus(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(
        r"(?P<prefix>(?:^|[=(,\[\{]\s*))\+(?P<expr>[A-Za-z_][A-Za-z0-9_.]*)",
        re.MULTILINE,
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
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
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
            if rule_context.is_enabled("VYD013"):
                diagnostics.append(
                    Diagnostic(
                        "VYD013", line, f"cannot infer whether 'not {expr}' is numeric or boolean"
                    )
                )
            continue
        if not is_integer_type(expr_type):
            continue
        replacement = f"{expr} == 0"
        if not rule_context.is_enabled("VY231"):
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


PRE_INTERFACE_RULES = (
    Rule(
        "pre_04_expression_rewrites",
        context_runner=_pre_04_expression_rewrites,
        changes=(
            crossing("VY220", (0, 3, 7)),
            crossing("VY230", (0, 3, 8)),
            crossing("VY231", (0, 3, 8)),
            crossing("VYD013", (0, 3, 8)),
        ),
    ),
)
