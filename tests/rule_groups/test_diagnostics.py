from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_missing_source_pragma_still_reports_unknown_source_version(config) -> None:
    source = """@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert result.source.startswith("#pragma version 0.4.3\n")
    assert [diagnostic.rule for diagnostic in result.diagnostics] == ["VYD005"]
