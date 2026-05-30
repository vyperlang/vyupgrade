from __future__ import annotations

import re

from ..analysis import infer_expr_type, parse_source_facts
from ..models import Config, Diagnostic, Fix
from ..rule_helpers import literal_integer as _literal_integer
from ..rule_registry import Rule, is_enabled as _enabled, target_floor
from ..source import code_mask, find_matching, line_number, split_top_level_args, span_is_code
from ..versions import MigrationContext, VyperVersion


def _legacy_diagnostics(
    source: str, config: Config, context: MigrationContext
) -> tuple[str, list[Fix], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    if _enabled("VYD210", config, context):
        diagnostics.extend(_byte_string_literal_diagnostics(source))
    if _enabled("VYD211", config, context) and (
        context.source_floor is None or context.source_floor <= VyperVersion("0.2.1")
    ):
        diagnostics.extend(_reserved_value_parameter_diagnostics(source))
    if _enabled("VYD212", config, context):
        diagnostics.extend(_slice_uint256_diagnostics(source))
    if _enabled("VYD213", config, context):
        diagnostics.extend(_len_uint256_diagnostics(source))
    if _enabled("VYD214", config, context):
        diagnostics.extend(_call_kwarg_uint256_diagnostics(source))
    if _enabled("VYD215", config, context):
        mask = code_mask(source)
        diagnostics.extend(
            Diagnostic(
                "VYD215",
                line_number(source, match.start()),
                "RLPList was removed; rewrite this data model manually",
            )
            for match in re.finditer(r"\bRLPList\b", source)
            if span_is_code(mask, match.start(), match.end())
        )
    return source, [], diagnostics


def _byte_string_literal_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    patterns = [
        (
            re.compile(r"\bBytes\s*\[[^\]]+\]\s*=\s*(?=\")"),
            'byte arrays require byte literals such as b"..."',
        ),
        (
            re.compile(r"\bString\s*\[[^\]]+\]\s*=\s*(?=b\")"),
            "strings require string literals, not byte literals",
        ),
    ]
    for pattern, message in patterns:
        for match in pattern.finditer(source):
            if span_is_code(mask, match.start(), match.end()):
                diagnostics.append(
                    Diagnostic("VYD210", line_number(source, match.start()), message)
                )
    return diagnostics


def _reserved_value_parameter_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for match in re.finditer(
        r"^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<args>[^)]*)\)", source, re.MULTILINE
    ):
        args = split_top_level_args(match.group("args"))
        if args is None:
            continue
        for arg in args:
            name = arg.split(":", 1)[0].split("=", 1)[0].strip()
            if name == "value":
                diagnostics.append(
                    Diagnostic(
                        "VYD211",
                        line_number(source, match.start()),
                        "function parameter name 'value' became reserved; rename it and update references",
                    )
                )
                break
    return diagnostics


def _slice_uint256_diagnostics(source: str) -> list[Diagnostic]:
    facts = parse_source_facts(source)
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bslice\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None or len(args) < 3:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        if not _is_uint256_expr(args[1], vars_for_line) or not _is_uint256_expr(
            args[2], vars_for_line
        ):
            diagnostics.append(
                Diagnostic(
                    "VYD212",
                    line_number(source, match.start()),
                    "slice start and length must be uint256",
                )
            )
    return diagnostics


def _len_uint256_diagnostics(source: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    pattern = re.compile(
        r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*(?P<typ>i?nt(?:8|16|32|64|128|256)?)\s*=\s*len\s*\(",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if match.group("typ") != "uint256":
            diagnostics.append(
                Diagnostic(
                    "VYD213",
                    line_number(source, match.start()),
                    "len() returns uint256; update the receiving type",
                )
            )
    return diagnostics


def _call_kwarg_uint256_diagnostics(source: str) -> list[Diagnostic]:
    facts = parse_source_facts(source)
    diagnostics: list[Diagnostic] = []
    mask = code_mask(source)
    for match in re.finditer(
        r"(?<![\w.])(?:[A-Za-z_][A-Za-z0-9_]*\([^)\n]*\)|(?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\s*\(",
        source,
    ):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        for arg in args:
            name, sep, value = arg.partition("=")
            if not sep or name.strip() not in {"gas", "value"}:
                continue
            if not _is_uint256_expr(value, vars_for_line):
                diagnostics.append(
                    Diagnostic(
                        "VYD214",
                        line_number(source, match.start()),
                        f"external-call {name.strip()} kwarg must be uint256",
                    )
                )
    return diagnostics


def _is_uint256_expr(expr: str, vars_for_line: dict[str, str]) -> bool:
    expr = expr.strip()
    if _literal_integer(expr):
        return True
    expr_type = infer_expr_type(expr, vars_for_line)
    return expr_type == "uint256"



RULES = (
    Rule(
        "legacy_diagnostics",
        runner=_legacy_diagnostics,
        changes=(
            target_floor("VYD210", (0, 2, 1)),
            target_floor("VYD211", (0, 2, 1)),
            target_floor("VYD212", (0, 2, 1)),
            target_floor("VYD213", (0, 2, 1)),
            target_floor("VYD214", (0, 2, 1)),
            target_floor("VYD215", (0, 2, 1)),
        ),
    ),
)
