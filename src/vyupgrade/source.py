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
    mask = [True] * len(source)
    i = 0
    string_quote: str | None = None
    triple = False
    while i < len(source):
        char = source[i]
        if string_quote is not None:
            mask[i] = False
            if char == "\\" and not triple:
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
    return mask


def span_is_code(mask: list[bool], start: int, end: int) -> bool:
    return start >= 0 and end <= len(mask) and all(mask[start:end])


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


def split_top_level_args(text: str) -> list[str] | None:
    args: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == "\\":
                continue
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif char == "," and depth == 0:
            args.append(text[start:index].strip())
            start = index + 1
    if depth != 0 or quote is not None:
        return None
    tail = text[start:].strip()
    if tail:
        args.append(tail)
    return args


def find_matching(source: str, open_index: int, open_char: str = "(", close_char: str = ")") -> int | None:
    depth = 0
    quote: str | None = None
    i = open_index
    while i < len(source):
        char = source[i]
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
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None

