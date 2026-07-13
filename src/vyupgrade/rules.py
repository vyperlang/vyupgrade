from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .models import Config, Diagnostic, Fix, RewriteResult
from .rule_groups.comparisons import RULES as COMPARISON_RULES
from .rule_groups.data_lifecycle import (
    CONSTRUCTOR_RULES as DATA_CONSTRUCTOR_RULES,
    ENUM_RULES as DATA_ENUM_RULES,
    POST_NUMERIC_RULES as DATA_POST_NUMERIC_RULES,
)
from .rule_groups.diagnostics import RULES as DIAGNOSTIC_RULES
from .rule_groups.external_calls import RULES as EXTERNAL_CALL_RULES
from .rule_groups.interfaces import RULES as INTERFACE_RULES
from .rule_groups.legacy import (
    EARLY_RULES as LEGACY_EARLY_RULES,
    POST_COMPARISON_RULES as LEGACY_POST_COMPARISON_RULES,
    POST_DIAGNOSTIC_RULES as LEGACY_POST_DIAGNOSTIC_RULES,
    POST_INTERFACE_RULES as LEGACY_POST_INTERFACE_RULES,
)
from .rule_groups.legacy_builtins import RULES as LEGACY_BUILTIN_RULES
from .rule_groups.legacy_diagnostics import RULES as LEGACY_DIAGNOSTIC_RULES
from .rule_groups.legacy_interfaces import RULES as LEGACY_INTERFACE_RULES
from .rule_groups.numeric import (
    INTEGER_DIVISION_RULES as NUMERIC_INTEGER_DIVISION_RULES,
    LATE_RULES as NUMERIC_LATE_RULES,
    REDUNDANT_CONVERT_RULES as NUMERIC_REDUNDANT_CONVERT_RULES,
)
from .rule_groups.numeric_constants import (
    BYTES_LITERAL_RULES as NUMERIC_BYTES_LITERAL_RULES,
    CONSTANT_DECL_RULES as NUMERIC_CONSTANT_DECL_RULES,
    CONSTANT_EXPONENT_RULES as NUMERIC_CONSTANT_EXPONENT_RULES,
    DYNAMIC_POW_RULES as NUMERIC_DYNAMIC_POW_RULES,
)
from .rule_groups.numeric_context_casts import RULES as NUMERIC_CONTEXT_CAST_RULES
from .rule_groups.numeric_operators import RULES as NUMERIC_OPERATOR_RULES
from .rule_groups.numeric_ranges import RULES as NUMERIC_RANGE_RULES
from .rule_groups.numeric_signedness import RULES as NUMERIC_SIGNEDNESS_RULES
from .rule_registry import (
    Rule,
    RuleRunner,
    RuleContext,
    crossing,
    rule_changes,
    target_floor,
)
from .versions import MigrationContext, infer_pragma


def apply_rules(source: str, config: Config, path: Path | None = None) -> RewriteResult:
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    context = MigrationContext.from_specs(
        config.source_version or infer_pragma(source), config.target_version
    )
    rule_context = RuleContext(source, config, context, path, RULE_CHANGES)
    if context.source_spec_unsupported():
        return _blocked_source_version_result(
            source,
            rule_context,
            _unsupported_source_version_diagnostic(context),
        )
    if context.source_newer_than_target():
        return _blocked_source_version_result(
            source,
            rule_context,
            _source_newer_than_target_diagnostic(context),
        )

    current = source
    for rule in _runnable_rules():
        current, rule_fixes, rule_diagnostics = rule(rule_context)
        rule_context = rule_context.with_source(current)
        fixes.extend(rule_fixes)
        diagnostics.extend(rule_diagnostics)

    fixes = [fix for fix in fixes if rule_context.is_enabled(fix.rule)]
    diagnostics = [diag for diag in diagnostics if rule_context.is_enabled(diag.rule)]
    return RewriteResult(current, fixes, diagnostics)


def _runnable_rules() -> Iterator[RuleRunner]:
    for rule in RULES:
        runner = rule.bind()
        if runner is not None:
            yield runner


METADATA_RULES = (
    Rule("interface_split", changes=(target_floor("VY120", (0, 4, 0)),)),
    Rule(
        "validation",
        changes=(
            crossing("VYD006", (0, 4, 0)),
            crossing("VYD007", (0, 4, 0)),
            crossing("VYD008", (0, 4, 0)),
            crossing("VYD009", (0, 4, 0)),
            target_floor("VYD016", (0, 1, 0)),
        ),
    ),
)

RULES = (
    *LEGACY_EARLY_RULES,
    *LEGACY_INTERFACE_RULES,
    *LEGACY_POST_INTERFACE_RULES,
    *LEGACY_DIAGNOSTIC_RULES,
    *LEGACY_POST_DIAGNOSTIC_RULES,
    *LEGACY_BUILTIN_RULES,
    *COMPARISON_RULES,
    *LEGACY_POST_COMPARISON_RULES,
    *NUMERIC_OPERATOR_RULES,
    *DATA_CONSTRUCTOR_RULES,
    *INTERFACE_RULES,
    *DATA_ENUM_RULES,
    *NUMERIC_RANGE_RULES,
    *EXTERNAL_CALL_RULES,
    *NUMERIC_INTEGER_DIVISION_RULES,
    *NUMERIC_CONSTANT_EXPONENT_RULES,
    *NUMERIC_SIGNEDNESS_RULES,
    *NUMERIC_CONTEXT_CAST_RULES,
    *NUMERIC_DYNAMIC_POW_RULES,
    *NUMERIC_REDUNDANT_CONVERT_RULES,
    *NUMERIC_CONSTANT_DECL_RULES,
    *NUMERIC_BYTES_LITERAL_RULES,
    *DATA_POST_NUMERIC_RULES,
    *NUMERIC_LATE_RULES,
    *DIAGNOSTIC_RULES,
    *METADATA_RULES,
)
RULE_CHANGES = rule_changes(RULES)


def _blocked_source_version_result(
    source: str, rule_context: RuleContext, diagnostic: Diagnostic
) -> RewriteResult:
    return RewriteResult(
        source,
        [],
        [diagnostic] if rule_context.is_enabled(diagnostic.rule) else [],
    )


def _unsupported_source_version_diagnostic(context: MigrationContext) -> Diagnostic:
    assert context.source_spec is not None
    return Diagnostic(
        "VYD016",
        1,
        f"source version {context.source_spec} matches no Vyper compiler supported by this vyupgrade release",
        "error",
    )


def _source_newer_than_target_diagnostic(context: MigrationContext) -> Diagnostic:
    assert context.source_floor is not None
    return Diagnostic(
        "VYD016",
        1,
        f"source version {context.source_spec} resolves to {context.source_floor}, which is newer than target {context.target_version}; choose a target >= {context.source_floor}",
        "error",
    )
