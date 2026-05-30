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
    assert "StrategyParams({" in result.source
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
