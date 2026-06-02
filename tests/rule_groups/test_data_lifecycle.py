from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_nested_struct_literals_rewrite_without_overlapping_edits(config) -> None:
    source = """# @version 0.3.10
struct TokenPermissions:
    token: address
    amount: uint256

struct PermitTransferFrom:
    permitted: TokenPermissions
    nonce: uint256

@external
def f(token: address, amount: uint256):
    permit: PermitTransferFrom = PermitTransferFrom({
        permitted: TokenPermissions({token: token, amount: amount}),
        nonce: 1,
    })
"""

    result = apply_rules(source, config())

    assert "TokenPermissions(token=token, amount=amount)" in result.source
    assert "PermitTransferFrom(permitted=TokenPermissions" in result.source


def test_create_from_blueprint_adds_code_offset_by_default(config) -> None:
    source = """# @version 0.3.10
@external
def f(target: address):
    create_from_blueprint(target)
"""

    result = apply_rules(source, config())

    assert "create_from_blueprint(target, code_offset=0)" in result.source
    assert any(fix.rule == "VY080" for fix in result.fixes)


def test_max_value_bound_public_storage_array_becomes_hashmap(config) -> None:
    source = """# @version 0.3.7
get_gauge_count: public(uint256)
get_gauge: public(address[max_value(uint256)])
restLayout: public(bytes32[115792089237316195423570985008687907853269984665640564039457584007913129639935])
fixed: public(address[8])

@external
def f(idx: uint256, gauge: address):
    self.get_gauge[idx] = gauge
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "get_gauge: public(HashMap[uint256, address])" in result.source
    assert "restLayout: public(HashMap[uint256, bytes32])" in result.source
    assert "fixed: public(address[8])" in result.source
    assert "self.get_gauge[idx] = gauge" in result.source
    assert any(fix.rule == "VY091" for fix in result.fixes)


def test_reserved_flag_public_storage_gets_backing_variable_and_getter(config) -> None:
    source = """# @version 0.2.12
flag: public(bool)
amount: public(uint256)

@external
def set_flag():
    self.flag = True

@external
def read_flag() -> bool:
    return self.flag
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "_flag: bool" in result.source
    assert "flag: public(bool)" not in result.source
    assert "def flag() -> bool:" in result.source
    assert "return self._flag" in result.source
    assert "self.flag" not in result.source
    assert any(fix.rule == "VY093" for fix in result.fixes)


def test_unbounded_dynarray_int128_limit_is_capped(config) -> None:
    source = """# @version 0.3.6
struct Pair:
    token: address

@external
@view
def pairs() -> DynArray[Pair, max_value(int128)]:
    all: DynArray[Pair, max_value(int128)] = []
    for index in range(max_value(int128)):
        if index > 10:
            break
    return all
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "def pairs() -> DynArray[Pair, max_value(uint32)]:" in result.source
    assert "all: DynArray[Pair, max_value(uint32)] = []" in result.source
    assert "for index: uint32 in range(max_value(uint32)):" in result.source
    assert any(fix.rule == "VY094" for fix in result.fixes)


def test_unreachable_code_after_return_is_removed(config) -> None:
    source = """# @version 0.3.10
@external
def f(flag: bool) -> uint256:
    if flag:
        return 1
        unreachable: uint256 = 2
    return 0
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "unreachable" not in result.source
    assert "return 0" in result.source
    assert any(fix.rule == "VY092" for fix in result.fixes)


def test_unreachable_code_after_exhaustive_if_chain_is_removed(config) -> None:
    source = """# @version 0.3.10
@external
def f(kind: uint256) -> uint256:
    if kind == 1:
        return 1
    elif kind == 2:
        return 2
    else:
        raise

    return 0
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "return 0" not in result.source
    assert any(fix.rule == "VY092" for fix in result.fixes)


def test_unreachable_code_inside_loop_is_removed(config) -> None:
    source = """# @version 0.3.10
@external
def f() -> uint256:
    for i in range(3):
        return i
        value: uint256 = i + 1
    return 0
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "value: uint256" not in result.source
    assert "return 0" in result.source
    assert any(fix.rule == "VY092" for fix in result.fixes)


def test_unreachable_code_after_nested_exhaustive_if_chain_is_removed(config) -> None:
    source = """# @version 0.3.10
@external
def f(gain: uint256, loss: uint256, fee: uint256) -> uint256:
    if gain >= loss:
        gross_profit: uint256 = gain - loss
        if gross_profit >= fee:
            return 0
        else:
            return fee - gross_profit
    else:
        return loss - gain + fee

    return 0
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert result.source.count("return 0") == 1
    assert "return loss - gain + fee" in result.source
    assert any(fix.rule == "VY092" for fix in result.fixes)


def test_unreachable_code_after_multiline_signature_if_chain_is_removed(config) -> None:
    source = """# @version 0.3.10
@external
def f(
    gain: uint256,
    loss: uint256,
    fee: uint256
) -> uint256:
    if gain >= loss:
        gross_profit: uint256 = gain - loss
        if gross_profit >= fee:
            return 0
        else:
            return fee - gross_profit
    else:
        return loss - gain + fee

    return 0
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert result.source.count("return 0") == 1
    assert "return loss - gain + fee" in result.source
    assert any(fix.rule == "VY092" for fix in result.fixes)


def test_non_exhaustive_if_chain_is_not_removed_as_unreachable(config) -> None:
    source = """# @version 0.3.10
@external
def f(kind: uint256) -> uint256:
    if kind == 1:
        return 1
    value: uint256 = 2
    return value
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "value: uint256 = 2" in result.source
    assert not any(fix.rule == "VY092" for fix in result.fixes)


def test_unreachable_code_keeps_backslash_return_continuation(config) -> None:
    source = """# @version 0.3.10
@internal
@pure
def pack(a: uint256, b: uint256) -> uint256:
    return a | \\
        (b << 64)

@internal
@pure
def unpack(value: uint256) -> uint256:
    return value
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "return a | \\\n        (b << 64)" in result.source
    assert "@internal\n@pure\ndef unpack" in result.source
    assert not any(fix.rule == "VY092" for fix in result.fixes)


def test_unreachable_code_keeps_backslash_boolean_return_continuation(config) -> None:
    source = """# @version 0.3.10
interface Access:
    def owner() -> address: view
    def hasRole(role: bytes32, operator: address) -> bool: view

ADMIN_ROLE: constant(bytes32) = keccak256("ADMIN")

@internal
@view
def is_admin(target: address, operator: address) -> bool:
    return Access(target).owner() == operator \\
        or Access(target).hasRole(ADMIN_ROLE, operator)

@internal
@view
def done() -> bool:
    return True
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "== operator \\\n        or staticcall Access(target).hasRole" in result.source
    assert "@internal\n@view\ndef done" in result.source
    assert not any(fix.rule == "VY092" for fix in result.fixes)


def test_pr_3777_struct_dict_instantiation_to_kwargs(config) -> None:
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


def test_struct_keyword_constructor_reorders_to_declaration_order(config) -> None:
    source = """# @version 0.3.10
struct VotedSlope:
    slope: uint256
    power: uint256
    end: uint256

@external
def f(slope: uint256, power: uint256, lock_end: uint256):
    new_slope: VotedSlope = VotedSlope(slope=slope, end=lock_end, power=power)
"""

    result = apply_rules(source, config())

    assert "VotedSlope(slope=slope, power=power, end=lock_end)" in result.source


def test_struct_constructor_reorders_with_full_line_comments(config) -> None:
    source = """# @version 0.3.10
struct StrategyParams:
    performanceFee: uint256
    activation: uint256
    enforceChangeLimit: bool
    profitLimitRatio: uint256
    lossLimitRatio: uint256
    customCheck: address

@external
def f(strategy: StrategyParams) -> StrategyParams:
    return StrategyParams({
        performanceFee: strategy.performanceFee,
        # NOTE: keep old activation time
        activation: strategy.activation,
        profitLimitRatio: strategy.profitLimitRatio,
        lossLimitRatio: strategy.lossLimitRatio,
        enforceChangeLimit: True,
        customCheck: strategy.customCheck,
    })
"""

    result = apply_rules(source, config())

    assert (
        "activation=strategy.activation,\n"
        "        enforceChangeLimit=True,\n"
        "        profitLimitRatio=strategy.profitLimitRatio,"
    ) in result.source
    assert "# NOTE: keep old activation time" in result.source


def test_struct_constructor_casts_integer_field_arguments(config) -> None:
    source = """# @version 0.2.7
struct SwapData:
    pool: address
    coin: address
    i: int128

@external
def f(pool: address, coin: address):
    for i in range(8):
        data: SwapData = SwapData({pool: pool, coin: coin, i: i})
"""

    result = apply_rules(source, config())

    assert "SwapData(pool=pool, coin=coin, i=convert(i, int128))" in result.source


def test_struct_literal_with_comments_is_left_source_preserving(config) -> None:
    source = """# @version 0.3.10
struct StrategyParams:
    performanceFee: uint256
    activation: uint256
    enforceChangeLimit: bool
    profitLimitRatio: uint256

@external
def f(fee: uint256, ts: uint256, ratio: uint256):
    params: StrategyParams = StrategyParams({
        performanceFee: fee,
        # use current timestamp
        activation: ts,
        profitLimitRatio: ratio,
        enforceChangeLimit: True,
    })
"""

    result = apply_rules(source, config())

    assert "# use current timestamp" in result.source
    assert "StrategyParams({" not in result.source
    assert "activation=ts,\n        enforceChangeLimit=True,\n        profitLimitRatio=ratio," in result.source
    assert "StrategyParams(performanceFee=fee" not in result.source


def test_pr_3697_enum_to_flag_is_review_by_default_and_aggressive_fix(config) -> None:
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


def test_pr_3729_constructor_deploy_replaces_external(config) -> None:
    source = """# @version 0.3.10
@external
def __init__():
    pass
"""

    result = apply_rules(source, config())

    assert "@external\ndef __init__" not in result.source
    assert "@deploy\ndef __init__" in result.source


def test_constructor_deploy_removes_return_type(config) -> None:
    source = """# @version 0.3.10
@external
def __init__(owner: address) -> bool:
    self.owner = owner
    return owner
"""

    result = apply_rules(source, config())

    assert "@deploy\ndef __init__(owner: address):" in result.source
    assert "-> bool" not in result.source
    assert "    return\n" in result.source
    assert "return owner" not in result.source
    assert any(fix.rule == "VY002" and "return type" in fix.message for fix in result.fixes)
    assert any(fix.rule == "VY002" and "return value" in fix.message for fix in result.fixes)


def test_pr_3769_single_named_reentrancy_lock_is_rewritten(config) -> None:
    source = """# @version 0.3.10
@nonreentrant("lock")
@external
def f():
    pass
"""

    result = apply_rules(source, config())

    assert '@nonreentrant("lock")' not in result.source
    assert "@nonreentrant\n@external" in result.source


def test_named_reentrancy_lock_reserves_legacy_storage_slot(config) -> None:
    source = """# @version 0.3.10
balance: uint256

@internal
@nonreentrant("lock")
def f():
    pass
"""

    result = apply_rules(source, config())

    assert "_vyupgrade_reentrancy_lock_slot: uint256" in result.source
    assert result.source.index("_vyupgrade_reentrancy_lock_slot") < result.source.index("balance")
    assert any(
        fix.rule == "VY090" and "storage slot" in fix.message for fix in result.fixes
    )


def test_named_reentrancy_lock_keeps_layout_gap_out_when_decorator_remains(config) -> None:
    source = """# @version 0.3.10
# pragma evm-version shanghai
balance: uint256

@nonreentrant("lock")
@external
def f():
    pass
"""

    result = apply_rules(source, config())

    assert "_vyupgrade_reentrancy_lock_slot: uint256" not in result.source
    assert "@nonreentrant\n@external" in result.source


def test_named_reentrancy_lock_reserves_slot_when_target_uses_default_evm(config) -> None:
    source = """# @version 0.3.10
balance: uint256

@nonreentrant("lock")
@external
def f():
    pass
"""

    result = apply_rules(source, config())

    assert "_vyupgrade_reentrancy_lock_slot: uint256" in result.source
    assert "@nonreentrant\n@external" in result.source


def test_internal_nonreentrant_removed_after_global_lock_migration(config) -> None:
    source = """# @version 0.3.7
@internal
@nonreentrant("lock")
def _reentrant():
    pass

@external
@nonreentrant("lock")
def f():
    self._reentrant()
"""

    result = apply_rules(source, config())

    assert "@internal\n@nonreentrant\ndef _reentrant" not in result.source
    assert "@internal\ndef _reentrant" in result.source
    assert "@external\n@nonreentrant\ndef f" in result.source
