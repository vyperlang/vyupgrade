from __future__ import annotations

import re
from pathlib import Path

from vyupgrade.rules import RULE_CHANGES


def test_migration_coverage_references_all_version_gated_rules() -> None:
    coverage = Path("docs/migration-coverage.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bVYD?\d{3}\b", coverage))

    assert set(RULE_CHANGES) <= referenced


def test_migration_coverage_uses_no_tables() -> None:
    coverage = Path("docs/migration-coverage.md").read_text(encoding="utf-8")

    assert not re.search(r"^\|", coverage, re.MULTILINE)
