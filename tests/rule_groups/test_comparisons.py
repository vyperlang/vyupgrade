from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_not_in_comparator_rewrites_when_crossing_0_2_8(config) -> None:
    source = """# @version 0.2.7
@external
def f(x: uint256, values: uint256[3]) -> bool:
    return not (x in values)
"""

    result = apply_rules(source, config(target_version="0.2.8"))

    assert "return x not in values" in result.source
    assert any(fix.rule == "VY211" for fix in result.fixes)
