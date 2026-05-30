from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_block_difficulty_alias_is_rewritten_when_crossing_0_3_7(config) -> None:
    source = """# @version 0.3.6
@external
def f() -> uint256:
    return block.difficulty
"""

    result = apply_rules(source, config(target_version="0.3.7"))

    assert "block.prevrandao" in result.source
    assert "block.difficulty" not in result.source
    assert any(fix.rule == "VY220" for fix in result.fixes)


def test_unary_plus_and_numeric_not_rewrite_when_crossing_0_3_8(config) -> None:
    source = """# @version 0.3.7
@external
def f(amount: uint256, ok: bool) -> bool:
    x: uint256 = +amount
    if not amount:
        return ok
    return not ok
"""

    result = apply_rules(source, config(target_version="0.3.8"))

    assert "x: uint256 = amount" in result.source
    assert "if amount == 0:" in result.source
    assert "return not ok" in result.source
    assert {fix.rule for fix in result.fixes} >= {"VY230", "VY231"}


def test_numeric_not_unknown_type_is_diagnostic_only(config) -> None:
    source = """# @version 0.3.7
@external
def f():
    if not amount:
        pass
"""

    result = apply_rules(source, config(target_version="0.3.8"))

    assert "if not amount:" in result.source
    assert any(diag.rule == "VYD013" for diag in result.diagnostics)
