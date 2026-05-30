from __future__ import annotations

from collections.abc import Callable
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
PathRuleFactory = Callable[[Path | None], RuleRunner]
ContextRuleRunner = Callable[["RuleContext"], tuple[str, list[Fix], list[Diagnostic]]]


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
    path_runner: PathRuleFactory | None = None
    context_runner: ContextRuleRunner | None = None
    changes: tuple[CodeChange, ...] = ()

    def bind(self) -> ContextRuleRunner | None:
        if self.context_runner is not None:
            return self.context_runner
        if self.runner is not None:
            return lambda context: self.runner(
                context.source, context.config, context.migration
            )
        if self.path_runner is not None:
            return lambda context: self.path_runner(context.path)(
                context.source, context.config, context.migration
            )
        return None


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
            if code in changes:
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
