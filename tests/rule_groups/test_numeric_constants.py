from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_unsigned_constant_exponent_uses_folded_integer_constants(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 3
PRICE_SIZE: constant(int128) = 256 // (N_COINS - 1)
PRICE_MASK: constant(uint256) = 2**PRICE_SIZE - 1
MAX_A: constant(uint256) = N_COINS**N_COINS * 1000
"""

    result = apply_rules(source, config())

    assert "PRICE_MASK: constant(uint256) = 2**128 - 1" in result.source
    assert "MAX_A: constant(uint256) = 3**3 * 1000" in result.source
    assert any(fix.rule == "VY054" for fix in result.fixes)


def test_int128_max_literal_rewrites_to_max_value(config) -> None:
    source = """# @version 0.3.10
@external
def pos(i: int128) -> int128:
    return (2**127-1) + i

@external
def neg(i: int128) -> int128:
    return i - (2 ** 127 - 1)
"""

    result = apply_rules(source, config())

    assert "return max_value(int128) + i" in result.source
    assert "return i - max_value(int128)" in result.source
    assert any(fix.rule == "VY054" for fix in result.fixes)


def test_dynamic_uint_exponent_rewrites_to_pow_mod256(config) -> None:
    source = """# @version 0.3.10
@external
def f(base: int128, exponent: int128) -> uint256:
    return convert(base, uint256) ** convert(exponent, uint256)
"""

    result = apply_rules(source, config())

    assert "pow_mod256(convert(base, uint256), convert(exponent, uint256))" in result.source
    assert any(fix.rule == "VY055" for fix in result.fixes)


def test_runtime_exponent_uses_folded_integer_constants(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 2

@external
def f(x: uint256) -> uint256:
    return (10**18 * N_COINS**N_COINS) * x
"""

    result = apply_rules(source, config())

    assert "(10**18 * 2**2) * x" in result.source
    assert any(fix.rule == "VY054" for fix in result.fixes)


def test_dynamic_bytes_hex_literal_becomes_byte_string_literal(config) -> None:
    source = """# @version 0.3.10
@external
def f() -> Bytes[256]:
    call_data: Bytes[256] = 0x00
    return call_data
"""

    result = apply_rules(source, config())

    assert 'call_data: Bytes[256] = b"\\x00"' in result.source


def test_fixed_bytes_hex_literal_is_left_alone(config) -> None:
    source = """# @version 0.3.10
value: bytes32 = 0x00
"""

    result = apply_rules(source, config())

    assert "value: bytes32 = 0x00" in result.source


def test_integer_constant_initializer_casts_to_declared_type(config) -> None:
    source = """# pragma version 0.3.10
N_COINS: constant(uint256) = 3
PRICE_SIZE: constant(uint128) = 256 / (N_COINS - 1)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "PRICE_SIZE: constant(uint128) = 128" in result.source
