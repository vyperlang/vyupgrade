from __future__ import annotations

import re

from ..analysis import SourceFacts
from ..source import code_mask, find_matching, span_is_code


def external_call_matches(
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
