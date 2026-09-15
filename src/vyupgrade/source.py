from __future__ import annotations

import re
from dataclasses import dataclass


IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class TextEdit:
    start: int
    end: int
    replacement: str


def line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def code_mask(source: str) -> list[bool]:
    return _scan_source(source)[0]


def _scan_source(source: str) -> tuple[list[bool], bool]:
    mask = [True] * len(source)
    i = 0
    string_quote: str | None = None
    triple = False
    while i < len(source):
        char = source[i]
        if string_quote is not None:
            mask[i] = False
            if char == "\\":
                if i + 1 < len(source):
                    mask[i + 1] = False
                i += 2
                continue
            if triple and source.startswith(string_quote * 3, i):
                for j in range(i, min(i + 3, len(source))):
                    mask[j] = False
                i += 3
                string_quote = None
                triple = False
                continue
            if not triple and char == string_quote:
                string_quote = None
            i += 1
            continue

        if char == "#":
            while i < len(source) and source[i] != "\n":
                mask[i] = False
                i += 1
            continue

        if char in {"'", '"'}:
            string_quote = char
            triple = source.startswith(char * 3, i)
            width = 3 if triple else 1
            for j in range(i, min(i + width, len(source))):
                mask[j] = False
            i += width
            continue

        i += 1
    return mask, string_quote is None


def span_is_code(mask: list[bool], start: int, end: int) -> bool:
    return start >= 0 and end <= len(mask) and all(mask[start:end])


def line_starts_in_code(source: str, mask: list[bool], offset: int) -> bool:
    """Return whether the line containing ``offset`` begins outside a string."""
    line_start = source.rfind("\n", 0, offset) + 1
    if line_start > 0 and not mask[line_start - 1]:
        return False
    first = line_start
    while first < len(source) and source[first] in " \t":
        first += 1
    return span_is_code(mask, line_start, first)


def code_identifiers(source: str) -> set[str]:
    mask = code_mask(source)
    return {
        match.group(0)
        for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", source)
        if span_is_code(mask, match.start(), match.end())
    }


def replace_identifier(source: str, name: str, replacement: str) -> tuple[str, list[TextEdit]]:
    mask = code_mask(source)
    edits: list[TextEdit] = []
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    for match in pattern.finditer(source):
        if span_is_code(mask, match.start(), match.end()):
            edits.append(TextEdit(match.start(), match.end(), replacement))
    return apply_edits(source, edits), edits


def apply_edits(source: str, edits: list[TextEdit]) -> str:
    if not edits:
        return source
    pieces: list[str] = []
    cursor = 0
    for edit in sorted(edits, key=lambda item: item.start):
        if edit.start < cursor:
            raise ValueError("overlapping edits")
        pieces.append(source[cursor : edit.start])
        pieces.append(edit.replacement)
        cursor = edit.end
    pieces.append(source[cursor:])
    return "".join(pieces)


def split_top_level_arg_spans(
    text: str,
    *,
    include_empty_separators: bool = False,
) -> list[tuple[int, int, str]] | None:
    mask, complete = _scan_source(text)
    if not complete:
        return None
    spans: list[tuple[int, int, str]] = []
    start = 0
    stack: list[str] = []
    for index, char in enumerate(text):
        if not mask[index]:
            continue
        if char in _CLOSERS:
            stack.append(_CLOSERS[char])
        elif char in _OPENERS:
            if not stack or stack.pop() != char:
                return None
        elif char == "," and not stack:
            _append_arg_span(spans, text, start, index, include_empty=include_empty_separators)
            start = index + 1
    if stack:
        return None
    _append_arg_span(spans, text, start, len(text), include_empty=False)
    return spans


def split_top_level_args(text: str) -> list[str] | None:
    spans = split_top_level_arg_spans(text, include_empty_separators=True)
    return None if spans is None else [arg for _start, _end, arg in spans]


def _append_arg_span(
    spans: list[tuple[int, int, str]], text: str, start: int, end: int, *, include_empty: bool
) -> None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end or include_empty:
        spans.append((start, end, text[start:end]))


_CLOSERS = {"(": ")", "[": "]", "{": "}"}
_OPENERS = {close: opening for opening, close in _CLOSERS.items()}


def _matching_delimiter(
    source: str, index: int, opening: str, closing: str, *, reverse: bool = False
) -> int | None:
    pairs = _OPENERS if reverse else _CLOSERS
    if pairs.get(opening) != closing:
        return None
    if not 0 <= index < len(source) or source[index] != opening:
        return None
    mask = code_mask(source)
    if not mask[index]:
        return None
    stack: list[str] = []
    indices = range(index, -1, -1) if reverse else range(index, len(source))
    for offset in indices:
        if not mask[offset]:
            continue
        char = source[offset]
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return offset
    return None


def find_matching(
    source: str, open_index: int, open_char: str = "(", close_char: str = ")"
) -> int | None:
    return _matching_delimiter(source, open_index, open_char, close_char)


def find_matching_open(
    source: str, close_index: int, open_char: str = "(", close_char: str = ")"
) -> int | None:
    return _matching_delimiter(source, close_index, close_char, open_char, reverse=True)
