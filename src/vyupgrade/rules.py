from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .models import Config, Diagnostic, Fix, RewriteResult
from .rule_groups.comparisons import not_in_comparator
from .rule_groups.data_lifecycle import (
    _abi_builtins,
    _constructor_deploy,
    _create_from_blueprint,
    _enum_to_flag,
    _nonreentrant,
    _struct_kwargs,
)
from .rule_groups.diagnostics import (
    decimal_diagnostic,
    missing_pragma_diagnostic,
    prevrandao_diagnostic,
)
from .rule_groups.interfaces import (
    _absolute_relative_imports,
    _constant_accessor_collisions,
    _immutable_accessor_collisions,
    _interface_imports,
    _interface_view_mutability,
    _legacy_constants,
    _pure_immutable_reads,
)
from .rule_groups.legacy import (
    _early_beta_syntax,
    _event_kwargs,
    _legacy_builtin_calls,
    _legacy_constructor_locks,
    _legacy_decorators,
    _legacy_diagnostics,
    _legacy_dynamic_types,
    _legacy_events,
    _legacy_maps_and_interfaces,
    _legacy_type_units,
    _natspec_strictness,
    _pragma,
    _reserved_parameter_names,
)
from .rule_groups.external_calls import (
    _external_call_keywords,
    _external_call_subscripts,
    ignored_external_call_results,
)
from .rule_groups.numeric import (
    _bitwise,
    _constant_exponent_literals_context,
    _constant_integer_decl_casts,
    _dynamic_bytes_hex_literals,
    _dynamic_pow_mod256,
    _integer_assignment_casts,
    _integer_division,
    _mixed_signed_unsigned_arithmetic,
    _pre_04_expression_rewrites,
    _range_bound,
    _redundant_integer_convert,
    _signed_integer_array_constant_types,
    _sqrt,
    _typed_array_literal_arguments,
    _typed_external_call_arguments,
    _typed_range_loops,
    _unsigned_range_bound_signed_constants,
)
from .rule_registry import (
    ContextRuleRunner,
    Rule,
    RuleContext,
    configure_rule_changes,
    crossing,
    is_enabled as _enabled,
    rule_changes,
    target_floor,
    target_update,
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
    Rule("pragma", runner=_pragma, changes=(target_update("VY001", (0, 3, 10)),)),
    Rule("legacy_decorators", runner=_legacy_decorators, changes=(target_floor("VY201", (0, 2, 1)),)),
    Rule("legacy_type_units", runner=_legacy_type_units, changes=(target_floor("VY202", (0, 2, 1)),)),
    Rule(
        "legacy_events",
        runner=_legacy_events,
        changes=(
            target_floor("VY203", (0, 2, 1)),
            target_floor("VY204", (0, 2, 1)),
        ),
    ),
    Rule("event_kwargs", runner=_event_kwargs, changes=(crossing("VY112", (0, 4, 1)),)),
    Rule(
        "legacy_maps_and_interfaces",
        runner=_legacy_maps_and_interfaces,
        changes=(
            target_floor("VY205", (0, 2, 1)),
            target_floor("VY206", (0, 2, 1)),
        ),
    ),
    Rule(
        "early_beta_syntax",
        runner=_early_beta_syntax,
        changes=(
            target_floor("VY216", (0, 2, 1)),
            target_floor("VY217", (0, 2, 1)),
            target_floor("VY218", (0, 2, 1)),
            target_floor("VY219", (0, 2, 1)),
            target_floor("VY221", (0, 2, 1)),
        ),
    ),
    Rule("legacy_dynamic_types", runner=_legacy_dynamic_types, changes=(target_floor("VY207", (0, 2, 1)),)),
    Rule("reserved_parameter_names", runner=_reserved_parameter_names, changes=(target_floor("VY212", (0, 2, 1)),)),
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
    Rule("natspec_strictness", runner=_natspec_strictness, changes=(crossing("VY058", (0, 4, 0)),)),
    Rule(
        "legacy_builtin_calls",
        runner=_legacy_builtin_calls,
        changes=(
            target_floor("VY208", (0, 2, 1)),
            target_floor("VY209", (0, 2, 1)),
        ),
    ),
    Rule(
        "not_in_comparator",
        context_runner=not_in_comparator,
        changes=(crossing("VY211", (0, 2, 8)),),
    ),
    Rule("legacy_constructor_locks", runner=_legacy_constructor_locks, changes=(crossing("VY210", (0, 2, 16)),)),
    Rule(
        "pre_04_expression_rewrites",
        runner=_pre_04_expression_rewrites,
        changes=(
            crossing("VY220", (0, 3, 7)),
            crossing("VY230", (0, 3, 8)),
            crossing("VY231", (0, 3, 8)),
            crossing("VYD013", (0, 3, 8)),
        ),
    ),
    Rule("constructor_deploy", runner=_constructor_deploy, changes=(crossing("VY002", (0, 4, 0)),)),
    Rule(
        "abi_builtins",
        runner=_abi_builtins,
        changes=(
            crossing("VY010", (0, 4, 0)),
            crossing("VY011", (0, 4, 0)),
        ),
    ),
    Rule("legacy_constants", runner=_legacy_constants, changes=(crossing("VY012", (0, 4, 0)),)),
    Rule("immutable_accessor_collisions", runner=_immutable_accessor_collisions, changes=(crossing("VY013", (0, 4, 0)),)),
    Rule("constant_accessor_collisions", runner=_constant_accessor_collisions, changes=(crossing("VY016", (0, 4, 0)),)),
    Rule("interface_view_mutability", runner=_interface_view_mutability, changes=(crossing("VY014", (0, 4, 0)),)),
    Rule("pure_immutable_reads", runner=_pure_immutable_reads, changes=(crossing("VY015", (0, 4, 0)),)),
    Rule(
        "interface_imports",
        runner=_interface_imports,
        changes=(
            crossing("VY020", (0, 4, 0)),
            crossing("VYD003", (0, 4, 0)),
        ),
    ),
    Rule(
        "absolute_relative_imports",
        path_runner=_absolute_relative_imports,
        changes=(crossing("VYD015", (0, 4, 1)),),
    ),
    Rule("enum_to_flag", runner=_enum_to_flag, changes=(crossing("VY030", (0, 4, 0)),)),
    Rule(
        "range_bound",
        runner=_range_bound,
        changes=(
            crossing("VY071", (0, 4, 0)),
            crossing("VYD011", (0, 4, 0)),
            crossing("VYD014", (0, 3, 10)),
        ),
    ),
    Rule("typed_range_loops", runner=_typed_range_loops, changes=(crossing("VY070", (0, 4, 0)),)),
    Rule("integer_assignment_casts", runner=_integer_assignment_casts, changes=(crossing("VY052", (0, 4, 0)),)),
    Rule(
        "external_call_keywords",
        runner=_external_call_keywords,
        changes=(
            crossing("VY040", (0, 4, 0)),
            crossing("VY041", (0, 4, 0)),
        ),
    ),
    Rule("external_call_subscripts", runner=_external_call_subscripts, changes=(crossing("VY042", (0, 4, 0)),)),
    Rule("external_call_keywords_after_subscripts", runner=_external_call_keywords),
    Rule(
        "ignored_external_call_results",
        context_runner=ignored_external_call_results,
        changes=(crossing("VY057", (0, 4, 0)),),
    ),
    Rule(
        "integer_division",
        runner=_integer_division,
        changes=(
            crossing("VY050", (0, 4, 0)),
            crossing("VYD004", (0, 4, 0)),
        ),
    ),
    Rule(
        "constant_exponent_literals",
        context_runner=_constant_exponent_literals_context,
        changes=(crossing("VY054", (0, 4, 0)),),
    ),
    Rule("mixed_signed_unsigned_arithmetic", runner=_mixed_signed_unsigned_arithmetic),
    Rule("signed_integer_array_constant_types", runner=_signed_integer_array_constant_types),
    Rule("typed_array_literal_arguments", runner=_typed_array_literal_arguments),
    Rule("unsigned_range_bound_signed_constants", runner=_unsigned_range_bound_signed_constants, changes=(crossing("VY056", (0, 4, 0)),)),
    Rule("typed_external_call_arguments", runner=_typed_external_call_arguments),
    Rule("dynamic_pow_mod256", runner=_dynamic_pow_mod256, changes=(crossing("VY055", (0, 4, 0)),)),
    Rule("redundant_integer_convert", runner=_redundant_integer_convert, changes=(crossing("VY051", (0, 4, 0)),)),
    Rule("constant_integer_decl_casts", runner=_constant_integer_decl_casts),
    Rule("dynamic_bytes_hex_literals", runner=_dynamic_bytes_hex_literals, changes=(crossing("VY053", (0, 4, 0)),)),
    Rule("struct_kwargs", runner=_struct_kwargs, changes=(crossing("VY060", (0, 4, 0)),)),
    Rule("create_from_blueprint", runner=_create_from_blueprint, changes=(crossing("VY080", (0, 4, 0)),)),
    Rule(
        "nonreentrant",
        runner=_nonreentrant,
        changes=(
            crossing("VY090", (0, 4, 0)),
            crossing("VYD002", (0, 4, 0)),
        ),
    ),
    Rule("sqrt", runner=_sqrt, changes=(crossing("VY100", (0, 4, 2)),)),
    Rule(
        "bitwise",
        runner=_bitwise,
        changes=(
            crossing("VY110", (0, 4, 2)),
            crossing("VY111", (0, 4, 2)),
            crossing("VYD012", (0, 4, 2)),
        ),
    ),
    Rule("decimal_diagnostic", context_runner=decimal_diagnostic, changes=(crossing("VYD001", (0, 4, 0)),)),
    Rule("prevrandao_diagnostic", context_runner=prevrandao_diagnostic, changes=(crossing("VYD010", (0, 4, 0)),)),
    Rule("missing_pragma_diagnostic", context_runner=missing_pragma_diagnostic, changes=(crossing("VYD005", (0, 4, 0)),)),
    Rule("interface_split", changes=(crossing("VY120", (0, 4, 0)),)),
    Rule(
        "validation",
        changes=(
            crossing("VYD006", (0, 4, 0)),
            crossing("VYD007", (0, 4, 0)),
            crossing("VYD008", (0, 4, 0)),
            crossing("VYD009", (0, 4, 0)),
        ),
    ),
)

RULE_CHANGES = rule_changes(RULES)
configure_rule_changes(RULE_CHANGES)
