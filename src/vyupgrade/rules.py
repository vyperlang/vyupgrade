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
    POST_INTERFACE_RULES as LEGACY_POST_INTERFACE_RULES,
)
from .rule_groups.legacy_interfaces import RULES as LEGACY_INTERFACE_RULES
from .rule_groups.meta import RULES as META_RULES
from .rule_groups.numeric import (
    INTEGER_DIVISION_RULES as NUMERIC_INTEGER_DIVISION_RULES,
    LATE_RULES as NUMERIC_LATE_RULES,
    PRE_INTERFACE_RULES as NUMERIC_PRE_INTERFACE_RULES,
    REDUNDANT_CONVERT_RULES as NUMERIC_REDUNDANT_CONVERT_RULES,
)
from .rule_groups.numeric_constants import (
    BYTES_LITERAL_RULES as NUMERIC_BYTES_LITERAL_RULES,
    CONSTANT_DECL_RULES as NUMERIC_CONSTANT_DECL_RULES,
    CONSTANT_EXPONENT_RULES as NUMERIC_CONSTANT_EXPONENT_RULES,
    DYNAMIC_POW_RULES as NUMERIC_DYNAMIC_POW_RULES,
)
from .rule_groups.numeric_ranges import RULES as NUMERIC_RANGE_RULES
from .rule_groups.numeric_signedness import RULES as NUMERIC_SIGNEDNESS_RULES
from .rule_registry import (
    ContextRuleRunner,
    RuleContext,
    configure_rule_changes,
    is_enabled as _enabled,
    rule_changes,
)
from .versions import MigrationContext, infer_pragma


def apply_rules(source: str, config: Config, path: Path | None = None) -> RewriteResult:
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    context = MigrationContext.from_specs(
        config.source_version or infer_pragma(source), config.target_version
    )

    current = source
    rule_context = RuleContext(
        current, config, context, path, lambda rule: _enabled(rule, config, context)
    )
    for rule in _runnable_rules():
        current, rule_fixes, rule_diagnostics = rule(rule_context)
        rule_context = rule_context.with_source(current)
        fixes.extend(rule_fixes)
        diagnostics.extend(rule_diagnostics)

    fixes = [fix for fix in fixes if _enabled(fix.rule, config, context)]
    diagnostics = [diag for diag in diagnostics if _enabled(diag.rule, config, context)]
    return RewriteResult(current, fixes, diagnostics)


def _runnable_rules() -> Iterator[ContextRuleRunner]:
    for rule in RULES:
        runner = rule.bind()
        if runner is not None:
            yield runner


RULES = (
    *LEGACY_EARLY_RULES,
    *LEGACY_INTERFACE_RULES,
    *LEGACY_POST_INTERFACE_RULES,
    *COMPARISON_RULES,
    *LEGACY_POST_COMPARISON_RULES,
    *NUMERIC_PRE_INTERFACE_RULES,
    *DATA_CONSTRUCTOR_RULES,
    *INTERFACE_RULES,
    *DATA_ENUM_RULES,
    *NUMERIC_RANGE_RULES,
    *EXTERNAL_CALL_RULES,
    *NUMERIC_INTEGER_DIVISION_RULES,
    *NUMERIC_CONSTANT_EXPONENT_RULES,
    *NUMERIC_SIGNEDNESS_RULES,
    *NUMERIC_DYNAMIC_POW_RULES,
    *NUMERIC_REDUNDANT_CONVERT_RULES,
    *NUMERIC_CONSTANT_DECL_RULES,
    *NUMERIC_BYTES_LITERAL_RULES,
    *DATA_POST_NUMERIC_RULES,
    *NUMERIC_LATE_RULES,
    *DIAGNOSTIC_RULES,
    *META_RULES,
)
RULE_CHANGES = rule_changes(RULES)
configure_rule_changes(RULE_CHANGES)
