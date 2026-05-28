from __future__ import annotations

from pathlib import Path

from vyupgrade.models import Config
from vyupgrade.rules import apply_rules


def config(**kwargs) -> Config:
    values = {"paths": (Path("contracts"),)}
    values.update(kwargs)
    return Config(**values)


def test_mechanical_rewrites_skip_comments_and_strings() -> None:
    source = '''# @version ^0.3.10

@external
def __init__():
    x: Bytes[32] = _abi_encode(1)
    y: String[64] = "_abi_decode should stay"
    # _abi_encode should stay
'''

    result = apply_rules(source, config())

    assert "#pragma version ^0.3.10" in result.source
    assert "@deploy\ndef __init__" in result.source
    assert "@external\ndef __init__" not in result.source
    assert "abi_encode(1)" in result.source
    assert '"_abi_decode should stay"' in result.source
    assert "# _abi_encode should stay" in result.source


def test_type_aware_rewrites() -> None:
    source = '''# @version 0.3.10

from vyper.interfaces import ERC20

interface Strategy:
    def totalAssets() -> uint256: view
    def withdraw(amount: uint256) -> uint256: nonpayable

struct Position:
    shares: uint256
    assets: uint256

token: public(ERC20)

@external
def f(strategy: Strategy, amount: uint256, price: uint256):
    shares: uint256 = amount / price
    p: Position = Position({shares: shares, assets: amount})
    b: uint256 = self.token.balanceOf(msg.sender)
    extcall strategy.withdraw(amount)
    t: uint256 = strategy.totalAssets()
    for i in range(10):
        pass
'''

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC20" in result.source
    assert "token: public(IERC20)" in result.source
    assert "amount // price" in result.source
    assert "Position(shares=shares, assets=amount)" in result.source
    assert "staticcall self.token.balanceOf(msg.sender)" in result.source
    assert "extcall strategy.withdraw(amount)" in result.source
    assert "staticcall strategy.totalAssets()" in result.source
    assert "for i: uint256 in range(10):" in result.source
    assert not [diag for diag in result.diagnostics if diag.rule == "VYD003"]


def test_array_loop_type_inference() -> None:
    source = """# @version 0.3.10
@external
def f(items: DynArray[address, 10]):
    for item in items:
        pass
"""

    result = apply_rules(source, config())

    assert "for item: address in items:" in result.source


def test_casted_interface_calls_and_assigned_integer_division() -> None:
    source = """# @version 0.3.10
interface Token:
    def balanceOf(owner: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable

@external
def f(token: address, amount: uint256):
    shares: uint256 = 0
    shares = amount / 2
    b: uint256 = Token(token).balanceOf(msg.sender)
    ok: bool = Token(token).transfer(msg.sender, amount)
"""

    result = apply_rules(source, config())

    assert "shares = amount // 2" in result.source
    assert "staticcall Token(token).balanceOf(msg.sender)" in result.source
    assert "extcall Token(token).transfer(msg.sender, amount)" in result.source


def test_integer_expression_division() -> None:
    source = """# @version 0.3.10
MAX_BPS: constant(uint256) = 10_000

@external
def f(total_fees: uint256, protocol_fee_bps: uint16) -> uint256:
    return total_fees * convert(protocol_fee_bps, uint256) / MAX_BPS
"""

    result = apply_rules(source, config())

    assert "convert(protocol_fee_bps, uint256) // MAX_BPS" in result.source


def test_legacy_numeric_constants() -> None:
    source = """# @version 0.3.3
@external
def f(amount: uint256 = MAX_UINT256) -> bool:
    return amount == MAX_UINT256
"""

    result = apply_rules(source, config())

    assert "amount: uint256 = max_value(uint256)" in result.source
    assert "amount == max_value(uint256)" in result.source
    assert "ZERO_ADDRESS" not in apply_rules("# @version 0.3.3\nx: address = ZERO_ADDRESS\n", config()).source


def test_redundant_convert_after_integer_division() -> None:
    source = """# @version 0.3.3
COEFF: constant(uint256) = 10 ** 18
@external
def f() -> uint256:
    return convert(COEFF * 46 / 10 ** 6, uint256)
"""

    result = apply_rules(source, config())

    assert "convert(" not in result.source
    assert "(COEFF * 46 // 10 ** 6)" in result.source


def test_self_storage_interface_not_shadowed_by_later_parameter() -> None:
    source = """# @version 0.3.10
interface Token:
    def balanceOf(owner: address) -> uint256: view

token: public(Token)

@external
def earlier(token: address):
    pass

@external
def later():
    x: uint256 = self.token.balanceOf(self)
"""

    result = apply_rules(source, config())

    assert "staticcall self.token.balanceOf(self)" in result.source


def test_multiline_integer_division() -> None:
    source = """# @version 0.3.10
totalSupply: public(uint256)

@internal
def f(shares: uint256) -> uint256:
    return (
        shares
        * self.totalSupply
        / self.totalSupply
    )
"""

    result = apply_rules(source, config())

    assert "// self.totalSupply" in result.source


def test_diagnostics_for_ambiguous_cases() -> None:
    source = '''# @version 0.3.10

@nonreentrant("a")
@external
def f(target: address, amount: uint256, scale: decimal):
    x: decimal = scale / 2.0
    target.foo()
    create_from_blueprint(target)

@nonreentrant("b")
@external
def g():
    pass
'''

    result = apply_rules(source, config())
    rules = {diag.rule for diag in result.diagnostics}

    assert "VYD001" in rules
    assert "VYD002" in rules
    assert "VYD003" in rules
    assert "VYD004" in rules
    assert "VY080" in rules


def test_idempotent() -> None:
    source = '''# @version 0.3.10
@external
def __init__():
    pass
'''
    once = apply_rules(source, config()).source
    twice = apply_rules(once, config()).source
    assert once == twice


def test_pr_3777_struct_dict_instantiation_to_kwargs() -> None:
    source = """# @version 0.3.10
struct Point:
    x: uint256
    y: uint256

@external
def f():
    p: Point = Point({x: 1, y: 2})
"""

    result = apply_rules(source, config())

    assert "Point(x=1, y=2)" in result.source


def test_pr_3697_enum_to_flag_is_review_by_default_and_aggressive_fix() -> None:
    source = """# @version 0.3.10
enum Roles:
    ADMIN
    KEEPER
"""

    default = apply_rules(source, config())
    aggressive = apply_rules(source, config(aggressive=True))

    assert "enum Roles:" in default.source
    assert any(diag.rule == "VY030" for diag in default.diagnostics)
    assert "flag Roles:" in aggressive.source


def test_pr_3729_constructor_deploy_replaces_external() -> None:
    source = """# @version 0.3.10
@external
def __init__():
    pass
"""

    result = apply_rules(source, config())

    assert "@external\ndef __init__" not in result.source
    assert "@deploy\ndef __init__" in result.source


def test_pr_2938_extcall_and_staticcall_keywords() -> None:
    source = """# @version 0.3.10
interface Token:
    def balanceOf(owner: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable

@external
def f(token: Token):
    balance: uint256 = token.balanceOf(msg.sender)
    sent: bool = token.transfer(msg.sender, balance)
"""

    result = apply_rules(source, config())

    assert "staticcall token.balanceOf(msg.sender)" in result.source
    assert "extcall token.transfer(msg.sender, balance)" in result.source


def test_pr_3596_loop_variable_type_annotation_for_range_and_arrays() -> None:
    source = """# @version 0.3.10
@external
def f(items: DynArray[address, 10]):
    for i in range(3):
        pass
    for item in items:
        pass
"""

    result = apply_rules(source, config())

    assert "for i: uint256 in range(3):" in result.source
    assert "for item: address in items:" in result.source


def test_pr_3769_single_named_reentrancy_lock_is_rewritten() -> None:
    source = """# @version 0.3.10
@nonreentrant("lock")
@external
def f():
    pass
"""

    result = apply_rules(source, config())

    assert '@nonreentrant("lock")' not in result.source
    assert "@nonreentrant\n@external" in result.source


def test_pr_2937_integer_division_to_floordiv_and_decimal_diagnostic() -> None:
    source = """# @version 0.3.10
@external
def f(amount: uint256, scale: decimal):
    shares: uint256 = amount / 2
    ratio: decimal = scale / 2.0
"""

    result = apply_rules(source, config())

    assert "amount // 2" in result.source
    assert "scale / 2.0" in result.source
    assert any(diag.rule == "VYD001" for diag in result.diagnostics)


def test_pr_3679_range_runtime_stop_gets_bound_keyword() -> None:
    source = """# @version 0.3.10
@external
def f(start: uint256):
    for i in range(start, start + 101):
        pass
"""

    result = apply_rules(source, config())

    assert "range(start, start + 101, bound=101)" in result.source
    assert "for i: uint256 in range" in result.source


def test_pr_3679_ambiguous_range_bound_is_diagnostic_only() -> None:
    source = """# @version 0.3.10
@external
def f(start: uint256, stop: uint256):
    for i in range(start, stop):
        pass
"""

    result = apply_rules(source, config())

    assert "range(start, stop, bound=" not in result.source
    assert any(diag.rule == "VYD011" for diag in result.diagnostics)


def test_pr_3679_literal_range_bounds_are_left_alone() -> None:
    source = """# @version 0.3.10
@external
def f():
    for i in range(1, 4):
        pass
"""

    result = apply_rules(source, config())

    assert "range(1, 4, bound=" not in result.source
    assert not [diag for diag in result.diagnostics if diag.rule == "VYD011"]


def test_target_before_change_skips_later_patch_rewrites() -> None:
    source = """# @version 0.4.1
@external
def f(a: uint256, b: uint256) -> uint256:
    return sqrt(bitwise_and(a, b))
"""

    result = apply_rules(source, config(target_version="0.4.1"))

    assert "math.sqrt" not in result.source
    assert "bitwise_and" in result.source
    assert not [fix for fix in result.fixes if fix.rule in {"VY100", "VY110"}]


def test_patch_rewrites_apply_when_crossing_0_4_2() -> None:
    source = """# @version 0.4.1
@external
def f(a: uint256, b: uint256) -> uint256:
    return sqrt(bitwise_and(a, b))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "import math" in result.source
    assert "math.sqrt((a & b))" in result.source
    assert {fix.rule for fix in result.fixes} >= {"VY100", "VY110"}


def test_source_at_change_skips_already_current_patch_rewrites() -> None:
    source = """# @version 0.4.2
@external
def f(a: uint256, b: uint256) -> uint256:
    return sqrt(bitwise_and(a, b))
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "math.sqrt" not in result.source
    assert "bitwise_and" in result.source
    assert not [fix for fix in result.fixes if fix.rule in {"VY100", "VY110"}]


def test_pragma_rewrite_is_gated_by_target_version() -> None:
    source = """# @version 0.3.8
@external
def f():
    pass
"""

    before = apply_rules(source, config(target_version="0.3.9"))
    after = apply_rules(source, config(target_version="0.3.10"))

    assert "# @version 0.3.8" in before.source
    assert "#pragma version 0.3.8" in after.source


def test_0_4_rules_apply_from_0_2_1_source() -> None:
    source = """# @version 0.2.1
@external
def __init__():
    pass
"""

    result = apply_rules(source, config())

    assert "@deploy\ndef __init__" in result.source
    assert any(fix.rule == "VY002" for fix in result.fixes)


def test_legacy_0_2_1_syntax_rewrites_are_granular() -> None:
    source = """# @version 0.2.1
Transfer: event({_from: indexed(address), _to: indexed(address), _value: uint256})

contract Token:
    def transfer(to: address, amount: uint256) -> bool: modifying

balances: map(address, uint256)
payload: bytes[100]
name: string[32]

@constant
@public
def f(token: Token, amount: uint256(wei), data: bytes[32]) -> uint256:
    log.Transfer(msg.sender, self, amount)
    assert_modifiable(token.transfer(msg.sender, amount))
    raw_call(msg.sender, data, outsize=32)
    return extract32(data, 0, type=uint256)

@private
def _g():
    pass
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "event Transfer:\n    _from: indexed(address)" in result.source
    assert "interface Token:" in result.source
    assert "balances: HashMap[address, uint256]" in result.source
    assert "payload: Bytes[100]" in result.source
    assert "name: String[32]" in result.source
    assert "@view\n@external" in result.source
    assert "amount: uint256," in result.source
    assert "data: Bytes[32]" in result.source
    assert "log Transfer(msg.sender, self, amount)" in result.source
    assert "assert token.transfer(msg.sender, amount)" in result.source
    assert "max_outsize=32" in result.source
    assert "output_type=uint256" in result.source
    assert "@internal\ndef _g" in result.source
    assert {fix.rule for fix in result.fixes} >= {
        "VY201",
        "VY202",
        "VY203",
        "VY204",
        "VY205",
        "VY206",
        "VY207",
        "VY208",
    }


def test_shift_builtin_rewrites_literals_and_flags_dynamic_amounts() -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, n: int128) -> uint256:
    a: uint256 = shift(x, 3)
    b: uint256 = shift(x, -2)
    return shift(x, n)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "a: uint256 = (x << 3)" in result.source
    assert "b: uint256 = (x >> 2)" in result.source
    assert "return shift(x, n)" in result.source
    assert any(fix.rule == "VY111" for fix in result.fixes)
    assert any(diag.rule == "VYD012" for diag in result.diagnostics)
