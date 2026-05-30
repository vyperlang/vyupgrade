from __future__ import annotations

import re

from ..analysis import SourceFacts, infer_expr_type, normalize_type, parse_source_facts
from ..models import Config, Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..rule_helpers import (
    innermost_non_overlapping as _innermost_non_overlapping,
    lhs_assigned_type as _lhs_assigned_type,
    lhs_declared_type as _lhs_declared_type,
    literal_integer as _literal_integer,
)
from ..source import (
    TextEdit,
    apply_edits,
    code_mask,
    line_number,
    span_is_code,
)
from ..versions import MigrationContext
from .numeric_constant_helpers import eval_integer_constant_expr, integer_constant_values


def _constant_integer_decl_casts(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    integer_type = r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)"
    pattern = re.compile(
        rf"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\(\s*(?P<type>{integer_type})\s*\)\s*=\s*(?P<value>[^\n#]+)(?P<comment>[ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start("name"), match.end("value")):
            continue
        expected_type = normalize_type(match.group("type"))
        if expected_type == "uint256":
            continue
        value = match.group("value").strip()
        if value.startswith("convert(") or _literal_integer(value):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        actual_type = infer_expr_type(value, vars_for_line, facts)
        if actual_type is not None and normalize_type(actual_type) == expected_type:
            continue
        folded = eval_integer_constant_expr(value, integer_constant_values(source, config.source_ast))
        if folded is None or not _integer_value_fits_type(folded, expected_type):
            continue
        before = match.group(0)
        after = f"{match.group('indent')}{match.group('name')}: constant({expected_type}) = {folded}{match.group('comment')}"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(
            Fix(
                "VY052",
                line_number(source, match.start()),
                "folded integer constant initializer to declared type",
                before,
                after,
            )
        )
    return apply_edits(source, edits), fixes, []


def _integer_value_fits_type(value: int, type_name: str) -> bool:
    match = re.fullmatch(r"(u?)int(\d+)", type_name)
    if match is None:
        return False
    bits = int(match.group(2))
    if match.group(1):
        return 0 <= value < 2**bits
    return -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)


def _constant_exponent_literals_context(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    config = rule_context.config
    facts = rule_context.facts
    mask = rule_context.code_mask
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    max_int128_re = re.compile(r"(?<![\w])(?:\(\s*)?2\s*\*\*\s*127\s*-\s*1(?:\s*\))?")
    for match in max_int128_re.finditer(source):
        if not span_is_code(mask, match.start(), match.end()) or not _int128_literal_context(
            source, match.start(), facts
        ):
            continue
        replacement = "max_value(int128)"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY054",
                line_number(source, match.start()),
                "replaced signed int128 max literal",
                match.group(0),
                replacement,
            )
        )
    constant_values = integer_constant_values(source, config.source_ast)
    for name, value in constant_values.items():
        if value < 0:
            continue
        name_re = re.compile(rf"\b{re.escape(name)}\b")
        for name_match in name_re.finditer(source):
            start = name_match.start()
            end = name_match.end()
            if not span_is_code(mask, start, end) or not _inside_exponent(source, start, end):
                continue
            replacement = str(value)
            edits.append(TextEdit(start, end, replacement))
            fixes.append(
                Fix(
                    "VY054",
                    line_number(source, start),
                    "folded integer constant in unsigned exponent expression",
                    name,
                    replacement,
                )
            )
    selected_edits, selected_fixes = _innermost_non_overlapping(edits, fixes)
    return apply_edits(source, selected_edits), selected_fixes, []


def _int128_literal_context(source: str, index: int, facts: SourceFacts) -> bool:
    line_no = line_number(source, index)
    return_type = facts.return_type_at_line(line_no)
    if normalize_type(return_type or "") == "int128":
        return True
    line_start = source.rfind("\n", 0, index) + 1
    line_end = source.find("\n", index)
    if line_end == -1:
        line_end = len(source)
    line = source[line_start:line_end]
    vars_for_line = facts.vars_at_line(line_no)
    return (
        normalize_type(_lhs_declared_type(line) or _lhs_assigned_type(line, vars_for_line) or "")
        == "int128"
    )


def _dynamic_pow_mod256(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    convert_operand = r"convert\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*uint256\s*\)"
    pattern = re.compile(rf"(?P<left>{convert_operand})\s*\*\*\s*(?P<right>{convert_operand})")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()) or _top_level_constant_line(
            source, match.start()
        ):
            continue
        left = match.group("left")
        right = match.group("right")
        replacement = f"pow_mod256({left}, {right})"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY055",
                line_number(source, match.start()),
                "rewrote dynamic exponentiation to pow_mod256",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _inside_exponent(source: str, start: int, end: int) -> bool:
    before = source[max(0, start - 8) : start]
    after = source[end : min(len(source), end + 8)]
    return bool(re.search(r"\*\*\s*$", before) or re.match(r"\s*\*\*", after))


def _top_level_constant_line(source: str, index: int) -> bool:
    line_start = source.rfind("\n", 0, index) + 1
    return bool(re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*:\s*constant\s*\(", source[line_start:]))


def _dynamic_bytes_hex_literals(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    pattern = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*:\s*Bytes\[[^\]]+\]\s*=\s*(0x[0-9A-Fa-f]*)\b")
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = _hex_literal_to_byte_string(match.group(1))
        if replacement is None:
            continue
        edits.append(TextEdit(match.start(1), match.end(1), replacement))
        fixes.append(
            Fix(
                "VY053",
                line_number(source, match.start()),
                "changed dynamic bytes hex literal to byte string literal",
                match.group(1),
                replacement,
            )
        )
    return apply_edits(source, edits), fixes, []


def _hex_literal_to_byte_string(literal: str) -> str | None:
    raw = literal.removeprefix("0x")
    if len(raw) % 2 != 0:
        return None
    return (
        'b"'
        + "".join(f"\\x{raw[index : index + 2].lower()}" for index in range(0, len(raw), 2))
        + '"'
    )




CONSTANT_EXPONENT_RULES = (
    Rule(
        "constant_exponent_literals",
        context_runner=_constant_exponent_literals_context,
        changes=(crossing("VY054", (0, 4, 0)),),
    ),
)

DYNAMIC_POW_RULES = (
    Rule("dynamic_pow_mod256", runner=_dynamic_pow_mod256, changes=(crossing("VY055", (0, 4, 0)),)),
)

CONSTANT_DECL_RULES = (
    Rule(
        "constant_integer_decl_casts",
        runner=_constant_integer_decl_casts,
        changes=(crossing("VY052", (0, 4, 0)),),
    ),
)

BYTES_LITERAL_RULES = (
    Rule("dynamic_bytes_hex_literals", runner=_dynamic_bytes_hex_literals, changes=(crossing("VY053", (0, 4, 0)),),),
)
