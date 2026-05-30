from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_legacy_0_2_1_hard_cases_emit_diagnostics(config) -> None:
    source = """# @version 0.2.1
xs: Bytes[5] = "hello"
name: String[5] = b"hello"

@external
def f(value: int128, data: Bytes[32], start: int128, length: int128, target: address):
    n: int128 = len(data)
    slice(data, start, length)
    target.foo(value=value, gas=start)

@external
def g(items: RLPList(uint256)):
    pass
"""

    result = apply_rules(source, config(target_version="0.2.1"))
    rules = {diag.rule for diag in result.diagnostics}

    assert {"VYD210", "VYD212", "VYD213", "VYD214", "VYD215"} <= rules
    assert "VYD211" not in rules
    assert "def f(_value: int128" in result.source
