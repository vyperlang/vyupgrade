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


def test_fixed_array_empty_equality_expands_to_elementwise_checks(config) -> None:
    source = """# @version 0.3.10
@external
def f(values: address[2]) -> bool:
    return values == empty(address[2])
"""

    result = apply_rules(source, config())

    assert (
        "return (values[0] == empty(address) and values[1] == empty(address))"
        in result.source
    )
    assert any(fix.rule == "VY213" for fix in result.fixes)


def test_fixed_array_empty_non_equality_expands_to_elementwise_checks(config) -> None:
    source = """# @version 0.3.10
@external
def f(values: address[2]) -> bool:
    return values != empty(address[2])
"""

    result = apply_rules(source, config())

    assert (
        "return (values[0] != empty(address) or values[1] != empty(address))" in result.source
    )
