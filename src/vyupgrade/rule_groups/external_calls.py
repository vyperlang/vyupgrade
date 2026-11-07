from __future__ import annotations

import ast
import re

from ..analysis import SourceFacts, infer_expr_type, normalize_type, unwrap_type
from ..models import Diagnostic, Fix
from ..rule_helpers import innermost_non_overlapping as _innermost_non_overlapping
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    find_matching,
    line_number,
    logical_statement_tokens,
    lone_cr_as_lf,
    span_is_code,
)
from .compiler_calls import discard_assignment_name, proven_call_keywords
from .external_call_helpers import external_call_matches


def ignored_external_call_results(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    taken_names = code_identifiers(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    offsets = rule_context.line_offsets
    for statement in logical_statement_tokens(lone_cr_as_lf(source)):
        first = statement[0]
        if first.string != "staticcall" or len(statement) < 2:
            continue
        line, column = first.start
        start = offsets[line - 1] + column
        receiver_line, column = statement[1].start
        receiver = offsets[receiver_line - 1] + column
        last_line, column = statement[-1].end
        end = offsets[last_line - 1] + column
        if external_call_expression_end(source, receiver) != end:
            continue
        expression = source[start:end]
        typ = infer_expr_type(expression, facts.vars_at_line(line), facts)
        if typ is None:
            continue
        name = discard_assignment_name(line, taken_names)
        prefix = f"{name}: {unwrap_type(typ)} = "
        edits.append(TextEdit(start, start, prefix))
        fixes.append(
            Fix(
                "VY057",
                line,
                "assigned ignored external call result",
                expression,
                prefix + expression,
            )
        )
    return apply_edits(source, edits), fixes, []


def external_call_expression_end(source: str, start: int) -> int | None:
    if source[start] == "(":
        target_close = find_matching(source, start)
        if target_close is not None:
            chained_end = _chained_call_end(source, target_close)
            if chained_end is not None:
                return chained_end

    cast_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", source[start:])
    if cast_match is not None:
        target_open = start + cast_match.end() - 1
        target_close = find_matching(source, target_open)
        if target_close is not None:
            chained_end = _chained_call_end(source, target_close)
            if chained_end is not None:
                return chained_end

    call_match = re.match(r"self\.[A-Za-z_][A-Za-z0-9_]*\s*\(", source[start:])
    if call_match is not None:
        target_open = start + call_match.end() - 1
        target_close = find_matching(source, target_open)
        if target_close is not None:
            chained_end = _chained_call_end(source, target_close)
            if chained_end is not None:
                return chained_end

    open_index = source.find("(", start)
    if open_index == -1:
        return None
    close = find_matching(source, open_index)
    return None if close is None else close + 1


def _chained_call_end(source: str, target_close: int) -> int | None:
    method_match = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source[target_close + 1 :])
    if method_match is None:
        return None
    method_open = target_close + 1 + method_match.end() - 1
    method_close = find_matching(source, method_open)
    return None if method_close is None else method_close + 1


def _external_call_keywords(
    rule_context: RuleContext,
    owned_functions: set[str],
) -> tuple[str, list[Fix], list[Diagnostic]]:
    current = rule_context.source
    all_fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    for _ in range(3):
        current_context = rule_context.with_source(current)
        current, fixes, diagnostics = _external_call_keywords_once(current_context, owned_functions)
        all_fixes.extend(fixes)
        if not fixes:
            break
    return current, all_fixes, diagnostics


def _claimed_rows(facts: SourceFacts, names: set[str]) -> set[int]:
    return {
        row
        for start, name in facts.function_names.items()
        if name in names
        for row in range(start, facts.function_ends.get(start, start) + 1)
    }


def _conditional_receiver_type(
    target: str, variables: dict[str, str], facts: SourceFacts
) -> str | None:
    if "if" not in target:
        return None
    try:
        expr = ast.parse(target, mode="eval").body
    except SyntaxError:
        return None
    if isinstance(expr, ast.IfExp):
        left = infer_expr_type(ast.get_source_segment(target, expr.body) or "", variables, facts)
        right = infer_expr_type(ast.get_source_segment(target, expr.orelse) or "", variables, facts)
        if left == right:
            return left
    return None


def _external_call_keywords_once(
    rule_context: RuleContext,
    owned_functions: set[str],
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    fixes: list[Fix] = []
    unknown: dict[int, Diagnostic] = {}
    resolved: set[int] = set()
    protected_lines = _claimed_rows(facts, owned_functions)
    text = lone_cr_as_lf(source)
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    for start, end, target, method, cast_type in external_call_matches(source, facts):
        if not span_is_code(mask, start, min(start + 1, len(source))):
            continue
        prefix = source[max(0, start - 16) : start]
        if target == "self":
            continue
        row = line_number(text, start)
        if row in protected_lines or end in resolved:
            continue
        vars_for_line = facts.vars_at_line(row)
        if target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(
                target, vars_for_line, facts
            )
        else:
            target_type = (
                cast_type
                or infer_expr_type(target, vars_for_line, facts)
                or _conditional_receiver_type(target, vars_for_line, facts)
            )
        if method in {"append", "pop"} and (target_type or "").startswith("DynArray["):
            resolved.add(end)
            unknown.pop(end, None)
            continue
        mutability = facts.interfaces.get(normalize_type(target_type or ""), {}).get(method)
        if mutability is None:
            if rule_context.is_enabled("VYD003"):
                unknown.setdefault(
                    end,
                    Diagnostic(
                        "VYD003",
                        row,
                        f"cannot infer mutability for external call {target}.{method}",
                    ),
                )
            continue
        resolved.add(end)
        unknown.pop(end, None)
        keyword = "staticcall" if mutability in {"view", "pure"} else "extcall"
        rule = "VY041" if keyword == "staticcall" else "VY040"
        if not rule_context.is_enabled(rule):
            continue
        existing_keyword = re.search(r"\b(?P<keyword>extcall|staticcall)\s+$", prefix)
        if existing_keyword is not None:
            if existing_keyword.group("keyword") == keyword:
                continue
            keyword_start = start - (len(prefix) - existing_keyword.start("keyword"))
            edits.append(
                TextEdit(
                    keyword_start, keyword_start + len(existing_keyword.group("keyword")), keyword
                )
            )
            fixes.append(
                Fix(
                    rule,
                    row,
                    f"changed external call keyword to {keyword}",
                    existing_keyword.group("keyword"),
                    keyword,
                )
            )
            continue
        edits.append(TextEdit(start, start, keyword + " "))
        fixes.append(
            Fix(
                rule,
                row,
                f"added {keyword} to {mutability} external call",
                source[start:end].rstrip(),
                keyword + " " + source[start:end].rstrip(),
            )
        )

    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, list(unknown.values())


def _external_call_subscripts(
    rule_context: RuleContext, owned_functions: set[str]
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    protected_lines = _claimed_rows(rule_context.facts, owned_functions)
    text = lone_cr_as_lf(source)
    for match in re.finditer(r"\b(?:staticcall|extcall)\s+", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        row = line_number(text, match.start())
        if row in protected_lines:
            continue
        expression_end = external_call_expression_end(source, match.end())
        if expression_end is None:
            continue
        if expression_end >= len(source) or source[expression_end] not in "[.":
            continue
        before = source[match.start() : expression_end]
        after = f"({before})"
        edits.extend(
            (
                TextEdit(match.start(), match.start(), "("),
                TextEdit(expression_end, expression_end, ")"),
            )
        )
        fixes.append(
            Fix(
                "VY042",
                row,
                "parenthesized external call before subscript",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _external_call_syntax(context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    keywords = context.any_enabled({"VY040", "VY041", "VYD003"})
    proven = proven_call_keywords(context) if keywords else None
    source, fixes, owned_functions = proven if proven is not None else (context.source, [], set())
    diagnostics: list[Diagnostic] = []
    if keywords:
        source, first_fixes, _ = _external_call_keywords(
            context.with_source(source), owned_functions
        )
        fixes.extend(first_fixes)
    if context.is_enabled("VY042"):
        source, subscript_fixes, _ = _external_call_subscripts(
            context.with_source(source), owned_functions
        )
        fixes.extend(subscript_fixes)
    if keywords:
        source, more_fixes, diagnostics = _external_call_keywords(
            context.with_source(source), owned_functions
        )
        fixes.extend(more_fixes)
    return source, fixes, diagnostics


RULES = (
    Rule(
        "external_call_syntax",
        runner=_external_call_syntax,
        changes=(
            crossing("VY040", (0, 4, 0)),
            crossing("VY041", (0, 4, 0)),
            crossing("VY042", (0, 4, 0)),
            crossing("VYD003", (0, 4, 0)),
        ),
    ),
    Rule(
        "ignored_external_call_results",
        runner=ignored_external_call_results,
        changes=(crossing("VY057", (0, 4, 0)),),
    ),
)
