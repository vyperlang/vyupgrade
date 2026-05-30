from __future__ import annotations

import re
from collections import Counter

from ..analysis import SourceFacts, parse_source_facts
from ..models import Config, Diagnostic, Fix
from ..rule_groups.numeric import _cast_integer_arg_to_expected
from ..rule_helpers import (
    has_line_comment as _has_line_comment,
    innermost_non_overlapping as _innermost_non_overlapping,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    remove_constructor_decorators as _remove_constructor_decorators,
    strip_arg_comments as _strip_arg_comments,
)
from ..rule_registry import any_enabled as _any_enabled, is_enabled as _enabled
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    find_matching,
    line_number,
    replace_identifier,
    split_top_level_args,
    span_is_code,
)
from ..versions import MigrationContext


def _constructor_deploy(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY002", config, context):
        return source, [], []
    current, fixes, insertions = _remove_constructor_decorators(
        source,
        {"@external", "@internal", "@public", "@private"},
        "VY002",
        "removed invalid constructor decorator",
        add_deploy=True,
    )
    fixes.extend(insertions)
    return current, fixes, []


def _abi_builtins(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    current = source
    for before, after, rule in [
        ("_abi_encode", "abi_encode", "VY010"),
        ("_abi_decode", "abi_decode", "VY011"),
    ]:
        if not _enabled(rule, config, context):
            continue
        next_source, edits = replace_identifier(current, before, after)
        for edit in edits:
            fixes.append(
                Fix(
                    rule,
                    line_number(current, edit.start),
                    f"renamed {before} to {after}",
                    before,
                    after,
                )
            )
        current = next_source
    return current, fixes, []


def _enum_to_flag(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY030"}, config, context):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if re.search(r"\benum\s+\w+:", source) is None:
        return source, fixes, diagnostics

    mask = code_mask(source)
    pattern = re.compile(r"^([ \t]*)enum[ \t]+([A-Za-z_][A-Za-z0-9_]*):", re.MULTILINE)
    for match in pattern.finditer(source):
        if not _line_match_starts_outside_string(source, mask, match.start()):
            continue
        diagnostics.append(
            Diagnostic(
                "VY030",
                line_number(source, match.start()),
                f"enum {match.group(2)} should be reviewed for flag compatibility",
            )
        )
    if not config.aggressive:
        return source, fixes, diagnostics

    def repl(match: re.Match[str]) -> str:
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        before = match.group(0)
        after = f"{match.group(1)}flag {match.group(2)}:"
        fixes.append(
            Fix("VY030", line_number(source, match.start()), "changed enum to flag", before, after)
        )
        return after

    return pattern.sub(repl, source), fixes, diagnostics


def _remove_internal_nonreentrant(source: str) -> tuple[str, list[Fix]]:
    lines = source.splitlines(keepends=True)
    fixes: list[Fix] = []
    out = list(lines)
    offset = 0
    for index, line in enumerate(lines):
        if not re.match(r"\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", line):
            continue
        start = index
        while start > 0 and re.match(
            r"\s*@[A-Za-z_][A-Za-z0-9_]*(?:\(.*\))?\s*(?:#.*)?$", lines[start - 1]
        ):
            start -= 1
        decorators = [
            decor.strip().split("(", 1)[0].split("#", 1)[0] for decor in lines[start:index]
        ]
        if "@internal" not in decorators or "@nonreentrant" not in decorators:
            continue
        for original_index in range(index - 1, start - 1, -1):
            if re.match(r"\s*@nonreentrant\b", lines[original_index]):
                before = out[original_index + offset].rstrip("\n")
                del out[original_index + offset]
                offset -= 1
                fixes.append(
                    Fix(
                        "VY090",
                        original_index + 1,
                        "removed internal nonreentrant decorator to avoid global-lock self-call violation",
                        before,
                        "",
                    )
                )
                break
    return "".join(out), fixes


def _struct_kwargs(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY060", config, context):
        return source, [], []
    current = source
    all_fixes: list[Fix] = []
    while True:
        facts = parse_source_facts(current)
        fixes: list[Fix] = []
        edits: list[TextEdit] = []
        mask = code_mask(current)
        for struct_name in sorted(facts.structs):
            field_order = list(facts.struct_fields.get(struct_name, {}))
            if not field_order:
                continue
            for match in re.finditer(rf"\b{re.escape(struct_name)}\s*\(", current):
                if not span_is_code(mask, match.start(), match.end()):
                    continue
                paren = current.find("(", match.start())
                close = find_matching(current, paren)
                if close is None:
                    continue
                raw_inner = current[paren + 1 : close]
                vars_for_line = facts.vars_at_line(line_number(current, match.start()))
                replacement_inner = _ordered_struct_args(
                    raw_inner,
                    facts.struct_fields.get(struct_name, {}),
                    vars_for_line,
                    facts,
                )
                if replacement_inner is None or replacement_inner == raw_inner:
                    continue
                replacement = f"{struct_name}({replacement_inner})"
                edits.append(TextEdit(match.start(), close + 1, replacement))
                fixes.append(
                    Fix(
                        "VY060",
                        line_number(current, match.start()),
                        "ordered struct constructor keyword arguments",
                        current[match.start() : close + 1],
                        replacement,
                    )
                )
        if not edits:
            return current, all_fixes, []
        selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
        all_fixes.extend(selected_fixes)
        current = apply_edits(current, selected_edits)


def _ordered_struct_args(
    raw_inner: str,
    struct_fields: dict[str, str],
    vars_for_line: dict[str, str],
    facts: SourceFacts,
) -> str | None:
    field_order = list(struct_fields)
    if "\n" in raw_inner and _has_line_comment(raw_inner):
        return None
    stripped = raw_inner.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        fields = split_top_level_args(_strip_arg_comments(stripped[1:-1]))
        if fields is None:
            return None
        pairs: list[tuple[str, str]] = []
        for field in fields:
            pair = _split_struct_pair(field, ":")
            if pair is None:
                return None
            pairs.append(pair)
        return _ordered_kwarg_string(pairs, field_order, struct_fields, vars_for_line, facts)

    args = split_top_level_args(_strip_arg_comments(raw_inner))
    if args is None:
        return None
    pairs = []
    for arg in args:
        pair = _split_struct_pair(arg, "=")
        if pair is None:
            return None
        pairs.append(pair)
    ordered = _ordered_kwarg_string(pairs, field_order, struct_fields, vars_for_line, facts)
    return ordered if ordered != ", ".join(arg.strip() for arg in args) else None


def _split_struct_pair(raw: str, sep: str) -> tuple[str, str] | None:
    if sep == "=":
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)\Z", raw, re.DOTALL)
    else:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)\Z", raw, re.DOTALL)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def _ordered_kwarg_string(
    pairs: list[tuple[str, str]],
    field_order: list[str],
    struct_fields: dict[str, str],
    vars_for_line: dict[str, str],
    facts: SourceFacts,
) -> str:
    by_name = dict(pairs)
    ordered_names = [name for name in field_order if name in by_name]
    ordered_names.extend(name for name, _value in pairs if name not in field_order)
    return ", ".join(
        f"{name}={_cast_integer_arg_to_expected(by_name[name], struct_fields.get(name), vars_for_line, facts)}"
        for name in ordered_names
    )


def _create_from_blueprint(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY080", config, context):
        return source, [], []
    diagnostics: list[Diagnostic] = []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bcreate_from_blueprint\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = source[match.end() : close]
        if "code_offset" in args:
            continue
        diagnostics.append(
            Diagnostic(
                "VY080",
                line_number(source, match.start()),
                "create_from_blueprint default code_offset changed from 0 to 3",
            )
        )
        edits.append(TextEdit(close, close, ", code_offset=0"))
        fixes.append(
            Fix(
                "VY080",
                line_number(source, match.start()),
                "added code_offset=0 to preserve 0.3.x behavior",
                "",
                "code_offset=0",
            )
        )
    return apply_edits(source, edits), fixes, diagnostics


def _nonreentrant(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY090", "VYD002"}, config, context):
        return source, [], []
    pattern = re.compile(r"@nonreentrant\(\s*([\"'])(.+?)\1\s*\)")
    locks = [match.group(2) for match in pattern.finditer(source)]
    diagnostics: list[Diagnostic] = []
    fixes: list[Fix] = []
    if not locks:
        return source, fixes, diagnostics
    counts = Counter(locks)
    if len(counts) > 1:
        first = pattern.search(source)
        diagnostics.append(
            Diagnostic(
                "VYD002",
                line_number(source, first.start() if first else 0),
                "multiple named reentrancy locks found; 0.4.x uses a global lock",
            )
        )
    if not _enabled("VY090", config, context):
        return source, fixes, diagnostics
    diagnostics.extend(
        Diagnostic(
            "VY090",
            line_number(source, match.start()),
            "single named nonreentrant lock rewritten; review callback assumptions",
        )
        for match in pattern.finditer(source)
    )

    def repl(match: re.Match[str]) -> str:
        fixes.append(
            Fix(
                "VY090",
                line_number(source, match.start()),
                "removed named nonreentrant lock",
                match.group(0),
                "@nonreentrant",
            )
        )
        return "@nonreentrant"

    current = pattern.sub(repl, source)
    current, internal_fixes = _remove_internal_nonreentrant(current)
    fixes.extend(internal_fixes)
    return current, fixes, diagnostics


