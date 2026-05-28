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


def test_migration_coverage_tracks_syntax_history_versions() -> None:
    history = Path("docs/vyper-syntax-history.md").read_text(encoding="utf-8")
    coverage = Path("docs/migration-coverage.md").read_text(encoding="utf-8")

    history_versions = set(re.findall(r"^### v0\.\d+\.\d+$", history, re.MULTILINE))
    coverage_versions = set(re.findall(r"^### v0\.\d+\.\d+$", coverage, re.MULTILINE))

    assert history_versions <= coverage_versions


def test_migration_coverage_has_no_unresolved_gaps() -> None:
    coverage = Path("docs/migration-coverage.md").read_text(encoding="utf-8")

    assert "no automated rule yet" not in coverage
    assert "TODO" not in coverage
