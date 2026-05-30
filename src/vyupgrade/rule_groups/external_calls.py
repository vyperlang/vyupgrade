from __future__ import annotations

import re

from ..analysis import SourceFacts, infer_expr_type, normalize_type, parse_source_facts, unwrap_type
from ..models import Config, Diagnostic, Fix
from ..rule_helpers import innermost_non_overlapping as _innermost_non_overlapping
from ..rule_registry import Rule, RuleContext, any_enabled as _any_enabled, crossing, is_enabled as _enabled
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
    span_is_code,
)
from ..versions import MigrationContext


def ignored_external_call_results(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    if not rule_context.is_enabled("VY057"):
        return source, [], []
    facts = rule_context.facts
    taken_names = code_identifiers(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        line_no = line_number(source, offset)
        code_part, comment_part = _split_inline_comment_preserving_strings(line)
        stripped = code_part.strip()
        if (
            not stripped.startswith("staticcall ")
            or _delimiter_depth_before(source, offset) != 0
            or _previous_code_line_continues(source, offset)
        ):
            offset += len(raw_line)
            continue
        indent = code_part[: len(code_part) - len(code_part.lstrip(" \t"))]
        expr_start = offset + len(indent)
        keyword_match = re.match(r"(?:staticcall|extcall)\s+", source[expr_start:])
        if keyword_match is None:
            offset += len(raw_line)
            continue
        expr_end = external_call_expression_end(source, expr_start + keyword_match.end())
        if expr_end is None or source[expr_end : offset + len(code_part)].strip():
            offset += len(raw_line)
            continue
        expr = source[expr_start:expr_end]
        expr_type = infer_expr_type(expr, facts.vars_at_line(line_no), facts)
        if expr_type is None:
            offset += len(raw_line)
            continue
        name = _discard_assignment_name(line_no, taken_names)
        replacement = f"{indent}{name}: {unwrap_type(expr_type)} = {expr}{comment_part}"
        edits.append(TextEdit(offset, offset + len(line), replacement))
        fixes.append(
            Fix(
                "VY057",
                line_no,
                "assigned ignored external call result",
                line,
                replacement,
            )
        )
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


def external_call_expression_end(source: str, start: int) -> int | None:
    cast_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", source[start:])
    if cast_match is not None:
        cast_open = start + cast_match.end() - 1
        cast_close = find_matching(source, cast_open)
        if cast_close is not None:
            method_match = re.match(
                r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source[cast_close + 1 :]
            )
            if method_match is not None:
                method_open = cast_close + 1 + method_match.end() - 1
                method_close = find_matching(source, method_open)
                if method_close is not None:
                    return method_close + 1

    open_index = source.find("(", start)
    if open_index == -1:
        return None
    close = find_matching(source, open_index)
    return None if close is None else close + 1


def _delimiter_depth_before(source: str, end: int) -> int:
    mask = code_mask(source[:end])
    depth = 0
    for index, char in enumerate(source[:end]):
        if not mask[index]:
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
    return depth


def _previous_code_line_continues(source: str, offset: int) -> bool:
    if offset <= 0 or source[offset - 1] != "\n":
        return False
    previous_end = offset - 1
    previous_start = source.rfind("\n", 0, previous_end) + 1
    code_part, _comment_part = _split_inline_comment_preserving_strings(
        source[previous_start:previous_end]
    )
    return code_part.rstrip().endswith("\\")


def _discard_assignment_name(line_no: int, taken_names: set[str]) -> str:
    base = f"__vyupgrade_discard_{line_no}"
    candidate = base
    suffix = 2
    while candidate in taken_names:
        candidate = f"{base}_{suffix}"
        suffix += 1
    taken_names.add(candidate)
    return candidate


def _split_inline_comment_preserving_strings(line: str) -> tuple[str, str]:
    quote: str | None = None
    i = 0
    while i < len(line):
        char = line[i]
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        if char == "#":
            code = line[:i].rstrip()
            spacer = "  " if code else ""
            return code, spacer + line[i:]
        i += 1
    return line, ""


def _external_call_keywords(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY040", "VY041", "VYD003"}, config, context):
        return source, [], []
    current = source
    all_fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    for _ in range(3):
        current, fixes, diagnostics = _external_call_keywords_once(current, config, context)
        all_fixes.extend(fixes)
        if not fixes:
            break
    return current, all_fixes, diagnostics


def _external_call_keywords_once(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for start, end, target, method, cast_type in _all_external_call_matches(source, facts):
        if not span_is_code(mask, start, end):
            continue
        prefix = source[max(0, start - 16) : start]
        if target == "self" or method in {"append", "pop"}:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, start))
        if target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(
                target, vars_for_line, facts
            )
        else:
            target_type = cast_type or infer_expr_type(target, vars_for_line, facts)
        mutability = facts.interfaces.get(normalize_type(target_type or ""), {}).get(method)
        if mutability is None:
            if _enabled("VYD003", config, context):
                diagnostics.append(
                    Diagnostic(
                        "VYD003",
                        line_number(source, start),
                        f"cannot infer mutability for external call {target}.{method}",
                    )
                )
            continue
        keyword = "staticcall" if mutability in {"view", "pure"} else "extcall"
        rule = "VY041" if keyword == "staticcall" else "VY040"
        if not _enabled(rule, config, context):
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
                    line_number(source, start),
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
                line_number(source, start),
                f"added {keyword} to {mutability} external call",
                source[start:end].rstrip(),
                keyword + " " + source[start:end].rstrip(),
            )
        )

    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, diagnostics


def _interface_cast_call_matches(
    source: str, interfaces: dict[str, dict[str, str]]
) -> list[tuple[int, int, str, str, str]]:
    matches: list[tuple[int, int, str, str, str]] = []
    mask = code_mask(source)
    for interface_name in sorted(interfaces, key=len, reverse=True):
        for match in re.finditer(rf"(?<![\w.]){re.escape(interface_name)}\s*\(", source):
            open_index = source.find("(", match.start())
            close = find_matching(source, open_index)
            if close is None or not span_is_code(mask, match.start(), min(close + 1, len(source))):
                continue
            tail = re.match(r"(?:\s|\\)*\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source[close + 1 :])
            if tail is None:
                continue
            end = close + 1 + tail.end()
            matches.append(
                (
                    match.start(),
                    end,
                    source[match.start() : close + 1],
                    tail.group(1),
                    interface_name,
                )
            )
    return matches


def _external_call_subscripts(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY042", config, context):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\b(?:staticcall|extcall)\s+", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        expression_end = external_call_expression_end(source, match.end())
        if expression_end is None:
            continue
        if expression_end >= len(source) or source[expression_end] not in "[.":
            continue
        before = source[match.start() : expression_end]
        after = f"({before})"
        edits.append(TextEdit(match.start(), expression_end, after))
        fixes.append(
            Fix(
                "VY042",
                line_number(source, match.start()),
                "parenthesized external call before subscript",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []

def _all_external_call_matches(
    source: str, facts: SourceFacts
) -> list[tuple[int, int, str, str, str | None]]:
    target_expr = (
        r"(?:self\.)?[A-Za-z_][A-Za-z0-9_]*"
        r"(?:\[[^\]\n]+\])?"
        r"(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\n]+\])?)*"
    )
    variable_call_re = re.compile(
        rf"(?<![\w.])(?P<target>{target_expr})\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\("
    )
    matches: list[tuple[int, int, str, str, str | None]] = []
    matches.extend(_interface_cast_call_matches(source, facts.interfaces))
    matches.extend(_parenthesized_external_call_matches(source))
    matches.extend(
        (match.start(), match.end(), match.group("target"), match.group("method"), None)
        for match in variable_call_re.finditer(source)
    )
    return sorted(matches)


def _parenthesized_external_call_matches(
    source: str,
) -> list[tuple[int, int, str, str, str | None]]:
    matches: list[tuple[int, int, str, str, str | None]] = []
    mask = code_mask(source)
    pattern = re.compile(r"\(\s*(?:staticcall|extcall)\s+")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, match.start())
        if close is None:
            continue
        tail = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source[close + 1 :])
        if tail is None:
            continue
        matches.append(
            (
                match.start(),
                close + 1 + tail.end(),
                source[match.start() : close + 1],
                tail.group(1),
                None,
            )
        )
    return matches


RULES = (
    Rule(
        "external_call_keywords",
        runner=_external_call_keywords,
        changes=(
            crossing("VY040", (0, 4, 0)),
            crossing("VY041", (0, 4, 0)),
            crossing("VYD003", (0, 4, 0)),
        ),
    ),
    Rule("external_call_subscripts", runner=_external_call_subscripts, changes=(crossing("VY042", (0, 4, 0)),)),
    Rule(
        "external_call_keywords_after_subscripts",
        runner=_external_call_keywords,
        changes=(
            crossing("VY040", (0, 4, 0)),
            crossing("VY041", (0, 4, 0)),
            crossing("VYD003", (0, 4, 0)),
        ),
    ),
    Rule(
        "ignored_external_call_results",
        context_runner=ignored_external_call_results,
        changes=(crossing("VY057", (0, 4, 0)),),
    ),
)
