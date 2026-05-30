from __future__ import annotations

import re

from ..models import Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..source import line_number


def decimal_diagnostic(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
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
    if rule_context.migration.source_spec is None and rule_context.config.source_version is None:
        return (
            source,
            [],
            [Diagnostic("VYD005", 1, "source has no version pragma and no --source-version")],
        )
    return source, [], []


RULES = (
    Rule("decimal_diagnostic", context_runner=decimal_diagnostic, changes=(crossing("VYD001", (0, 4, 0)),)),
    Rule("prevrandao_diagnostic", context_runner=prevrandao_diagnostic, changes=(crossing("VYD010", (0, 4, 0)),)),
    Rule("missing_pragma_diagnostic", context_runner=missing_pragma_diagnostic, changes=(crossing("VYD005", (0, 4, 0)),)),
)
