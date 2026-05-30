from __future__ import annotations

import re
from collections.abc import Iterator

from ..models import Fix
from ..source import TextEdit, apply_edits, code_mask, find_matching, line_number, span_is_code


def replace_identifier_call(source: str, old: str, new: str, rule: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(rf"\b{re.escape(old)}\s*(?=\()", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(), match.start() + len(old), new))
        fixes.append(
            Fix(rule, line_number(source, match.start()), f"renamed legacy {old} builtin", old, new)
        )
    return apply_edits(source, edits), fixes


def iter_calls(
    source: str, call_name: str, mask: list[bool] | None = None
) -> Iterator[tuple[re.Match[str], int, int, str]]:
    if mask is None:
        mask = code_mask(source)
    for match in re.finditer(rf"\b{re.escape(call_name)}\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = match.end() - 1
        close = find_matching(source, open_index)
        if close is not None:
            yield match, open_index, close, source[open_index + 1 : close]

