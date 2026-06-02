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


def test_unsigned_constant_exponent_uses_late_folded_integer_constants(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(uint256) = 3
PRICE_SIZE: constant(int128) = 256 / (N_COINS - 1)
PRICE_MASK: constant(uint256) = 2**PRICE_SIZE - 1
"""

    result = apply_rules(source, config())

    assert "PRICE_SIZE: constant(int128) = 128" in result.source
    assert "PRICE_MASK: constant(uint256) = 2**128 - 1" in result.source
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


def test_signed_boundary_default_literals_rewrite_to_min_max_value(config) -> None:
    source = """# @version 0.3.10
@external
def f(lower: int256=-2**255, upper: int256 = (2 ** 255 - 1)) -> int256:
    return lower + upper
"""

    result = apply_rules(source, config())

    assert "lower: int256=min_value(int256)" in result.source
    assert "upper: int256 = max_value(int256)" in result.source
    assert any(fix.rule == "VY054" for fix in result.fixes)


def test_signed_boundary_assignment_literals_rewrite_to_min_max_value(config) -> None:
    source = """# @version 0.3.10
@external
def f() -> int128:
    lower: int128 = (-2**127)
    upper: int128 = 2 ** 127 - 1
    return lower + upper
"""

    result = apply_rules(source, config())

    assert "lower: int128 = min_value(int128)" in result.source
    assert "upper: int128 = max_value(int128)" in result.source


def test_signed_boundary_return_array_literals_rewrite_to_min_max_value(config) -> None:
    source = """# @version 0.3.10
@internal
def f(value: int256) -> int256[2]:
    if value < 0:
        return [-2**255, -value]
    return [-value, 2**255 - 1]
"""

    result = apply_rules(source, config())

    assert "return [min_value(int256), -value]" in result.source
    assert "return [-value, max_value(int256)]" in result.source


def test_bytes32_convert_exponent_max_literal_is_folded(config) -> None:
    source = """# @version 0.3.7
@external
def f() -> Bytes[64]:
    return concat(
        convert(2 ** 128 - 1, bytes32),
        convert(2 ** 128 - 1, bytes32)
    )
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "convert(340282366920938463463374607431768211455, bytes32)" in result.source
    assert "2 ** 128 - 1" not in result.source
    assert any(fix.rule == "VY054" for fix in result.fixes)


def test_one_base_exponent_literal_folds_to_one(config) -> None:
    source = """# @version 0.2.12
RATE_REDUCTION_COEFFICIENT: constant(uint256) = 135_998_912 * (1 ** 10)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "RATE_REDUCTION_COEFFICIENT: constant(uint256) = 135_998_912 * 1" in result.source
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


def test_dynamic_bytes_hex_external_call_argument_becomes_byte_string_literal(config) -> None:
    source = """# @version 0.3.10
interface Geyser:
    def stakeFor(user: address, amount: uint256, data: Bytes[32]): nonpayable

@external
def f(geyser: address):
    Geyser(geyser).stakeFor(msg.sender, 1, 0x00)
"""

    result = apply_rules(source, config())

    assert 'extcall Geyser(geyser).stakeFor(msg.sender, 1, b"\\x00")' in result.source
    assert any(fix.rule == "VY053" for fix in result.fixes)


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
