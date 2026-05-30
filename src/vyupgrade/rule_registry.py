from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import Config, Diagnostic, Fix
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


@dataclass(frozen=True)
class Rule:
    name: str
    runner: RuleRunner | None = None
    path_runner: PathRuleFactory | None = None
    changes: tuple[CodeChange, ...] = ()

    def bind(self, path: Path | None) -> RuleRunner | None:
        if self.runner is not None:
            return self.runner
        if self.path_runner is not None:
            return self.path_runner(path)
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
