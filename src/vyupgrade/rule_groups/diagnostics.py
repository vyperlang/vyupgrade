from __future__ import annotations

import re

from ..models import Diagnostic, Fix
from ..rule_registry import RuleContext
from ..source import line_number


def decimal_diagnostic(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    if not rule_context.is_enabled("VYD001"):
        return source, [], []
    if re.search(r"\bdecimal\b", source) and not rule_context.config.enable_decimals:
        return (
            source,
            [],
            [
                Diagnostic(
                    "VYD001",
                    1,
                    "decimal type is used; target compile may require --enable-decimals",
                )
            ],
        )
    return source, [], []


def prevrandao_diagnostic(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    if not rule_context.is_enabled("VYD010"):
        return source, [], []
    diagnostics = [
        Diagnostic(
            "VYD010",
            line_number(source, match.start()),
            "block.prevrandao signature changed in 0.4.0; review manually",
        )
        for match in re.finditer(r"\bblock\.prevrandao\b", source)
    ]
    return source, [], diagnostics


def missing_pragma_diagnostic(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    if not rule_context.is_enabled("VYD005"):
        return source, [], []
    if rule_context.migration.source_spec is None and rule_context.config.source_version is None:
        return (
            source,
            [],
            [Diagnostic("VYD005", 1, "source has no version pragma and no --source-version")],
        )
    return source, [], []
