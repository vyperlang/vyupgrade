from __future__ import annotations

import re
from pathlib import Path

from .models import Config, Fix
from .source import TextEdit, apply_edits, code_mask, line_number, span_is_code
from .versions import MigrationContext, VyperVersion


def line_match_starts_outside_string(source: str, mask: list[bool], start: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    if line_start > 0 and not mask[line_start - 1]:
        return False
    first = line_start
    while first < len(source) and source[first] in " \t":
        first += 1
    return span_is_code(mask, line_start, first)


def pre_021_context(context: MigrationContext) -> bool:
    return context.source_floor is None or context.source_floor < VyperVersion("0.2.1")


def innermost_non_overlapping(
    edits: list[TextEdit], fixes: list[Fix]
) -> tuple[list[TextEdit], list[Fix]]:
    selected: list[tuple[TextEdit, Fix]] = []
    for edit, fix in sorted(
        zip(edits, fixes, strict=True),
        key=lambda item: (item[0].end - item[0].start, item[0].start),
    ):
        if any(edit.start < kept.end and kept.start < edit.end for kept, _fix in selected):
            continue
        selected.append((edit, fix))
    selected.sort(key=lambda item: item[0].start)
    return [edit for edit, _fix in selected], [fix for _edit, fix in selected]


def line_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", source):
        offsets.append(match.end())
    return offsets


def insert_import(source: str, line: str) -> str:
    lines = source.splitlines(keepends=True)
    insert_at = 0
    while insert_at < len(lines) and (
        lines[insert_at].startswith("#pragma")
        or lines[insert_at].startswith("# @version")
        or lines[insert_at].strip() == ""
        or lines[insert_at].startswith('"""')
    ):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].startswith("import "):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].startswith("from "):
        insert_at += 1
    lines.insert(insert_at, line)
    return "".join(lines)


def remove_constructor_decorators(
    source: str,
    decorators_to_remove: set[str],
    rule: str,
    message: str,
    add_deploy: bool = False,
) -> tuple[str, list[Fix], list[Fix]]:
    lines = source.splitlines(keepends=True)
    fixes: list[Fix] = []
    insertions: list[Fix] = []
    out = list(lines)
    offset = 0
    for index, line in enumerate(lines):
        if not re.match(r"\s*def\s+__init__\s*\(", line):
            continue
        start = index
        while start > 0 and re.match(
            r"\s*@[A-Za-z_][A-Za-z0-9_]*(?:\(.*\))?\s*(?:#.*)?$", lines[start - 1]
        ):
            start -= 1
        decorators = [decor.strip() for decor in lines[start:index]]
        insert_at = start + offset
        remove_indices: list[int] = []
        has_deploy = any(decor.startswith("@deploy") for decor in decorators)
        for rel, decor in enumerate(decorators):
            decor_name = decor.split("(", 1)[0].split("#", 1)[0].strip()
            if decor_name in decorators_to_remove:
                remove_indices.append(start + rel)
        for original_index in sorted(remove_indices, reverse=True):
            before = out[original_index + offset].rstrip("\n")
            del out[original_index + offset]
            offset -= 1
            fixes.append(Fix(rule, original_index + 1, message, before, ""))
        if add_deploy and not has_deploy:
            indent = re.match(r"(\s*)", line).group(1)
            out.insert(insert_at, f"{indent}@deploy\n")
            offset += 1
            insertions.append(
                Fix(rule, index + 1, "added @deploy to constructor", "", f"{indent}@deploy")
            )
    return "".join(out), fixes, insertions


def nested_under_config_path(path: Path, config: Config) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in config.paths:
        try:
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return resolved.parent != root.resolve()
    return path.parent != Path(".")


def replace_identifier_expr(
    source: str,
    before: str,
    after: str,
    rule: str,
    message: str,
) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(rf"(?<![\w.]){re.escape(before)}(?![\w.])", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(Fix(rule, line_number(source, match.start()), message, before, after))
    return apply_edits(source, edits), fixes


def find_matching_open(
    source: str, close_index: int, open_char: str = "(", close_char: str = ")"
) -> int | None:
    depth = 0
    for index in range(close_index, -1, -1):
        char = source[index]
        if char == close_char:
            depth += 1
        elif char == open_char:
            depth -= 1
            if depth == 0:
                return index
    return None
