from __future__ import annotations

import pytest

from vyupgrade.rules import apply_rules


@pytest.mark.parametrize(
    "text",
    [
        '# keccak("SKIP_CONFIG") in decimal; block.prevrandao\n',
        'label: constant(String[32]) = "decimal block.prevrandao"\n',
        "label: constant(String[32]) = 'decimal block.prevrandao'\n",
        '"""Contract docs:\ndecimal and block.prevrandao\n"""\n',
        "'''Contract docs:\ndecimal and block.prevrandao\n'''\n",
        '@external\ndef f():\n    """decimal block.prevrandao"""\n    pass\n',
    ],
)
def test_diagnostics_ignore_comments_strings_and_docstrings(config, text: str) -> None:
    source = "# @version 0.3.10\n" + text

    result = apply_rules(source, config(select=frozenset({"VYD001", "VYD010"})))

    assert result.source == source
    assert result.fixes == []
    assert result.diagnostics == []


def test_diagnostics_keep_code_matches_after_comments_and_strings(config) -> None:
    source = '''# @version 0.3.10
"""decimal block.prevrandao"""
# decimal block.prevrandao
value: decimal
other: decimal

@external
def f() -> uint256:
    label: String[32] = "decimal block.prevrandao"
    first: uint256 = block.prevrandao  # decimal block.prevrandao
    return block.prevrandao
'''

    result = apply_rules(source, config(select=frozenset({"VYD001", "VYD010"})))

    assert result.source == source
    assert result.fixes == []
    assert [(diagnostic.rule, diagnostic.line) for diagnostic in result.diagnostics] == [
        ("VYD001", 1),
        ("VYD010", 10),
        ("VYD010", 11),
    ]


def test_decimal_diagnostic_respects_enable_decimals(config) -> None:
    source = "# @version 0.3.10\nvalue: decimal\n"

    result = apply_rules(source, config(select=frozenset({"VYD001"}), enable_decimals=True))

    assert result.diagnostics == []


def test_missing_source_pragma_still_reports_unknown_source_version(config) -> None:
    source = """@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert result.source.startswith("#pragma version 0.4.3\n")
    assert [diagnostic.rule for diagnostic in result.diagnostics] == ["VYD005"]
