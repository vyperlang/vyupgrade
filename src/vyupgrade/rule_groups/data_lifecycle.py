from __future__ import annotations

import re
from collections import Counter

from ..analysis import SourceFacts, parse_source_facts
from ..models import Diagnostic, Fix
from .numeric_casts import cast_integer_arg_to_expected
from ..rule_helpers import (
    has_line_comment as _has_line_comment,
    innermost_non_overlapping as _innermost_non_overlapping,
    line_match_starts_outside_string as _line_match_starts_outside_string,
    remove_constructor_decorators as _remove_constructor_decorators,
    strip_arg_comments as _strip_arg_comments,
)
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
    replace_identifier,
    split_top_level_args,
    span_is_code,
)


def _constructor_deploy(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    current, fixes, insertions = _remove_constructor_decorators(
        source,
        {"@external", "@internal", "@public", "@private"},
        "VY002",
        "removed invalid constructor decorator",
        add_deploy=True,
    )
    fixes.extend(insertions)
    edits: list[TextEdit] = []
    for match in re.finditer(
        r"^(?P<prefix>[ \t]*def[ \t]+__init__\s*\([^)\n]*\))\s*->\s*(?P<return_type>[^:\n#]+)(?P<suffix>\s*:.*)$",
        current,
        re.MULTILINE,
    ):
        if not _line_match_starts_outside_string(current, code_mask(current), match.start()):
            continue
        before = match.group(0)
        after = f"{match.group('prefix')}{match.group('suffix')}"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY002",
                line_number(current, match.start()),
                "removed constructor return type",
                before,
                after,
            )
        )
    if edits:
        current = apply_edits(current, edits)
    return_edits: list[TextEdit] = []
    for start, end, before, after in _constructor_value_returns(current):
        return_edits.append(TextEdit(start, end, after))
        fixes.append(
            Fix(
                "VY002",
                line_number(current, start),
                "removed constructor return value",
                before,
                after,
            )
        )
    if return_edits:
        current = apply_edits(current, return_edits)
    return current, fixes, []


def _constructor_value_returns(source: str) -> list[tuple[int, int, str, str]]:
    edits: list[tuple[int, int, str, str]] = []
    offset = 0
    in_constructor = False
    constructor_indent = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        if in_constructor and stripped and not stripped.startswith("#") and indent <= constructor_indent:
            in_constructor = False
        if re.match(r"[ \t]*def[ \t]+__init__\s*\(", line):
            in_constructor = True
            constructor_indent = indent
        elif in_constructor:
            match = re.match(r"(?P<indent>[ \t]*)return[ \t]+(?P<value>[^#\n]+)(?P<comment>[ \t]*(?:#.*)?)$", line)
            if match is not None:
                before = line
                after = f"{match.group('indent')}return{match.group('comment')}"
                edits.append((offset, offset + len(line), before, after))
        offset += len(raw_line)
    return edits


def _abi_builtins(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    source = rule_context.source
    current = source
    for before, after, rule in [
        ("_abi_encode", "abi_encode", "VY010"),
        ("_abi_decode", "abi_decode", "VY011"),
    ]:
        if not rule_context.is_enabled(rule):
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


def _enum_to_flag(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    config = rule_context.config
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if re.search(r"\benum\s+\w+:", source) is None:
        return source, fixes, diagnostics

    mask = rule_context.code_mask
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


def _max_value_storage_arrays(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    mask = rule_context.code_mask
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    pattern = re.compile(
        r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<type>[^#\n=]+)(?P<comment>[ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start("name"), match.end("type")):
            continue
        replacement_type = _max_value_array_type_to_hashmap(match.group("type").strip())
        if replacement_type is None:
            continue
        before = match.group(0)
        after = f"{match.group('name')}: {replacement_type}{match.group('comment')}"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY091",
                line_number(source, match.start()),
                "lowered max_value-bound storage array to hashmap",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _max_value_array_type_to_hashmap(type_expr: str) -> str | None:
    wrapper = re.fullmatch(r"(?P<name>public|immutable|constant)\s*\((?P<inner>.*)\)", type_expr)
    if wrapper is not None:
        inner_replacement = _max_value_array_type_to_hashmap(wrapper.group("inner").strip())
        if inner_replacement is None:
            return None
        return f"{wrapper.group('name')}({inner_replacement})"
    max_uint256 = "115792089237316195423570985008687907853269984665640564039457584007913129639935"
    array = re.fullmatch(
        rf"(?P<element>.+)\[\s*(?:max_value\s*\(\s*uint256\s*\)|{max_uint256})\s*\]",
        type_expr,
    )
    if array is None:
        return None
    element = array.group("element").strip()
    if not element:
        return None
    return f"HashMap[uint256, {element}]"


def _reserved_flag_storage(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    mask = rule_context.code_mask
    declaration = re.search(
        r"^flag\s*:\s*public\s*\(\s*(?P<type>[^)\n]+)\s*\)(?P<comment>[ \t]*(?:#.*)?)$",
        source,
        re.MULTILINE,
    )
    if declaration is None or not span_is_code(mask, declaration.start(), declaration.end()):
        return source, [], []
    if re.search(r"^[ \t]*def\s+flag\s*\(", source, re.MULTILINE):
        return source, [], []
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    value_type = declaration.group("type").strip()
    replacement = f"_flag: {value_type}{declaration.group('comment')}"
    edits.append(TextEdit(declaration.start(), declaration.end(), replacement))
    fixes.append(
        Fix(
            "VY093",
            line_number(source, declaration.start()),
            "renamed reserved flag storage variable",
            declaration.group(0),
            replacement,
        )
    )
    for match in re.finditer(r"\bself\.flag\b", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(), match.end(), "self._flag"))
        fixes.append(
            Fix(
                "VY093",
                line_number(source, match.start()),
                "renamed reserved flag storage reference",
                "self.flag",
                "self._flag",
            )
        )
    insert_at = _first_top_level_function_offset(source)
    getter = f"@external\n@view\ndef flag() -> {value_type}:\n    return self._flag\n\n"
    edits.append(TextEdit(insert_at, insert_at, getter))
    fixes.append(Fix("VY093", line_number(source, insert_at), "added flag getter", "", getter.rstrip()))
    return apply_edits(source, edits), fixes, []


def _unbounded_dynarray_limits(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    mask = rule_context.code_mask
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for match in re.finditer(r"\bDynArray\s*\[[^\n\]]+,\s*max_value\s*\(\s*int128\s*\)\s*\]", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        bound = re.search(r"max_value\s*\(\s*int128\s*\)", match.group(0))
        if bound is None:
            continue
        start = match.start() + bound.start()
        end = match.start() + bound.end()
        edits.append(TextEdit(start, end, "max_value(uint32)"))
        fixes.append(
            Fix(
                "VY094",
                line_number(source, start),
                "bounded legacy unbounded DynArray length",
                source[start:end],
                "max_value(uint32)",
            )
        )
    if edits:
        for match in re.finditer(r"\brange\s*\(\s*max_value\s*\(\s*int128\s*\)\s*\)", source):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            bound = re.search(r"max_value\s*\(\s*int128\s*\)", match.group(0))
            if bound is None:
                continue
            start = match.start() + bound.start()
            end = match.start() + bound.end()
            edits.append(TextEdit(start, end, "max_value(uint32)"))
            fixes.append(
                Fix(
                    "VY094",
                    line_number(source, start),
                    "bounded legacy unbounded range",
                    source[start:end],
                    "max_value(uint32)",
                )
            )
        for match in re.finditer(
            r"\bfor\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*int128\s+in\s+range\s*\(\s*max_value\s*\(\s*int128\s*\)\s*\)",
            source,
        ):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            type_start = source.find("int128", match.start(), match.end())
            edits.append(TextEdit(type_start, type_start + len("int128"), "uint32"))
            fixes.append(
                Fix(
                    "VY094",
                    line_number(source, match.start()),
                    "changed legacy unbounded range loop type",
                    "int128",
                    "uint32",
                )
            )
    return apply_edits(source, edits), fixes, []


def _first_top_level_function_offset(source: str) -> int:
    offset = 0
    pending_decorator = 0
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        if line[:1] not in {" ", "\t"} and stripped.startswith("@"):
            if pending_decorator == 0:
                pending_decorator = offset
        elif line[:1] not in {" ", "\t"} and re.match(r"def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped):
            return pending_decorator or offset
        elif stripped and not stripped.startswith("#"):
            pending_decorator = 0
        offset += len(line)
    return len(source)


def _unreachable_code(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    lines = source.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    remove: set[int] = set()
    for function_line, end_line in sorted(rule_context.facts.function_ends.items()):
        def_index = function_line - 1
        if def_index < 0 or def_index >= len(lines):
            continue
        function_indent = _line_indent(lines[def_index])
        body_start = _function_body_start_line(lines, def_index, end_line)
        body_indent = _first_child_indent(lines, body_start, end_line, function_indent)
        if body_indent is None:
            continue
        _scan_terminal_block(lines, body_start, end_line, body_indent, remove)
    if not remove:
        return source, [], []
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for start, end in _contiguous_line_ranges(remove):
        edit_start = offsets[start]
        edit_end = offsets[end]
        before = source[edit_start:edit_end].rstrip("\n")
        edits.append(TextEdit(edit_start, edit_end, ""))
        fixes.append(
            Fix(
                "VY092",
                start + 1,
                "removed unreachable code",
                before,
                "",
            )
        )
    return apply_edits(source, edits), fixes, []


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    offsets.append(cursor)
    return offsets


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _function_body_start_line(lines: list[str], def_index: int, end: int) -> int:
    balance = 0
    for index in range(def_index, min(end, len(lines))):
        statement = lines[index].split("#", 1)[0]
        for char in statement:
            if char in "([{":
                balance += 1
            elif char in ")]}" and balance > 0:
                balance -= 1
        if balance == 0 and statement.rstrip().endswith(":"):
            return index + 1
    return def_index + 1


def _significant_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _first_child_indent(
    lines: list[str], start: int, end: int, parent_indent: int
) -> int | None:
    for index in range(start, min(end, len(lines))):
        if not _significant_line(lines[index]):
            continue
        indent = _line_indent(lines[index])
        if indent > parent_indent:
            return indent
        if indent <= parent_indent:
            return None
    return None


def _scan_terminal_block(
    lines: list[str], start: int, end: int, indent: int, remove: set[int]
) -> bool:
    index = start
    while index < min(end, len(lines)):
        line = lines[index]
        if not _significant_line(line):
            index += 1
            continue
        current_indent = _line_indent(line)
        if current_indent < indent:
            break
        if current_indent > indent:
            index += 1
            continue
        stripped = line.strip()
        quote = _triple_quote_start(stripped)
        if quote is not None:
            index = _triple_quoted_string_end(lines, index, end, quote) + 1
            continue
        if _is_if_header(stripped):
            chain_end, terminates = _if_chain_terminates(lines, index, end, indent, remove)
            if terminates:
                _mark_unreachable_after(lines, chain_end, end, indent, remove)
                return True
            index = chain_end
            continue
        if _is_terminator(stripped):
            _mark_unreachable_after(
                lines, _continued_statement_end(lines, index, end), end, indent, remove
            )
            return True
        child_indent = _first_child_indent(lines, index + 1, end, indent)
        if child_indent is not None:
            block_end = _next_statement_index(lines, index + 1, end, indent)
            _scan_terminal_block(lines, index + 1, block_end, child_indent, remove)
        index = _next_statement_index(lines, index + 1, end, indent)
    return False


def _if_chain_terminates(
    lines: list[str], start: int, end: int, indent: int, remove: set[int]
) -> tuple[int, bool]:
    index = start
    has_else = False
    branch_results: list[bool] = []
    while index < min(end, len(lines)):
        stripped = lines[index].strip()
        if index == start:
            if not _is_if_header(stripped):
                break
        elif _is_elif_header(stripped):
            pass
        elif _is_else_header(stripped):
            has_else = True
        else:
            break
        branch_start = index + 1
        branch_end = _next_if_branch_or_block_end(lines, branch_start, end, indent)
        body_indent = _first_child_indent(lines, branch_start, branch_end, indent)
        branch_results.append(
            body_indent is not None
            and _scan_terminal_block(lines, branch_start, branch_end, body_indent, remove)
        )
        index = branch_end
        if index >= min(end, len(lines)):
            break
        next_line = lines[index]
        if not _significant_line(next_line) or _line_indent(next_line) != indent:
            break
        next_stripped = next_line.strip()
        if not (_is_elif_header(next_stripped) or _is_else_header(next_stripped)):
            break
    return index, has_else and all(branch_results)


def _next_if_branch_or_block_end(lines: list[str], start: int, end: int, indent: int) -> int:
    index = start
    while index < min(end, len(lines)):
        if not _significant_line(lines[index]):
            index += 1
            continue
        current_indent = _line_indent(lines[index])
        if current_indent == indent and (
            _is_elif_header(lines[index].strip()) or _is_else_header(lines[index].strip())
        ):
            return index
        if current_indent <= indent:
            return index
        index += 1
    return min(end, len(lines))


def _next_statement_index(lines: list[str], start: int, end: int, indent: int) -> int:
    index = start
    while index < min(end, len(lines)):
        if not _significant_line(lines[index]):
            index += 1
            continue
        current_indent = _line_indent(lines[index])
        if current_indent <= indent:
            return index
        index += 1
    return min(end, len(lines))


def _mark_unreachable_after(
    lines: list[str], start: int, end: int, indent: int, remove: set[int]
) -> None:
    first_unreachable: int | None = None
    for index in range(start, min(end, len(lines))):
        if not _significant_line(lines[index]):
            continue
        current_indent = _line_indent(lines[index])
        if current_indent < indent:
            return
        first_unreachable = index
        break
    if first_unreachable is None:
        return
    for index in range(start, min(end, len(lines))):
        if _significant_line(lines[index]) and _line_indent(lines[index]) < indent:
            break
        remove.add(index)


def _contiguous_line_ranges(lines: set[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for line in sorted(lines):
        if not ranges or line != ranges[-1][1]:
            ranges.append((line, line + 1))
        else:
            ranges[-1] = (ranges[-1][0], line + 1)
    return ranges


def _is_terminator(stripped: str) -> bool:
    statement = stripped.split("#", 1)[0].rstrip()
    if not _balanced_delimiters(statement):
        return False
    return statement in {"return", "raise", "break", "continue"} or statement.startswith(
        (
            "return ",
            "return(",
            "raise ",
            "break ",
            "continue ",
        )
    )


def _triple_quote_start(stripped: str) -> str | None:
    for quote in ('"""', "'''"):
        if stripped.startswith(quote):
            return quote
    return None


def _triple_quoted_string_end(lines: list[str], start: int, end: int, quote: str) -> int:
    limit = min(end, len(lines))
    if lines[start].strip().count(quote) >= 2:
        return start
    index = start + 1
    while index < limit:
        if quote in lines[index]:
            return index
        index += 1
    return limit - 1


def _continued_statement_end(lines: list[str], index: int, end: int) -> int:
    limit = min(end, len(lines))
    while index < limit and _line_continues(lines[index]):
        index += 1
    return min(index + 1, limit)


def _line_continues(line: str) -> bool:
    return line.split("#", 1)[0].rstrip().endswith("\\")


def _balanced_delimiters(text: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for char in text:
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values() and (not stack or stack.pop() != char):
            return False
    return not stack


def _is_if_header(stripped: str) -> bool:
    return stripped.startswith(("if ", "if("))


def _is_elif_header(stripped: str) -> bool:
    return stripped.startswith(("elif ", "elif("))


def _is_else_header(stripped: str) -> bool:
    return stripped.startswith("else:")


def _struct_kwargs(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    current = rule_context.source
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
    stripped = raw_inner.strip()
    if "\n" in raw_inner and _has_line_comment(raw_inner):
        if _has_inline_comment(raw_inner):
            return None
        sep = ":" if stripped.startswith("{") and stripped.endswith("}") else "="
        commented = _ordered_commented_struct_args(
            stripped,
            sep,
            field_order,
            struct_fields,
            vars_for_line,
            facts,
        )
        return commented
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


def _has_inline_comment(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if _has_line_comment(line):
            return True
    return False


def _ordered_commented_struct_args(
    stripped: str,
    sep: str,
    field_order: list[str],
    struct_fields: dict[str, str],
    vars_for_line: dict[str, str],
    facts: SourceFacts,
) -> str | None:
    body = stripped[1:-1] if sep == ":" else stripped
    records: list[tuple[str, str, list[str]]] = []
    pending_comments: list[str] = []
    indent = ""
    for line in body.splitlines():
        if not line.strip():
            continue
        stripped_line = line.strip()
        if stripped_line.startswith("#"):
            pending_comments.append(stripped_line)
            continue
        indent = indent or line[: len(line) - len(line.lstrip())]
        pair = _split_struct_pair(stripped_line.rstrip(","), sep)
        if pair is None:
            return None
        name, value = pair
        records.append((name, value, pending_comments))
        pending_comments = []
    if pending_comments:
        return None
    if not records:
        return None

    indent = indent or "    "
    close_indent = indent[:-4] if len(indent) >= 4 else ""
    by_name = {name: (value, comments) for name, value, comments in records}
    ordered_names = [name for name in field_order if name in by_name]
    ordered_names.extend(name for name, _value, _comments in records if name not in field_order)
    lines: list[str] = []
    for name in ordered_names:
        value, comments = by_name[name]
        lines.extend(f"{indent}{comment}" for comment in comments)
        casted = cast_integer_arg_to_expected(value, struct_fields.get(name), vars_for_line, facts)
        lines.append(f"{indent}{name}={casted},")
    return "\n" + "\n".join(lines) + f"\n{close_indent}"


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
        f"{name}={cast_integer_arg_to_expected(by_name[name], struct_fields.get(name), vars_for_line, facts)}"
        for name in ordered_names
    )


def _create_from_blueprint(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    diagnostics: list[Diagnostic] = []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
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
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
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
    if not rule_context.is_enabled("VY090"):
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
    if "@nonreentrant" not in current:
        current, gap_fixes = _insert_nonreentrant_storage_gaps(current, len(counts))
        fixes.extend(gap_fixes)
    return current, fixes, diagnostics


def _insert_nonreentrant_storage_gaps(source: str, count: int) -> tuple[str, list[Fix]]:
    if count <= 0:
        return source, []
    names = _nonreentrant_storage_gap_names(source, count)
    declarations = "".join(
        f"{name}: uint256  # preserves legacy nonreentrant lock storage slot\n" for name in names
    )
    insert_at = _storage_gap_insert_offset(source)
    if insert_at is None:
        return source, []
    before = "\n" if insert_at > 0 and not source[:insert_at].endswith("\n\n") else ""
    after = "" if declarations.endswith("\n") else "\n"
    replacement = f"{before}{declarations}{after}"
    line = line_number(source, insert_at)
    return source[:insert_at] + replacement + source[insert_at:], [
        Fix(
            "VY090",
            line,
            "reserved legacy nonreentrant lock storage slot",
            "",
            declarations.rstrip("\n"),
        )
    ]


def _nonreentrant_storage_gap_names(source: str, count: int) -> list[str]:
    used = code_identifiers(source)
    names: list[str] = []
    index = 1
    while len(names) < count:
        suffix = "" if index == 1 else f"_{index}"
        name = f"_vyupgrade_reentrancy_lock_slot{suffix}"
        if name not in used:
            names.append(name)
            used.add(name)
        index += 1
    return names


def _storage_gap_insert_offset(source: str) -> int | None:
    facts = parse_source_facts(source)
    storage_names = {
        name
        for name, type_name in facts.global_vars.items()
        if not type_name.startswith(("constant(", "immutable("))
    }
    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or line[:1].isspace():
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
        if match and match.group(1) in storage_names:
            return offsets[index]
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or line[:1].isspace():
            continue
        if stripped.startswith("@") or re.match(r"def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped):
            return offsets[index]
    return len(source)


CONSTRUCTOR_RULES = (
    Rule("constructor_deploy", runner=_constructor_deploy, changes=(crossing("VY002", (0, 4, 0)),)),
    Rule(
        "abi_builtins",
        runner=_abi_builtins,
        changes=(
            crossing("VY010", (0, 4, 0)),
            crossing("VY011", (0, 4, 0)),
        ),
    ),
)

ENUM_RULES = (
    Rule("enum_to_flag", runner=_enum_to_flag, changes=(crossing("VY030", (0, 4, 0)),)),
)

POST_NUMERIC_RULES = (
    Rule("struct_kwargs", runner=_struct_kwargs, changes=(crossing("VY060", (0, 4, 0)),)),
    Rule("create_from_blueprint", runner=_create_from_blueprint, changes=(crossing("VY080", (0, 4, 0)),)),
    Rule("max_value_storage_arrays", runner=_max_value_storage_arrays, changes=(crossing("VY091", (0, 4, 0)),)),
    Rule("unreachable_code", runner=_unreachable_code, changes=(crossing("VY092", (0, 4, 0)),)),
    Rule("reserved_flag_storage", runner=_reserved_flag_storage, changes=(crossing("VY093", (0, 3, 4)),)),
    Rule("unbounded_dynarray_limits", runner=_unbounded_dynarray_limits, changes=(crossing("VY094", (0, 4, 0)),)),
    Rule(
        "nonreentrant",
        runner=_nonreentrant,
        changes=(
            crossing("VY090", (0, 4, 0)),
            crossing("VYD002", (0, 4, 0)),
        ),
    ),
)
