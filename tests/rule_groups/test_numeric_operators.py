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


def test_unary_plus_after_augmented_assignment_is_rewritten(config) -> None:
    source = """# @version 0.3.7
liquidity: uint256

@external
def f(new_balance: uint256):
    self.liquidity += + new_balance
"""

    result = apply_rules(source, config(target_version="0.3.8"))

    assert "self.liquidity += new_balance" in result.source
    assert any(fix.rule == "VY230" for fix in result.fixes)


def test_line_leading_binary_plus_is_preserved_in_tuple_sum(config) -> None:
    source = """# @version 0.3.7
@external
def f(a: uint256, b: uint256) -> uint256:
    return (
        a
        + b
    )
"""

    result = apply_rules(source, config(target_version="0.3.8"))

    assert "        + b" in result.source
    assert not any(fix.rule == "VY230" for fix in result.fixes)


def test_line_leading_binary_plus_is_preserved_after_backslash(config) -> None:
    source = """# @version 0.3.7
@external
def f(a: uint256, b: uint256) -> uint256:
    total: uint256 = a \\
        + b
    return total
"""

    result = apply_rules(source, config(target_version="0.3.8"))

    assert "        + b" in result.source
    assert not any(fix.rule == "VY230" for fix in result.fixes)


def test_line_leading_unary_plus_after_open_paren_is_rewritten(config) -> None:
    source = """# @version 0.3.7
@external
def f(a: uint256) -> uint256:
    return (
        + a
    )
"""

    result = apply_rules(source, config(target_version="0.3.8"))

    assert "        a" in result.source
    assert any(fix.rule == "VY230" for fix in result.fixes)


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
