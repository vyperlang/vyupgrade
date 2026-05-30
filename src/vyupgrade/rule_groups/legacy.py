from __future__ import annotations

import re

from ..analysis import SourceFacts, parse_source_facts
from ..models import Config, Diagnostic, Fix
from .legacy_call_helpers import _iter_calls, _replace_identifier_call
from ..rule_helpers import (
    function_body_span as _function_body_span,
    function_start_at_line as _function_start_at_line,
    strip_arg_comments as _strip_arg_comments,
    is_attribute_name as _is_attribute_name,
    is_keyword_argument_name as _is_keyword_argument_name,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    line_offsets as _line_offsets,
    pre_021_context as _pre_021_context,
    remove_constructor_decorators as _remove_constructor_decorators,
)
from ..rule_registry import (
    Rule,
    RuleContext,
    crossing,
    target_floor,
    target_update,
)
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    find_matching,
    line_number,
    split_top_level_args,
    span_is_code,
)
from ..versions import MigrationContext, VyperVersion


def _pragma(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    mask = code_mask(source)
    pattern = re.compile(
        r"^([ \t]*)#[ \t]*(?:@version|pragma[ \t]+version)[ \t]+(.+?)[ \t]*$", re.MULTILINE
    )
    matched = False

    def repl(match: re.Match[str]) -> str:
        nonlocal matched
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        matched = True
        before = match.group(0)
        after = f"{match.group(1)}#pragma version {config.target_version}"
        if before != after:
            fixes.append(
                Fix(
                    "VY001",
                    line_number(source, match.start()),
                    "modernized version pragma",
                    before,
                    after,
                )
            )
        return after

    rewritten = pattern.sub(repl, source)
    if matched:
        return rewritten, fixes, []
    pragma = f"#pragma version {config.target_version}\n"
    fixes.append(Fix("VY001", 1, "added version pragma", "", pragma.rstrip()))
    return pragma + rewritten, fixes, []


def _legacy_decorators(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    replacements = {
        "public": "external",
        "private": "internal",
        "constant": "view",
    }
    mask = code_mask(source)
    pattern = re.compile(r"^([ \t]*)@(public|private|constant)([ \t]*(?:#.*)?$)", re.MULTILINE)

    def repl(match: re.Match[str]) -> str:
        if not _line_match_starts_outside_string(source, mask, match.start()):
            return match.group(0)
        before = match.group(0)
        after = f"{match.group(1)}@{replacements[match.group(2)]}{match.group(3)}"
        fixes.append(
            Fix(
                "VY201",
                line_number(source, match.start()),
                "renamed legacy decorator",
                before,
                after,
            )
        )
        return after

    return pattern.sub(repl, source), fixes, []


def _legacy_type_units(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    type_re = re.compile(
        r"\b(u?int(?:8|16|32|64|128|256)?|decimal)\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)"
    )
    for match in type_re.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        before = match.group(0)
        after = match.group(1)
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY202",
                line_number(source, match.start()),
                "removed legacy type unit",
                before,
                after,
            )
        )
    for edit in _legacy_timestamp_type_edits(source, mask):
        edits.append(edit)
        fixes.append(
            Fix(
                "VY202",
                line_number(source, edit.start),
                "replaced legacy timestamp type",
                "timestamp",
                "uint256",
            )
        )
    return apply_edits(source, edits), fixes, []


def _legacy_timestamp_type_edits(source: str, mask: list[bool]) -> list[TextEdit]:
    edits: list[TextEdit] = []
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        code = line.split("#", 1)[0]
        if "timestamp" not in code:
            offset += len(raw_line)
            continue
        spans: list[tuple[int, int]] = []
        if re.match(r"\s*def\b", code):
            spans.append((0, len(code)))
        else:
            colon = code.find(":")
            if colon != -1:
                assignment = code.find("=", colon + 1)
                end = assignment if assignment != -1 else len(code)
                spans.append((colon + 1, end))
        for start, end in spans:
            for match in re.finditer(r"\btimestamp\b", code[start:end]):
                absolute_start = offset + start + match.start()
                absolute_end = offset + start + match.end()
                if absolute_start > 0 and source[absolute_start - 1] == ".":
                    continue
                if span_is_code(mask, absolute_start, absolute_end):
                    edits.append(TextEdit(absolute_start, absolute_end, "uint256"))
        offset += len(raw_line)
    return edits


def _legacy_events(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    current = source
    if rule_context.is_enabled("VY203"):
        current, event_fixes = _rewrite_legacy_event_declarations(current)
        fixes.extend(event_fixes)
    if rule_context.is_enabled("VY204"):
        pattern = re.compile(r"\blog\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        edits: list[TextEdit] = []
        mask = code_mask(current)
        for match in pattern.finditer(current):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            replacement = f"log {match.group(1)}("
            edits.append(TextEdit(match.start(), match.end(), replacement))
            fixes.append(
                Fix(
                    "VY204",
                    line_number(current, match.start()),
                    "changed legacy log call to statement",
                    match.group(0),
                    replacement,
                )
            )
        current = apply_edits(current, edits)
    return current, fixes, []


def _event_kwargs(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    event_fields = _collect_event_fields(source)
    if not event_fields:
        return source, [], []

    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\blog\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        event_name = match.group(1)
        fields = event_fields.get(event_name)
        if fields is None:
            continue
        open_index = source.find("(", match.start(), match.end())
        close = find_matching(source, open_index)
        if close is None:
            continue
        raw_args = source[open_index + 1 : close]
        args = split_top_level_args(_strip_arg_comments(raw_args))
        if args is None or len(args) != len(fields) or any("=" in arg for arg in args):
            continue
        kwargs = [f"{field}={arg}" for field, arg in zip(fields, args, strict=True)]
        if "\n" in raw_args:
            indent = source[source.rfind("\n", 0, match.start()) + 1 : match.start()]
            child_indent = indent + "    "
            joined = ",\n".join(f"{child_indent}{kwarg}" for kwarg in kwargs)
            replacement = f"log {event_name}(\n{joined}\n{indent})"
        else:
            replacement = f"log {event_name}({', '.join(kwargs)})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY112",
                line_number(source, match.start()),
                "changed positional event log to keyword arguments",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _legacy_dynamic_types(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\b(bytes|string)(\s*\[)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        after = "Bytes" if match.group(1) == "bytes" else "String"
        edits.append(TextEdit(match.start(1), match.end(1), after))
        fixes.append(
            Fix(
                "VY207",
                line_number(source, match.start()),
                f"capitalized legacy {match.group(1)} type",
                match.group(1),
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _early_beta_syntax(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    context = rule_context.migration
    rules = {"VY216", "VY217", "VY218", "VY219", "VY221"}
    if not rule_context.any_enabled(rules) or not _pre_021_context(context):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    if rule_context.is_enabled("VY216"):
        current, new_fixes = _rewrite_early_beta_types(current)
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY217"):
        current, new_fixes = _replace_identifier_call(current, "sha3", "keccak256", "VY217")
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY218"):
        current, new_fixes = _rewrite_string_convert_types(current)
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY219"):
        current, new_fixes = _rewrite_early_beta_clear(current)
        fixes.extend(new_fixes)
    if rule_context.is_enabled("VY221"):
        current, new_fixes = _rewrite_early_beta_call_syntax(current)
        fixes.extend(new_fixes)
    return current, fixes, []


def _rewrite_early_beta_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bbytes\s*<=\s*([A-Za-z_][A-Za-z0-9_]*|\d+)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = f"bytes[{match.group(1)}]"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY216",
                line_number(source, match.start()),
                "changed early beta bytes bound syntax",
                match.group(0),
                replacement,
            )
        )
    type_replacements = {
        "num128": "int128",
        "num256": "uint256",
        "signed256": "int256",
        "num": "int128",
    }
    pattern = re.compile(r"(?P<prefix>(?::|->|\[)\s*)(?P<type>num128|num256|signed256|num)\b")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = type_replacements[match.group("type")]
        edits.append(TextEdit(match.start("type"), match.end("type"), replacement))
        fixes.append(
            Fix(
                "VY216",
                line_number(source, match.start()),
                "renamed early beta numeric type",
                match.group("type"),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_string_convert_types(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, "convert"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 2:
            continue
        target = args[1].strip()
        target_match = re.fullmatch(r"""(["'])([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\1""", target)
        if target_match is None:
            continue
        replacement = f"convert({args[0].strip()}, {target_match.group(2)})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY218",
                line_number(source, match.start()),
                "changed convert string type argument",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_early_beta_clear(source: str) -> tuple[str, list[Fix]]:
    current, fixes = _replace_identifier_call(source, "reset", "clear", "VY219")
    mask = code_mask(current)
    edits: list[TextEdit] = []
    for match in re.finditer(
        r"^(?P<indent>[ \t]*)del[ \t]+(?P<expr>[^#\n]+?)(?P<trailing>[ \t]*(?:#.*)?)(?=\n|$)",
        current,
        re.MULTILINE,
    ):
        if not _line_match_starts_outside_string(current, mask, match.start()):
            continue
        expr = match.group("expr").strip()
        replacement = f"{match.group('indent')}clear({expr}){match.group('trailing')}"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY219",
                line_number(current, match.start()),
                "changed early beta delete statement",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(current, edits), fixes


def _rewrite_early_beta_call_syntax(source: str) -> tuple[str, list[Fix]]:
    current, fixes = _rewrite_as_wei_value_units(source)
    current, slice_fixes = _rewrite_slice_keyword_args(current)
    fixes.extend(slice_fixes)
    return current, fixes


def _rewrite_as_wei_value_units(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, "as_wei_value"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 2:
            continue
        unit = args[1].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", unit) is None:
            continue
        replacement = f'as_wei_value({args[0].strip()}, "{unit}")'
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY221",
                line_number(source, match.start()),
                "quoted as_wei_value unit",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _rewrite_slice_keyword_args(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match, _open_index, close, raw_args in _iter_calls(source, "slice"):
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 3:
            continue
        value_arg: str | None = None
        start_arg: str | None = None
        length_arg: str | None = None
        for arg in args:
            name, sep, raw_value = arg.partition("=")
            if not sep:
                if value_arg is not None:
                    value_arg = None
                    break
                value_arg = arg.strip()
                continue
            if name.strip() == "start":
                start_arg = raw_value.strip()
            elif name.strip() == "len":
                length_arg = raw_value.strip()
            else:
                value_arg = None
                break
        if value_arg is None or start_arg is None or length_arg is None:
            continue
        replacement = f"slice({value_arg}, {start_arg}, {length_arg})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(
            Fix(
                "VY221",
                line_number(source, match.start()),
                "changed slice required keyword arguments",
                source[match.start() : close + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _reserved_parameter_names(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    if context.source_floor is not None and context.source_floor > VyperVersion("0.2.1"):
        return source, [], []
    facts = parse_source_facts(source)
    line_offsets = _line_offsets(source)
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    pattern = re.compile(r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<args>[^)]*)\)", re.MULTILINE)
    for match in pattern.finditer(source):
        args = split_top_level_args(match.group("args"))
        if args is None:
            continue
        names = {arg.split(":", 1)[0].split("=", 1)[0].strip() for arg in args}
        if "value" not in names:
            continue
        replacement = "_value" if "_value" not in names else "value_"
        args_start = match.start("args")
        args_text = match.group("args")
        for name_match in re.finditer(r"\bvalue\b(?=\s*(?::|=|,|$))", args_text):
            start = args_start + name_match.start()
            edits.append(TextEdit(start, start + len("value"), replacement))
        function_line = line_number(source, match.start())
        body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
        for name_match in re.finditer(r"\bvalue\b", source[body_start:body_end]):
            start = body_start + name_match.start()
            end = body_start + name_match.end()
            if not span_is_code(mask, start, end):
                continue
            if _is_attribute_name(source, start) or _is_keyword_argument_name(source, start, end):
                continue
            edits.append(TextEdit(start, end, replacement))
        fixes.append(
            Fix(
                "VY212",
                function_line,
                "renamed reserved function parameter value",
                "value",
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _natspec_strictness(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    facts = parse_source_facts(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    in_docstring = False
    quote = ""
    doc_function_start: int | None = None
    offset = 0
    for line_no, raw_line in enumerate(source.splitlines(keepends=True), start=1):
        line = raw_line.rstrip("\n")
        stripped = line.lstrip()
        if not in_docstring:
            if stripped.startswith(('"""', "'''")):
                quote = stripped[:3]
                in_docstring = True
                doc_function_start = _function_start_at_line(facts, line_no)
                if stripped.count(quote) >= 2:
                    in_docstring = False
                    doc_function_start = None
            offset += len(raw_line)
            continue

        if stripped.startswith(quote):
            in_docstring = False
            doc_function_start = None
            offset += len(raw_line)
            continue

        params = _function_param_names_at_start(facts, doc_function_start)
        replacement = _natspec_line_replacement(line, params)
        if replacement is None:
            edits.append(TextEdit(offset, offset + len(raw_line), ""))
            fixes.append(
                Fix(
                    "VY058",
                    line_no,
                    "removed NatSpec line for unknown function parameter",
                    line,
                    "",
                )
            )
        elif replacement != line:
            edits.append(TextEdit(offset, offset + len(line), replacement))
            fixes.append(Fix("VY058", line_no, "updated NatSpec tag syntax", line, replacement))
        offset += len(raw_line)
    return apply_edits(source, edits), fixes, []


def _function_param_names_at_start(facts: SourceFacts, start: int | None) -> set[str] | None:
    if start is None:
        return None
    name = facts.function_names.get(start)
    if name is None:
        return None
    return set(facts.function_params.get(name, {}))


def _natspec_line_replacement(line: str, params: set[str] | None) -> str | None:
    param_match = re.match(r"^(\s*)@param\s+([A-Za-z_][A-Za-z0-9_]*)(:)?(\s+.*)?$", line)
    if param_match is not None and params is not None:
        name = param_match.group(2)
        if name not in params or not (param_match.group(4) or "").strip():
            return None
        if param_match.group(3):
            return f"{param_match.group(1)}@param {name}{param_match.group(4) or ''}"
        return line
    fork_match = re.match(r"^(\s*)@fork(\s+.*)?$", line)
    if fork_match is not None:
        return f"{fork_match.group(1)}@custom:fork{fork_match.group(2) or ''}"
    return line


def _legacy_constructor_locks(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    current, fixes, insertions = _remove_constructor_decorators(
        source,
        {"@nonreentrant"},
        "VY210",
        "removed nonreentrant constructor decorator",
    )
    fixes.extend(insertions)
    return current, fixes, []


def _rewrite_legacy_event_declarations(source: str) -> tuple[str, list[Fix]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*event\s*\(\s*\{", re.MULTILINE)
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_brace = source.rfind("{", match.start(), match.end())
        close_brace = find_matching(source, open_brace, "{", "}")
        if close_brace is None:
            continue
        close_paren = source.find(")", close_brace)
        if close_paren == -1:
            continue
        fields = split_top_level_args(source[open_brace + 1 : close_brace])
        if fields is None:
            continue
        lines = [f"{match.group(1)}event {match.group(2)}:"]
        child_indent = f"{match.group(1)}    "
        ok = True
        for field in fields:
            if ":" not in field:
                ok = False
                break
            name, typ = field.split(":", 1)
            lines.append(f"{child_indent}{name.strip()}: {typ.strip()}")
        if not ok:
            continue
        replacement = "\n".join(lines)
        edits.append(TextEdit(match.start(), close_paren + 1, replacement))
        fixes.append(
            Fix(
                "VY203",
                line_number(source, match.start()),
                "changed legacy event declaration",
                source[match.start() : close_paren + 1],
                replacement,
            )
        )
    return apply_edits(source, edits), fixes


def _collect_event_fields(source: str) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {}
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(
            r"^(?P<indent>\s*)event\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:#.*)?$", line
        )
        if match is None:
            index += 1
            continue
        indent = len(match.group("indent"))
        fields: list[str] = []
        index += 1
        while index < len(lines):
            child = lines[index]
            if not child.strip() or child.lstrip().startswith("#"):
                index += 1
                continue
            child_indent = len(child) - len(child.lstrip())
            if child_indent <= indent:
                break
            field = re.match(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:", child)
            if field is not None:
                fields.append(field.group("name"))
            index += 1
        if fields:
            events[match.group("name")] = fields
    return events




EARLY_RULES = (
    Rule("pragma", runner=_pragma, changes=(target_update("VY001", (0, 3, 10)),)),
    Rule("legacy_decorators", runner=_legacy_decorators, changes=(target_floor("VY201", (0, 2, 1)),)),
    Rule("legacy_type_units", runner=_legacy_type_units, changes=(target_floor("VY202", (0, 2, 1)),)),
    Rule(
        "legacy_events",
        context_runner=_legacy_events,
        changes=(
            target_floor("VY203", (0, 2, 1)),
            target_floor("VY204", (0, 2, 1)),
        ),
    ),
    Rule("event_kwargs", runner=_event_kwargs, changes=(crossing("VY112", (0, 4, 1)),)),
)

POST_INTERFACE_RULES = (
    Rule(
        "early_beta_syntax",
        context_runner=_early_beta_syntax,
        changes=(
            target_floor("VY216", (0, 2, 1)),
            target_floor("VY217", (0, 2, 1)),
            target_floor("VY218", (0, 2, 1)),
            target_floor("VY219", (0, 2, 1)),
            target_floor("VY221", (0, 2, 1)),
        ),
    ),
    Rule("legacy_dynamic_types", runner=_legacy_dynamic_types, changes=(target_floor("VY207", (0, 2, 1)),)),
    Rule("reserved_parameter_names", runner=_reserved_parameter_names, changes=(target_floor("VY212", (0, 2, 1)),),),
)

POST_DIAGNOSTIC_RULES = (
    Rule("natspec_strictness", runner=_natspec_strictness, changes=(crossing("VY058", (0, 4, 0)),)),
)

POST_COMPARISON_RULES = (
    Rule("legacy_constructor_locks", runner=_legacy_constructor_locks, changes=(crossing("VY210", (0, 2, 16)),)),
)
