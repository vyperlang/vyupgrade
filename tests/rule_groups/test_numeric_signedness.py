from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_signed_constant_converted_in_uint_arithmetic(config) -> None:
    source = """# @version 0.2.8
N_COINS: constant(int128) = 3

@external
def f(fee: uint256) -> uint256:
    adjusted: uint256 = fee * N_COINS / (4 * (N_COINS - 1))
    for i in range(N_COINS):
        pass
    return adjusted
"""

    result = apply_rules(source, config())

    assert (
        "fee * convert(N_COINS, uint256) // (4 * (convert(N_COINS, uint256) - 1))" in result.source
    )
    assert "for i: int128 in range(N_COINS):" in result.source


def test_signed_constant_converted_in_uint_assignment(config) -> None:
    source = """# @version 0.2.8
MAX_COINS: constant(int128) = 8

@external
def f() -> uint256:
    n_coins: uint256 = MAX_COINS
    return n_coins
"""

    result = apply_rules(source, config())

    assert "n_coins: uint256 = convert(MAX_COINS, uint256)" in result.source


def test_signed_constant_does_not_rewrite_struct_literal_key(config) -> None:
    source = """# @version 0.2.8
expanse: public(int128)

struct Lode:
    expanse: int128
    total: uint256

lodes: Lode[2]

@external
def f():
    self.lodes[0] = Lode({total: 1, expanse: 0})
"""

    result = apply_rules(source, config())

    assert "Lode(expanse=convert(0, int128), total=1)" in result.source
    assert "convert(expanse" not in result.source


def test_unsigned_assignment_keeps_unsigned_numerator_when_signed_denominator_converts(
    config,
) -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 3
values: uint256[3]

@external
def f(D: uint256):
    self.values[0] = D / N_COINS
"""

    result = apply_rules(source, config())

    assert "self.values[0] = D // convert(N_COINS, uint256)" in result.source
    assert "convert(D, int128)" not in result.source


def test_signed_loop_variable_converted_in_uint_assignment(config) -> None:
    source = """# @version 0.2.8
MAX_COINS: constant(int128) = 8

@external
def f() -> uint256:
    n_coins: uint256 = convert(MAX_COINS, uint256)
    for i in range(MAX_COINS):
        n_coins = i
    return n_coins
"""

    result = apply_rules(source, config())

    assert "n_coins = convert(i, uint256)" in result.source


def test_signed_loop_index_converted_in_uint_index_arithmetic(config) -> None:
    source = """# @version 0.2.8
MAX_COIN: constant(int128) = 1
BASE_N_COINS: constant(int128) = 3

@external
def f(amounts: uint256[4]) -> uint256[3]:
    base_amounts: uint256[3] = empty(uint256[3])
    for i in range(BASE_N_COINS):
        base_amounts[i] = amounts[i + MAX_COIN]
    return base_amounts
"""

    result = apply_rules(source, config())

    assert (
        "base_amounts[convert(i, uint256)] = "
        "amounts[convert(i, uint256) + convert(MAX_COIN, uint256)]"
    ) in result.source


def test_signed_constant_converted_in_nested_uint_call_argument(config) -> None:
    source = """# @version 0.2.8
interface Pool:
    def calc(i: uint256) -> uint256: view

N_STABLECOINS: constant(int128) = 3

@external
def f(pool: address, i: uint256) -> uint256:
    return Pool(pool).calc(i - (N_STABLECOINS - 1))
"""

    result = apply_rules(source, config())

    assert "staticcall Pool(pool).calc(i - (convert(N_STABLECOINS, uint256) - 1))" in result.source


def test_signed_time_constant_converted_in_uint_comparison(config) -> None:
    source = """# @version 0.2.8
BASE_CACHE_EXPIRES: constant(int128) = 600
base_cache_updated: public(uint256)

@external
def f() -> bool:
    return block.timestamp > self.base_cache_updated + BASE_CACHE_EXPIRES
"""

    result = apply_rules(source, config())

    assert "self.base_cache_updated + convert(BASE_CACHE_EXPIRES, uint256)" in result.source


def test_max_value_comparison_casts_to_unsigned_peer_type(config) -> None:
    source = """# @version 0.3.10
@external
def f(tokens: DynArray[address, max_value(uint8)]) -> bool:
    return len(tokens) == max_value(uint8)
"""

    result = apply_rules(source, config())

    assert "len(tokens) == convert(max_value(uint8), uint256)" in result.source


def test_signed_max_value_comparison_casts_to_unsigned_peer_type(config) -> None:
    source = """# @version 0.2.16
totalSupply: uint256

@external
def rebase():
    if self.totalSupply > max_value(int128):
        self.totalSupply = max_value(int128)
"""

    result = apply_rules(source, config())

    assert "self.totalSupply > convert(max_value(int128), uint256)" in result.source
    assert "self.totalSupply = convert(max_value(int128), uint256)" in result.source


def test_narrow_unsigned_comparison_arithmetic_casts_to_peer_type(config) -> None:
    source = """# @version 0.3.10
interface ERC20:
    def balanceOf(owner: address) -> uint256: view

token: ERC20
amount_per_draw: constant(uint8) = 10
ultra_rare_bonus: constant(uint8) = 10

@external
@view
def f() -> bool:
    return self.token.balanceOf(self) >= amount_per_draw * ultra_rare_bonus * 10
"""

    result = apply_rules(source, config())

    assert (
        "staticcall self.token.balanceOf(self) >= "
        "convert(amount_per_draw, uint256) * convert(ultra_rare_bonus, uint256) * 10"
    ) in result.source


def test_signed_constant_converted_in_unsigned_index_comparison(config) -> None:
    source = """# @version 0.2.8
PRECISION: constant(int128) = 10 ** 18

@external
def f(rate_multipliers: uint256[4]):
    for i in range(4):
        assert rate_multipliers[i] == PRECISION
"""

    result = apply_rules(source, config())

    assert "assert rate_multipliers[i] == convert(PRECISION, uint256)" in result.source


def test_signed_constant_not_converted_in_signed_loop_comparison(config) -> None:
    source = """# @version 0.2.8
N_COINS: constant(int128) = 2

@internal
def f() -> uint256:
    total: uint256 = 0
    for i in range(N_COINS):
        if i == N_COINS:
            break
        total += convert(i, uint256)
    for i in range(10):
        total += i
    return total
"""

    result = apply_rules(source, config())

    assert "if i == N_COINS:" in result.source
    assert "if i == convert(N_COINS, uint256):" not in result.source


def test_signed_loop_variable_not_converted_against_signed_cast(config) -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@internal
def f(n_coins: uint256) -> int128:
    for k in range(MAX_COINS):
        if k >= convert(n_coins, int128):
            return k
    return 0
"""

    result = apply_rules(source, config())

    assert "if k >= convert(n_coins, int128):" in result.source
    assert "if convert(k, uint256) >= convert(n_coins, int128):" not in result.source


def test_unsigned_loop_variable_converted_against_signed_cast(config) -> None:
    source = """# @version 0.3.10
BASE_N_COINS: constant(int128) = 2

@internal
def f() -> uint256:
    total: uint256 = 0
    for i in range(4):
        if i < convert(BASE_N_COINS, int128):
            total += i
    return total
"""

    result = apply_rules(source, config())

    assert "if convert(i, int128) < BASE_N_COINS:" in result.source


def test_unsigned_loop_variable_converted_in_elif_signed_comparison(config) -> None:
    source = """# @version 0.2.16
@external
def f(j: int128, xp: uint256[3]) -> uint256:
    total: uint256 = 0
    for _i in range(3):
        if _i == 0:
            total += 1
        elif _i != j:
            total += xp[_i]
    return total
"""

    result = apply_rules(source, config())

    assert "elif convert(_i, int128) != j:" in result.source
    assert "total += xp[_i]" in result.source


def test_comment_identifier_does_not_create_unsigned_context(config) -> None:
    source = """# @version 0.3.7
rate: uint256
sigma: int256

@external
def f(p: int256) -> int256:
    power: int256 = (10**18 - p) * 10**18 / sigma  # low rate
    return power
"""

    result = apply_rules(source, config())

    assert "// sigma  # low rate" in result.source
    assert "convert(sigma, uint256)" not in result.source


def test_unsigned_constant_folds_in_signed_integer_division(config) -> None:
    source = """# @version 0.2.4
MAXTIME: constant(uint256) = 4 * 365 * 86400

struct LockedBalance:
    amount: int128

struct Point:
    slope: int128

@external
def f(old_locked: LockedBalance):
    u_old: Point = empty(Point)
    u_old.slope = old_locked.amount / MAXTIME
"""

    result = apply_rules(source, config())

    assert "u_old.slope = old_locked.amount // 126144000" in result.source
    assert "convert(MAXTIME, int128)" not in result.source


def test_unsigned_constant_converted_in_signed_param_comparison(config) -> None:
    source = """# @version 0.2.8
MAX_PCT: constant(uint256) = 10_000

@external
def f(percentage: int256):
    assert percentage <= MAX_PCT
"""

    result = apply_rules(source, config())

    assert "assert percentage <= convert(MAX_PCT, int256)" in result.source


def test_event_field_name_does_not_force_signed_param_to_uint(config) -> None:
    source = """# @version 0.2.8
event DelegateBoost:
    _expire_time: uint256

MIN_DELEGATION_TIME: constant(uint256) = 86400

@external
def f(_expire_time: int256) -> bool:
    time: int256 = convert(block.timestamp, int256)
    return _expire_time > time + MIN_DELEGATION_TIME
"""

    result = apply_rules(source, config())

    assert "convert(_expire_time, uint256)" not in result.source
    assert "time + convert(MIN_DELEGATION_TIME, int256)" in result.source


def test_signed_lhs_keeps_signed_operand_in_mixed_arithmetic(config) -> None:
    source = """# @version 0.3.10
@external
def f(n1: int256, n: uint256) -> int256:
    n2: int256 = n1 + convert(n - 1, int256)
    return n2
"""

    result = apply_rules(source, config())

    assert "n2: int256 = n1 + convert(n - 1, int256)" in result.source
    assert "convert(n1, uint256)" not in result.source


def test_unsigned_constant_inside_final_signed_convert_stays_unsigned(config) -> None:
    source = """# @version 0.3.10
COLLATERAL_PRECISION: immutable(uint256)

@external
def __init__():
    COLLATERAL_PRECISION = 10**18

@external
def f(p: uint256, debt: uint256) -> int256:
    health: int256 = 0
    health += convert(p * COLLATERAL_PRECISION // debt, int256)
    return health
"""

    result = apply_rules(source, config())

    assert "p * COLLATERAL_PRECISION // debt" in result.source
    assert "convert(COLLATERAL_PRECISION, int256)" not in result.source


def test_unsigned_expression_nested_in_uint_internal_arg_stays_unsigned(config) -> None:
    source = """# @version 0.3.10
LOGN_A_RATIO: immutable(int256)

@deploy
def __init__():
    A: uint256 = 100
    LOGN_A_RATIO = self.wad_ln(unsafe_div(A * 10**18, unsafe_sub(A, 1)))

@internal
@pure
def wad_ln(x: uint256) -> int256:
    return convert(x, int256)
"""

    result = apply_rules(source, config())

    assert "LOGN_A_RATIO = self.wad_ln(unsafe_div(A * 10**18, unsafe_sub(A, 1)))" in result.source
    assert "convert(A, int256)" not in result.source


def test_unsigned_array_literal_expression_keeps_unsigned_constants(config) -> None:
    source = """# @version 0.2.16
N_COINS: constant(int128) = 2
PRECISION: constant(uint256) = 10**18

@external
def f(d: uint256, price_scale: uint256) -> uint256[N_COINS]:
    xp: uint256[N_COINS] = [d / N_COINS, d * PRECISION / (N_COINS * price_scale)]
    return xp
"""

    result = apply_rules(source, config())

    assert "d * PRECISION // (convert(N_COINS, uint256) * price_scale)" in result.source
    assert "convert(PRECISION, int128)" not in result.source
    assert "convert(price_scale, int128)" not in result.source


def test_signed_array_constant_assigned_to_unsigned_array_becomes_unsigned(config) -> None:
    source = """# @version 0.2.16
N_COINS: constant(int128) = 2
PRECISION_MUL: constant(int128[N_COINS]) = [1, 1]

@external
def f() -> uint256[N_COINS]:
    result: uint256[N_COINS] = PRECISION_MUL
    return result
"""

    result = apply_rules(source, config())

    assert "PRECISION_MUL: constant(uint256[N_COINS]) = [1, 1]" in result.source


def test_signed_array_constant_kept_when_used_as_signed_array(config) -> None:
    source = """# @version 0.2.16
N_COINS: constant(int128) = 2
PRECISION_MUL: constant(int128[N_COINS]) = [1, 1]

@external
def f() -> int128[N_COINS]:
    result: int128[N_COINS] = PRECISION_MUL
    return result
"""

    result = apply_rules(source, config())

    assert "PRECISION_MUL: constant(int128[N_COINS]) = [1, 1]" in result.source


def test_boolean_comparison_prefers_casting_unsigned_constant_peer(config) -> None:
    source = """# @version 0.2.16
N_COINS: constant(int128) = 2
ETH_INDEX: constant(uint256) = 0

@external
def f(use_eth: bool) -> bool:
    for i in range(N_COINS):
        if use_eth and i == ETH_INDEX:
            return True
    return False
"""

    result = apply_rules(source, config())

    assert "if use_eth and i == convert(ETH_INDEX, int128):" in result.source
    assert "convert(i, uint256) == convert(ETH_INDEX, int128)" not in result.source


def test_array_literal_elements_cast_to_exact_integer_type(config) -> None:
    source = """# @version 0.3.3
@external
def f(amount: uint256) -> DynArray[int256, 3]:
    limits: DynArray[int256, 3] = [
        convert(amount, int256),
        MAX_INT128,
        MAX_INT128,
    ]
    return limits
"""

    result = apply_rules(source, config())

    assert "convert(max_value(int128), int256)" in result.source


def test_unsigned_range_loop_converted_in_signed_comparison(config) -> None:
    source = """# @version 0.2.4
n_gauge_types: int128
points_sum: HashMap[int128, uint256]

@internal
def _get_sum(gauge_type: int128) -> uint256:
    return 0

@internal
def f():
    _n_gauge_types: int128 = self.n_gauge_types
    for gauge_type in range(100):
        if gauge_type == _n_gauge_types:
            break
        self._get_sum(gauge_type)
        value: uint256 = self.points_sum[gauge_type]
"""

    result = apply_rules(source, config())

    assert "for gauge_type: uint256 in range(100):" in result.source
    assert "if convert(gauge_type, int128) == _n_gauge_types:" in result.source
    assert "self._get_sum(convert(gauge_type, int128))" in result.source
    assert "self.points_sum[convert(gauge_type, int128)]" in result.source


def test_signed_parameter_not_converted_in_uint_arithmetic(config) -> None:
    source = """# @version 0.3.7
@internal
def g(i: int128) -> int128:
    return i

@external
def f(i: int128, x: uint256) -> uint256:
    y: uint256 = x + i
    return y
"""

    result = apply_rules(source, config())

    assert "x + i" in result.source


def test_signed_attribute_fields_are_not_rewritten_as_methods(config) -> None:
    source = """# @version 0.3.10
struct Trade:
    n2: int256

bands_x: HashMap[int256, uint256]
active_band: int256

@internal
@view
def _p_oracle_up(n: int256) -> uint256:
    return 1

@external
def f() -> uint256:
    out: Trade = empty(Trade)
    out.n2 = self.active_band
    p: uint256 = self._p_oracle_up(out.n2)
    x: uint256 = self.bands_x[out.n2]
    y: uint256 = self.bands_x[self.active_band]
    return p + x + y
"""

    result = apply_rules(source, config())

    assert "out.convert" not in result.source
    assert "self.convert" not in result.source
    assert "self._p_oracle_up(out.n2)" in result.source
    assert "self.bands_x[out.n2]" in result.source
    assert "self.bands_x[self.active_band]" in result.source


def test_signed_internal_call_argument_not_converted_for_uint_assignment(config) -> None:
    source = """# @version 0.2.4
points_sum: HashMap[int128, uint256]

@internal
def _get_sum(gauge_type: int128) -> uint256:
    return 0

@external
def f():
    gauge_type: int128 = 0
    old_sum_bias: uint256 = self._get_sum(gauge_type)
    old_sum_slope: uint256 = self.points_sum[gauge_type]
"""

    result = apply_rules(source, config())

    assert "self._get_sum(gauge_type)" in result.source
    assert "self.points_sum[gauge_type]" in result.source
    assert "convert(gauge_type, uint256)" not in result.source


def test_signed_constant_not_converted_when_external_param_is_signed(config) -> None:
    source = """# @version 0.2.16
interface CurveMeta:
    def calc_withdraw_one_coin(_token_amount: uint256, i: int128) -> uint256: view

MAX_COIN: constant(int128) = 2

@external
def f(pool: address, amount: uint256) -> uint256:
    return CurveMeta(pool).calc_withdraw_one_coin(amount, MAX_COIN)
"""

    result = apply_rules(source, config())

    assert "staticcall CurveMeta(pool).calc_withdraw_one_coin(amount, MAX_COIN)" in result.source
    assert "convert(MAX_COIN, uint256)" not in result.source


def test_external_call_argument_casts_to_interface_param_type(config) -> None:
    source = """# @version 0.2.16
interface CurveBase:
    def calc_withdraw_one_coin(_token_amount: uint256, i: int128) -> uint256: view

@external
def f(pool: address, amount: uint256, i: uint256) -> uint256:
    return CurveBase(pool).calc_withdraw_one_coin(amount, i)
"""

    result = apply_rules(source, config())

    assert (
        "staticcall CurveBase(pool).calc_withdraw_one_coin(amount, convert(i, int128))"
        in result.source
    )


def test_signed_cast_argument_preserves_uint_arithmetic_inside_convert(config) -> None:
    source = """# @version 0.2.16
interface StableSwap:
    def calc_withdraw_one_coin(_token_amount: uint256, i: int128) -> uint256: view

N_COINS: constant(int128) = 2

@external
def f(pool: address, amount: uint256, i: uint256) -> uint256:
    return StableSwap(pool).calc_withdraw_one_coin(amount, convert(i - (N_COINS - 1), int128))
"""

    result = apply_rules(source, config())

    assert (
        "staticcall StableSwap(pool).calc_withdraw_one_coin("
        "amount, convert(i - (convert(N_COINS, uint256) - 1), int128)"
        ")"
    ) in result.source


def test_signed_cast_removed_from_unsigned_subscript_arithmetic(config) -> None:
    source = """# @version 0.2.16
MAX_COIN: constant(uint256) = 1
BASE_N_COINS: constant(uint256) = 2

@external
def f(i: int128) -> address:
    base_coins: address[BASE_N_COINS] = empty(address[BASE_N_COINS])
    return base_coins[i - convert(MAX_COIN, int128)]
"""

    result = apply_rules(source, config())

    assert "base_coins[convert(i, uint256) - MAX_COIN]" in result.source


def test_signed_literal_cast_removed_from_unsigned_subscript_arithmetic(config) -> None:
    source = """# @version 0.2.16
MAX_DISCOUNTS: constant(int8) = 10

struct Discount:
    duration: uint256

discounts: Discount[MAX_DISCOUNTS]

@external
def f():
    for i in range(MAX_DISCOUNTS):
        self.discounts[i] = self.discounts[i - convert(1, int8)]
"""

    result = apply_rules(source, config())

    assert "self.discounts[convert(i, uint256) - 1]" in result.source


def test_unsigned_expression_cast_removed_from_unsigned_subscript_arithmetic(config) -> None:
    source = """# @version 0.3.10
META_N_COINS: constant(uint256) = 2
MAX_COINS: constant(uint256) = 8

@external
def f(i: int128) -> address:
    base_coins: DynArray[address, MAX_COINS] = empty(DynArray[address, MAX_COINS])
    return base_coins[i - convert(META_N_COINS - 1, int128)]
"""

    result = apply_rules(source, config())

    assert "base_coins[convert(i, uint256) - (META_N_COINS - 1)]" in result.source


def test_signed_constant_casted_in_unsigned_subscript_arithmetic(config) -> None:
    source = """# @version 0.2.16
N_COINS: constant(int128) = 2
N_STABLECOINS: constant(int128) = 3

@external
def f(i: uint256, dx: uint256) -> uint256[N_STABLECOINS]:
    amounts: uint256[N_STABLECOINS] = empty(uint256[N_STABLECOINS])
    amounts[i - (N_COINS - 1)] = dx
    return amounts
"""

    result = apply_rules(source, config())

    assert "amounts[i - (convert(N_COINS, uint256) - 1)] = dx" in result.source


def test_unsigned_local_converted_inside_signed_external_arg_expression(config) -> None:
    source = """# @version 0.2.16
interface CurvePool:
    def balances(i: int128) -> uint256: view

MAX_COINS: constant(int128) = 8

@external
def f(pool: address, base_coin_idx: uint256) -> uint256:
    total: uint256 = 0
    for i in range(MAX_COINS):
        total += CurvePool(pool).balances(convert(i - base_coin_idx, int128))
    return total
"""

    result = apply_rules(source, config())

    assert (
        "staticcall CurvePool(pool).balances((i - convert(base_coin_idx, int128)))"
        in result.source
    )


def test_unsigned_constant_converted_in_signed_external_arg_expression(config) -> None:
    source = """# @version 0.3.10
interface StableSwap:
    def calc_withdraw_one_coin(token_amount: uint256, i: int128) -> uint256: view

META_N_COINS: constant(uint256) = 2

@external
def f(pool: address, amount: uint256) -> uint256:
    return StableSwap(pool).calc_withdraw_one_coin(amount, META_N_COINS - 1)
"""

    result = apply_rules(source, config())

    assert (
        "staticcall StableSwap(pool).calc_withdraw_one_coin("
        "amount, convert(META_N_COINS, int128) - 1)"
    ) in result.source


def test_unsafe_mul_literal_casts_to_enclosing_signed_convert(config) -> None:
    source = """# @version 0.3.10
@external
def f() -> int256:
    return convert(unsafe_mul(10**32, 3), int256)
"""

    result = apply_rules(source, config())

    assert "return unsafe_mul(10**32, convert(3, int256))" in result.source


def test_unsigned_unsafe_shift_return_casts_to_signed_return_type(config) -> None:
    source = """# @version 0.3.10
@external
@pure
def wad_exp(r: int256, k: int256) -> int256:
    return unsafe_mul(convert(convert(r, bytes32), uint256), 3_822_833_074_963_236_453_042_738_258_902_158_003_155_416_615_667) >>\\
           convert(unsafe_sub(convert(195, int256), k), uint256)
"""

    result = apply_rules(source, config())

    assert (
        "return convert(unsafe_mul(convert(convert(r, bytes32), uint256), "
        "convert(3_822_833_074_963_236_453_042_738_258_902_158_003_155_416_615_667, uint256)) >>\\\n"
        "           convert(unsafe_sub(convert(195, int256), k), uint256), int256)"
    ) in result.source


def test_signed_loop_index_casted_in_unsigned_subscript_arithmetic(config) -> None:
    source = """# @version 0.2.16
N_COINS: constant(int128) = 2

@external
def f(dx: uint256) -> uint256[N_COINS]:
    amounts: uint256[N_COINS] = empty(uint256[N_COINS])
    for i in range(N_COINS):
        amounts[i - (N_COINS - 1)] = dx
    return amounts
"""

    result = apply_rules(source, config())

    assert "amounts[convert(i, uint256) - (convert(N_COINS, uint256) - 1)] = dx" in result.source


def test_signed_assignment_after_subscript_is_not_treated_as_index_context(config) -> None:
    source = """# @version 0.2.16
gauge_types_: HashMap[address, int128]

@external
def f(addr: address, gauge_type: int128):
    self.gauge_types_[addr] = gauge_type + 1
"""

    result = apply_rules(source, config())

    assert "self.gauge_types_[addr] = gauge_type + 1" in result.source
    assert "convert(gauge_type, uint256)" not in result.source


def test_signed_negation_assigned_to_uint_is_converted(config) -> None:
    source = """# @version 0.2.16
@external
def f(position: int256) -> uint256:
    if position < 0:
        _pos: uint256 = (-position)
        return _pos
    return 0
"""

    result = apply_rules(source, config())

    assert "_pos: uint256 = convert(-position, uint256)" in result.source


def test_external_call_arg_uses_nearest_loop_type_for_reused_name(config) -> None:
    source = """# @version 0.2.12
interface Curve:
    def balances(i: uint256) -> uint256: view
    def price_scale(i: uint256) -> uint256: view

N_COINS: constant(int128) = 3

@external
def f(pool: address) -> uint256:
    total: uint256 = 0
    for k in range(N_COINS):
        total += Curve(pool).balances(k)
    for k in range(N_COINS - 1):
        total += Curve(pool).price_scale(k)
    return total
"""

    result = apply_rules(source, config())

    assert "for k: int128 in range(N_COINS):" in result.source
    assert "staticcall Curve(pool).balances(convert(k, uint256))" in result.source
    assert "for k: uint256 in range(convert(N_COINS, uint256) - 1, bound=2):" in result.source
    assert "staticcall Curve(pool).price_scale(k)" in result.source


def test_signed_loop_declaration_is_not_cast_when_name_reused_by_later_uint_loop(config) -> None:
    source = """# @version 0.2.12
interface Curve:
    def balances(i: uint256) -> uint256: view
    def price_scale(i: uint256) -> uint256: view

N_COINS: constant(int128) = 3
PRECISION: constant(uint256) = 10 ** 18

@external
def f(amounts: uint256[N_COINS], deposit: bool):
    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    for k in range(N_COINS):
        xp[k] = Curve(msg.sender).balances(k)
    if deposit:
        for k in range(N_COINS):
            xp[k] += amounts[k]
    else:
        for k in range(N_COINS):
            xp[k] -= amounts[k]
    for k in range(N_COINS-1):
        p: uint256 = Curve(msg.sender).price_scale(k)
        xp[k+1] = xp[k+1] * p / PRECISION
"""

    result = apply_rules(source, config())

    assert "for convert(k, uint256):" not in result.source
    assert result.source.count("for k: int128 in range(N_COINS):") == 3
    assert "xp[convert(k, uint256)] += amounts[convert(k, uint256)]" in result.source
    assert "for k: uint256 in range(convert(N_COINS, uint256)-1, bound=2):" in result.source


def test_unsafe_sub_array_index_operands_cast_to_unsigned_context(config) -> None:
    source = """# @version 0.3.10
MAX_TICKS: constant(int256) = 5
MAX_TICKS_UINT: constant(uint256) = 5

struct Swap:
    ticks_in: DynArray[uint256, MAX_TICKS_UINT]

@external
def f(n_diff: int256) -> uint256:
    out: Swap = empty(Swap)
    total: uint256 = 0
    for k in range(MAX_TICKS):
        total += out.ticks_in[unsafe_sub(n_diff, k)]
    return total
"""

    result = apply_rules(source, config())

    assert "out.ticks_in[unsafe_sub(convert(n_diff, uint256), convert(k, uint256))]" in result.source


def test_signed_loop_type_is_not_overwritten_by_later_loop_same_name(config) -> None:
    source = """# @version 0.2.4
N_COINS: constant(int128) = 2

@external
def f(i: int128):
    for _i in range(N_COINS):
        if _i != i:
            pass
    for _i in range(2):
        pass
"""

    result = apply_rules(source, config())

    assert "for _i: int128 in range(N_COINS):\n        if _i != i:" in result.source
    assert "convert(_i, int128)" not in result.source


def test_signed_hashmap_key_is_not_rewritten_to_uint_index(config) -> None:
    source = """# pragma version 0.3.10
values: HashMap[int128, uint256]

@external
def get(period: int128) -> uint256:
    return self.values[period]
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "self.values[period]" in result.source
    assert "convert(period, uint256)" not in result.source


def test_signed_storage_hashmap_key_ignores_shadowing_local_name(config) -> None:
    source = """# pragma version 0.3.10
integrate_inv_supply: HashMap[int128, uint256]

@external
def get(period: int128) -> uint256:
    integrate_inv_supply: uint256 = self.integrate_inv_supply[period]
    return integrate_inv_supply
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "self.integrate_inv_supply[period]" in result.source
    assert "convert(period, uint256)" not in result.source


def test_unsigned_loop_assignment_to_signed_local_is_converted(config) -> None:
    source = """# pragma version 0.3.10
@internal
def f() -> int256:
    ret: int256 = -1
    for i in range(8):
        ret = i
    return ret
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "for i: uint256 in range(8):" in result.source
    assert "ret = convert(i, int256)" in result.source


def test_unsigned_loop_augmented_division_in_signed_local_is_converted(config) -> None:
    source = """# pragma version 0.3.10
@internal
def f(x: int256) -> int256:
    term: int256 = x
    for i in range(1, 50):
        term //= (i + 1)
    return term
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "for i: uint256 in range(1, 50):" in result.source
    assert "term //= (convert(i, int256) + 1)" in result.source
