from __future__ import annotations

import re

from ..analysis import infer_expr_type, unwrap_type
from ..models import Diagnostic, Fix
from ..rule_registry import RuleContext
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
)


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
