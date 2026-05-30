from __future__ import annotations

import re
from pathlib import Path

from vyupgrade.models import Config, Diagnostic, Fix
from vyupgrade.rule_registry import Rule, RuleContext, crossing, rule_changes
from vyupgrade.rules import RULE_CHANGES
from vyupgrade.versions import MigrationContext


RULE_CODE_RE = re.compile(r"""["'](?P<code>VYD?\d{3})["']""")


def test_rule_bind_skips_runner_when_descriptor_is_disabled() -> None:
    calls = 0

    def runner(
        source: str, config: Config, context: MigrationContext
    ) -> tuple[str, list[Fix], list[Diagnostic]]:
        nonlocal calls
        calls += 1
        return source + "changed", [], []

    rule = Rule("sample", runner=runner, changes=(crossing("VYX001", (0, 4, 0)),))
    bound = rule.bind()
    assert bound is not None

    context = RuleContext(
        "source",
        Config(paths=(Path("contract.vy"),), source_version="0.3.10", target_version="0.3.10"),
        MigrationContext.from_specs("0.3.10", "0.3.10"),
        Path("contract.vy"),
        rule_changes((rule,)),
    )

    assert bound(context) == ("source", [], [])
    assert calls == 0


def test_rule_bind_runs_runner_when_any_descriptor_is_enabled() -> None:
    calls = 0

    def runner(
        source: str, config: Config, context: MigrationContext
    ) -> tuple[str, list[Fix], list[Diagnostic]]:
        nonlocal calls
        calls += 1
        return source + " changed", [], []

    rule = Rule(
        "sample",
        runner=runner,
        changes=(crossing("VYX001", (0, 4, 0)), crossing("VYX002", (0, 4, 0))),
    )
    bound = rule.bind()
    assert bound is not None

    context = RuleContext(
        "source",
        Config(paths=(Path("contract.vy"),), source_version="0.3.10", target_version="0.4.0"),
        MigrationContext.from_specs("0.3.10", "0.4.0"),
        Path("contract.vy"),
        rule_changes((rule,)),
    )

    assert bound(context) == ("source changed", [], [])
    assert calls == 1


def test_rule_changes_cover_rule_codes_used_in_source() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "vyupgrade"
    used_codes = {
        match.group("code")
        for path in source_root.rglob("*.py")
        for match in RULE_CODE_RE.finditer(path.read_text())
    }

    assert used_codes - set(RULE_CHANGES) == set()
