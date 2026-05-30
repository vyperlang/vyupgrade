from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

from .analysis import SourceFacts, parse_source_facts
from .models import Config, Diagnostic, Fix
from .source import code_mask
from .versions import MigrationContext, VyperVersion


Activation = Literal["crossing", "target_floor", "target_update"]


@dataclass(frozen=True)
class RuleChange:
    introduced: VyperVersion
    activation: Activation = "crossing"


CodeChange = tuple[str, RuleChange]
RuleRunner = Callable[
    [str, Config, MigrationContext], tuple[str, list[Fix], list[Diagnostic]]
]
ContextRuleRunner = Callable[["RuleContext"], tuple[str, list[Fix], list[Diagnostic]]]
_RULE_CHANGES: Mapping[str, RuleChange] = {}


def configure_rule_changes(changes: Mapping[str, RuleChange]) -> None:
    global _RULE_CHANGES
    _RULE_CHANGES = changes


def is_enabled(rule: str, config: Config, context: MigrationContext) -> bool:
    if config.select and rule not in config.select:
        return False
    if rule in config.ignore:
        return False
    change = _RULE_CHANGES.get(rule)
    if change is None:
        return True
    if change.activation in {"target_floor", "target_update"}:
        return context.target_at_least(change.introduced)
    return context.crosses(change.introduced)


def any_enabled(rules: set[str], config: Config, context: MigrationContext) -> bool:
    return any(is_enabled(rule, config, context) for rule in rules)


@dataclass(frozen=True)
class RuleContext:
    source: str
    config: Config
    migration: MigrationContext
    path: Path | None = None
    _is_enabled: Callable[[str], bool] | None = None

    @cached_property
    def code_mask(self) -> list[bool]:
        return code_mask(self.source)

    @cached_property
    def facts(self) -> SourceFacts:
        return parse_source_facts(self.source)

    @cached_property
    def line_offsets(self) -> list[int]:
        offsets = [0]
        for index, char in enumerate(self.source):
            if char == "\n":
                offsets.append(index + 1)
        return offsets

    def with_source(self, source: str) -> RuleContext:
        if source == self.source:
            return self
        return RuleContext(
            source, self.config, self.migration, self.path, self._is_enabled
        )

    def is_enabled(self, rule: str) -> bool:
        if self._is_enabled is None:
            return True
        return self._is_enabled(rule)


@dataclass(frozen=True)
class Rule:
    name: str
    runner: RuleRunner | None = None
    context_runner: ContextRuleRunner | None = None
    changes: tuple[CodeChange, ...] = ()

    def bind(self) -> ContextRuleRunner | None:
        runner: ContextRuleRunner | None
        if self.context_runner is not None:
            runner = self.context_runner
        elif self.runner is not None:
            def source_runner(
                context: RuleContext,
                self: Rule = self,
            ) -> tuple[str, list[Fix], list[Diagnostic]]:
                assert self.runner is not None
                return self.runner(context.source, context.config, context.migration)

            runner = source_runner
        else:
            return None

        def gated_runner(context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
            if not self.is_enabled(context):
                return context.source, [], []
            return runner(context)

        return gated_runner

    def is_enabled(self, context: RuleContext) -> bool:
        if not self.changes:
            return True
        return any(context.is_enabled(code) for code, _change in self.changes)


def crossing(code: str, version: str | tuple[int, int, int]) -> CodeChange:
    return _change(code, version)


def target_floor(code: str, version: str | tuple[int, int, int]) -> CodeChange:
    return _change(code, version, "target_floor")


def target_update(code: str, version: str | tuple[int, int, int]) -> CodeChange:
    return _change(code, version, "target_update")


def rule_changes(rules: tuple[Rule, ...]) -> dict[str, RuleChange]:
    changes: dict[str, RuleChange] = {}
    for rule in rules:
        for code, change in rule.changes:
            if code in changes and changes[code] != change:
                raise ValueError(f"duplicate rule descriptor for {code}")
            changes[code] = change
    return changes


def _change(
    code: str,
    version: str | tuple[int, int, int],
    activation: Activation = "crossing",
) -> CodeChange:
    raw_version = ".".join(str(part) for part in version) if isinstance(version, tuple) else version
    return code, RuleChange(VyperVersion(raw_version), activation)
