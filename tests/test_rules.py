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

    assert "#pragma version 0.4.3" in result.source
    assert "@deploy\ndef __init__" in result.source
    assert "@external\ndef __init__" not in result.source
    assert "abi_encode(1)" in result.source
    assert '"_abi_decode should stay"' in result.source
    assert "# _abi_encode should stay" in result.source


def test_line_rewrites_skip_docstring_content() -> None:
    source = '''# @version 0.3.10

@external
def f():
    """
    @public
    # @version 0.2.1
    contract Foo:
    """
    pass
'''

    result = apply_rules(source, config())

    assert "#pragma version 0.4.3" in result.source
    assert "    @public" in result.source
    assert "    # @version 0.2.1" in result.source
    assert "    contract Foo:" in result.source
    assert "    @external" not in result.source
    assert "    #pragma version 0.2.1" not in result.source
    assert "    interface Foo:" not in result.source


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


def test_modern_erc_interface_imports() -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC4626, ERC721

asset: public(ERC4626)
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC4626, IERC721" in result.source
    assert "asset: public(IERC4626)" in result.source


def test_erc721_import_migration_preserves_staticcall_inference() -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC721

@external
def f(nft: ERC721, owner: address) -> uint256:
    return nft.balanceOf(owner)
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC721" in result.source
    assert "def f(nft: IERC721, owner: address)" in result.source
    assert "return staticcall nft.balanceOf(owner)" in result.source
    assert not [diag for diag in result.diagnostics if diag.rule == "VYD003"]


def test_modern_erc_interface_imports_alias_when_new_name_exists() -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC20

interface IERC20:
    def decimals() -> uint256: view

@external
def f(token: address) -> uint256:
    return ERC20(token).balanceOf(msg.sender) + IERC20(token).decimals()
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC20 as ERC20" in result.source
    assert "interface IERC20:" in result.source
    assert "ERC20(token).balanceOf" in result.source
    assert "IERC20(token).decimals" in result.source


def test_erc4626_builtin_calls() -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC4626

@external
def f(vault: address) -> uint256:
    return ERC4626(vault).convertToAssets(10**18)
"""

    result = apply_rules(source, config())

    assert "return staticcall IERC4626(vault).convertToAssets(10**18)" in result.source


def test_ignored_external_call_result_is_assigned() -> None:
    source = """# @version 0.3.10
interface VeDelegation:
    def adjusted_balance_of(_account: address) -> uint256: view

@external
def set_delegation(delegation: address):
    VeDelegation(delegation).adjusted_balance_of(msg.sender)  # validation call
"""

    result = apply_rules(source, config())

    assert "__vyupgrade_discard_7: uint256 = staticcall VeDelegation(delegation).adjusted_balance_of(msg.sender)  # validation call" in result.source
    assert any(fix.rule == "VY057" for fix in result.fixes)


def test_ignored_external_call_without_return_stays_statement() -> None:
    source = """# @version 0.3.10
interface Token:
    def transfer(receiver: address, amount: uint256): nonpayable

@external
def f(token: address, receiver: address):
    Token(token).transfer(receiver, 1)
"""

    result = apply_rules(source, config())

    assert "extcall Token(token).transfer(receiver, 1)" in result.source
    assert "__vyupgrade_discard" not in result.source


def test_external_call_inside_multiline_expression_is_not_discard_assigned() -> None:
    source = """# @version 0.3.10
interface Strategy:
    def maxRedeem(owner: address) -> uint256: view
    def convertToAssets(shares: uint256) -> uint256: view

@external
def f(strategy: address) -> uint256:
    return Strategy(strategy).convertToAssets(
        Strategy(strategy).maxRedeem(self)
    )
"""

    result = apply_rules(source, config())

    assert "staticcall Strategy(strategy).maxRedeem(self)" in result.source
    assert "__vyupgrade_discard" not in result.source


def test_external_call_in_backslash_assignment_is_not_discard_assigned() -> None:
    source = """# @version 0.3.10
interface Feed:
    def latestRoundData() -> (uint256, int256, uint256, uint256, uint256): view

@external
def f(feed: address):
    round_id: uint256 = 0
    answer: int256 = 0
    started_at: uint256 = 0
    updated_at: uint256 = 0
    answered_in_round: uint256 = 0
    (round_id, answer, started_at, updated_at, answered_in_round) = \\
        Feed(feed).latestRoundData()
"""

    result = apply_rules(source, config())

    assert "staticcall Feed(feed).latestRoundData()" in result.source
    assert "__vyupgrade_discard" not in result.source


def test_ignored_staticcall_array_result_keeps_full_return_type() -> None:
    source = """# @version 0.3.10
interface Synth:
    def settle(key: bytes32) -> uint256[3]: view

@external
def f(target: address, key: bytes32):
    Synth(target).settle(key)
"""

    result = apply_rules(source, config())

    assert "__vyupgrade_discard_7: uint256[3] = staticcall Synth(target).settle(key)" in result.source


def test_nested_struct_literals_rewrite_without_overlapping_edits() -> None:
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


def test_nested_shift_rewrites_without_overlapping_edits() -> None:
    source = """# @version 0.3.0
@external
def f(indexes: uint256) -> uint256:
    return shift(shift(indexes, -128), 128)
"""

    result = apply_rules(source, config())

    assert "shift(" not in result.source
    assert "return ((indexes >> 128) << 128)" in result.source


def test_event_logs_rewrite_to_keyword_arguments() -> None:
    source = """# @version 0.3.0
event Transfer:
    sender: indexed(address)
    receiver: indexed(address)
    value: uint256

@external
def f(receiver: address, value: uint256):
    log Transfer(msg.sender, receiver, value)
"""

    result = apply_rules(source, config())

    assert "log Transfer(sender=msg.sender, receiver=receiver, value=value)" in result.source


def test_event_logs_rewrite_multiline_arguments_with_comments() -> None:
    source = """# @version 0.3.10
event StrategyReported:
    strategy: indexed(address)
    gain: uint256
    loss: uint256
    protocol_fees: uint256
    total_fees: uint256

@external
def f(strategy: address, gain: uint256, loss: uint256, total_fees: uint256):
    log StrategyReported(
        strategy,
        gain,
        loss,
        total_fees / 100,  # Protocol Fees
        total_fees
    )
"""

    result = apply_rules(source, config())

    assert "protocol_fees=total_fees // 100" in result.source
    assert "total_fees=#" not in result.source
    assert "total_fees=total_fees" in result.source


def test_array_loop_type_inference() -> None:
    source = """# @version 0.3.10
@external
def f(items: DynArray[address, 10]):
    for item in items:
        pass
"""

    result = apply_rules(source, config())

    assert "for item: address in items:" in result.source


def test_range_loop_uses_known_bound_integer_type() -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 2

@external
def f():
    for i in range(N_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "for i: int128 in range(N_COINS):" in result.source


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


def test_lowercase_c_prefix_interface_calls() -> None:
    source = """# @version 0.2.4
interface cERC20:
    def balanceOf(owner: address) -> uint256: view

@external
def f(token: address) -> uint256:
    return cERC20(token).balanceOf(self)
"""

    result = apply_rules(source, config())

    assert "return staticcall cERC20(token).balanceOf(self)" in result.source


def test_nested_interface_cast_calls() -> None:
    source = """# @version 0.3.7
interface Token:
    def balanceOf(owner: address) -> uint256: view
    def allowance(owner: address, spender: address) -> uint256: view

@external
def f(token: address, owner: address) -> uint256:
    return min(Token(token).balanceOf(owner), Token(token).allowance(owner, self))
"""

    result = apply_rules(source, config())

    assert "min(staticcall Token(token).balanceOf(owner), staticcall Token(token).allowance(owner, self))" in result.source


def test_multiline_interface_method_mutability() -> None:
    source = """# @version 0.3.1
interface Calculator:
    def get_dy(n_coins: uint256, balances: uint256[8],
               i: int128, j: int128) -> uint256: view

@external
def f(calculator: address, balances: uint256[8], i: int128, j: int128) -> uint256:
    return Calculator(calculator).get_dy(2, balances, i, j)
"""

    result = apply_rules(source, config())

    assert "return staticcall Calculator(calculator).get_dy(2, balances, i, j)" in result.source


def test_legacy_interface_body_mutability_updates_keyword() -> None:
    source = """# @version 0.2.15
interface Vault:
    def withdraw(amount: uint256):
        nonpayable

    def token() -> address:
        view

@external
def f(vault: address, amount: uint256) -> address:
    extcall Vault(vault).withdraw(amount)
    return extcall Vault(vault).token()
"""

    result = apply_rules(source, config())

    assert "extcall Vault(vault).withdraw(amount)" in result.source
    assert "return staticcall Vault(vault).token()" in result.source


def test_immutable_interface_storage_var_calls() -> None:
    source = """# @version 0.3.10
interface Pool:
    def set_management(account: address): nonpayable

pool: immutable(Pool)

@external
def __init__(_pool: address):
    pool = Pool(_pool)

@external
def f(account: address):
    pool.set_management(account)
"""

    result = apply_rules(source, config())

    assert "extcall pool.set_management(account)" in result.source


def test_legacy_interface_header_calls() -> None:
    source = """# @version ^0.3.3
interface Vault():
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable

VAULT: immutable(Vault)

@external
def __init__(vault: address):
    VAULT = Vault(vault)

@external
def f(amount: uint256):
    VAULT.transferFrom(msg.sender, self, amount)
"""

    result = apply_rules(source, config())

    assert "extcall VAULT.transferFrom(msg.sender, self, amount)" in result.source


def test_multiline_assert_interface_cast_call() -> None:
    source = """# @version 0.1.0b17
contract Token:
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: modifying

coins: address[2]

@public
def f(amount: uint256):
    assert Token(self.coins[0])\\
        .transferFrom(msg.sender, self, amount)
"""

    result = apply_rules(source, config())

    assert "assert extcall Token(self.coins[0])\\\n        .transferFrom(msg.sender, self, amount)" in result.source


def test_multiline_interface_def_calls() -> None:
    source = """# @version 0.3.7
interface Vault:
    def initialize(
        asset: address,
        name: String[64]
    ): nonpayable

@external
def f(vault: address, asset: address, name: String[64]):
    Vault(vault).initialize(asset, name)
"""

    result = apply_rules(source, config())

    assert "extcall Vault(vault).initialize(asset, name)" in result.source


def test_nested_cast_expression_call() -> None:
    source = """# @version 0.3.7
interface Vault:
    def asset() -> address: view

interface Token:
    def balanceOf(owner: address) -> uint256: view

vault: address

@external
def f() -> uint256:
    return Token(staticcall Vault(self.vault).asset()).balanceOf(self)
"""

    result = apply_rules(source, config())

    assert "return staticcall Token(staticcall Vault(self.vault).asset()).balanceOf(self)" in result.source


def test_external_call_subscript_parentheses() -> None:
    source = """# @version 0.3.10
interface Auction:
    def auctions(account: address) -> (uint256, uint256): view

auction: Auction

@external
def f(account: address) -> bool:
    return auction.auctions(account)[1] == 0
"""

    result = apply_rules(source, config())

    assert "return (staticcall auction.auctions(account))[1] == 0" in result.source


def test_external_call_cast_subscript_parentheses() -> None:
    source = """# @version 0.3.1
interface Registry:
    def get_fees(pool: address) -> uint256[2]: view

registry: address

@external
def f(pool: address) -> uint256:
    return Registry(registry).get_fees(pool)[0]
"""

    result = apply_rules(source, config())

    assert "return (staticcall Registry(registry).get_fees(pool))[0]" in result.source


def test_external_call_result_attribute_parentheses() -> None:
    source = """# @version 0.2.16
interface Vat:
    def urns(ilk: bytes32, urn: address) -> Vault: view

struct Vault:
    ink: uint256
    art: uint256

vat: address

@external
def f(ilk: bytes32, urn: address) -> uint256:
    return Vat(vat).urns(ilk, urn).ink
"""

    result = apply_rules(source, config())

    assert "return (staticcall Vat(vat).urns(ilk, urn)).ink" in result.source


def test_interface_after_struct_keeps_method_mutability() -> None:
    source = """# @version 0.3.7
struct StrategyParams:
    activation: uint256

interface IVault:
    def strategies(strategy: address) -> StrategyParams: view
    def deposit(assets: uint256, receiver: address) -> uint256: nonpayable

@external
def f(vault: address, strategy: address) -> StrategyParams:
    return IVault(vault).strategies(strategy)
"""

    result = apply_rules(source, config())

    assert "return staticcall IVault(vault).strategies(strategy)" in result.source


def test_external_call_on_interface_struct_field() -> None:
    source = """# @version 0.3.7
interface Stableswap:
    def price_oracle() -> uint256: view

struct PricePair:
    pool: Stableswap

price_pairs: PricePair[2]

@external
def f() -> uint256:
    price_pair: PricePair = self.price_pairs[0]
    return price_pair.pool.price_oracle()
"""

    result = apply_rules(source, config())

    assert "return staticcall price_pair.pool.price_oracle()" in result.source


def test_external_call_on_typed_loop_variable() -> None:
    source = """# @version 0.3.7
interface PegKeeper:
    def debt() -> uint256: view

peg_keepers: PegKeeper[5]

@external
def f() -> uint256:
    total: uint256 = 0
    for pk: PegKeeper in self.peg_keepers:
        total += pk.debt()
    return total
"""

    result = apply_rules(source, config())

    assert "total += staticcall pk.debt()" in result.source


def test_external_call_on_local_interface_variable_after_returning_call() -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC20

interface Registry:
    def tokens(index: uint256) -> ERC20: view

registry: Registry

@external
def f(user: address) -> uint256:
    token: ERC20 = registry.tokens(0)
    return token.balanceOf(user)
"""

    result = apply_rules(source, config())

    assert "token: IERC20 = staticcall registry.tokens(0)" in result.source
    assert "return staticcall token.balanceOf(user)" in result.source


def test_struct_literal_field_does_not_shadow_interface_local() -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC20

struct TokenInfo:
    token: address
    token_balance: uint256

@external
def f(user: address, token_address: address) -> TokenInfo:
    token: ERC20 = ERC20(token_address)
    token_balance: uint256 = token.balanceOf(user)
    return TokenInfo({
        token: token.address,
        token_balance: token_balance,
    })
"""

    result = apply_rules(source, config())

    assert "token_balance: uint256 = staticcall token.balanceOf(user)" in result.source
    assert "token=token.address" in result.source


def test_integer_expression_division() -> None:
    source = """# @version 0.3.10
MAX_BPS: constant(uint256) = 10_000

@external
def f(total_fees: uint256, protocol_fee_bps: uint16) -> uint256:
    return total_fees * convert(protocol_fee_bps, uint256) / MAX_BPS
"""

    result = apply_rules(source, config())

    assert "convert(protocol_fee_bps, uint256) // MAX_BPS" in result.source


def test_immutable_accessor_collision_renames_backing_variable() -> None:
    source = """# @version 0.3.10
x: immutable(uint256)

@external
def __init__(initial_x: uint256):
    x = initial_x

@view
@external
def x() -> uint256:
    return x
"""

    result = apply_rules(source, config())

    assert "_x: immutable(uint256)" in result.source
    assert "def x() -> uint256:" in result.source
    assert "_x = initial_x" in result.source
    assert "return _x" in result.source
    assert any(fix.rule == "VY013" for fix in result.fixes)


def test_immutable_accessor_collision_preserves_keyword_and_attribute_names() -> None:
    source = """# @version 0.3.10
x: immutable(uint256)

event Changed:
    x: uint256

@external
def __init__(initial_x: uint256):
    x = initial_x

@external
def x() -> uint256:
    log Changed(x=x)
    return self.x()
"""

    result = apply_rules(source, config())

    assert "log Changed(x=_x)" in result.source
    assert "x: uint256" in result.source
    assert "return self.x()" in result.source


def test_immutable_accessor_collision_avoids_existing_local_names() -> None:
    source = """# @version 0.3.10
coins: immutable(address[2])

@external
def __init__(_coins: address[2]):
    coins = _coins

@view
@external
def coins(i: uint256) -> address:
    _coins: address[2] = coins
    return _coins[i]
"""

    result = apply_rules(source, config())

    assert "__coins: immutable(address[2])" in result.source
    assert "\n    coins = _coins" not in result.source
    assert "__coins = _coins" in result.source
    assert "_coins: address[2] = __coins" in result.source


def test_constant_accessor_collision_renames_backing_variable() -> None:
    source = """# @version 0.3.10
token: constant(address) = 0x0000000000000000000000000000000000000001
coins: constant(address[2]) = [
    0x0000000000000000000000000000000000000010,
    0x0000000000000000000000000000000000000011,
]

@external
@view
def token() -> address:
    return token

@external
@view
def coins(i: uint256) -> address:
    _coins: address[2] = coins
    return _coins[i]

@external
def use_token(receiver: address):
    extcall CurveToken(token).mint(receiver, 1)
"""

    result = apply_rules(source, config())

    assert "_token: constant(address)" in result.source
    assert "__coins: constant(address[2])" in result.source
    assert "def token() -> address:\n    return _token" in result.source
    assert "def coins(i: uint256) -> address:\n    _coins: address[2] = __coins" in result.source
    assert "CurveToken(_token).mint" in result.source
    assert any(fix.rule == "VY016" for fix in result.fixes)


def test_constant_accessor_collision_handles_uppercase_names() -> None:
    source = """# @version 0.3.10
DAY: constant(uint256) = 86400
GRACE_PERIOD: constant(uint256) = 14 * DAY

@external
@view
def GRACE_PERIOD() -> uint256:
    return GRACE_PERIOD

@external
def f(eta: uint256):
    assert block.timestamp <= eta + GRACE_PERIOD
"""

    result = apply_rules(source, config())

    assert "_GRACE_PERIOD: constant(uint256) = 14 * DAY" in result.source
    assert "def GRACE_PERIOD() -> uint256:\n    return _GRACE_PERIOD" in result.source
    assert "eta + _GRACE_PERIOD" in result.source


def test_natspec_strictness_removes_unknown_params_and_customizes_unknown_tags() -> None:
    source = '''# @version 0.3.10
"""
@title Voting Escrow
@fork Curve Finance
"""

@external
def createMotion(
    targets: DynArray[address, 4],
    values: DynArray[uint256, 4],
) -> uint256:
    """
    @notice Create a motion.
    @param targets: The contracts to call
    @param values: The values to send
    @param calldatas: The calldata payloads
    @param emptyParam
    @return motionId: The id of the motion
    """
    return 1
'''

    result = apply_rules(source, config())

    assert "@custom:fork Curve Finance" in result.source
    assert "@param targets The contracts to call" in result.source
    assert "@param values The values to send" in result.source
    assert "calldatas" not in result.source
    assert "emptyParam" not in result.source
    assert any(fix.rule == "VY058" for fix in result.fixes)


def test_local_interface_nonpayable_matches_view_function() -> None:
    source = """# @version 0.3.10
interface Bucket:
    def above_floor() -> bool: nonpayable

implements: Bucket

@external
@view
def above_floor() -> bool:
    return False
"""

    result = apply_rules(source, config())

    assert "def above_floor() -> bool: view" in result.source
    assert any(fix.rule == "VY014" for fix in result.fixes)


def test_local_interface_nonpayable_matches_public_getter() -> None:
    source = """# @version 0.3.10
interface RateProvider:
    def rate(_asset: address) -> uint256: nonpayable

implements: RateProvider

rate: public(HashMap[address, uint256])
"""

    result = apply_rules(source, config())

    assert "def rate(_asset: address) -> uint256: view" in result.source
    assert any(fix.rule == "VY014" for fix in result.fixes)


def test_pure_function_reading_immutable_becomes_view() -> None:
    source = """# @version 0.3.10
TARGET: immutable(address)

@external
def __init__(_target: address):
    TARGET = _target

@pure
@external
def target() -> address:
    return TARGET
"""

    result = apply_rules(source, config())

    assert "@view\n@external\ndef target" in result.source
    assert any(fix.rule == "VY015" for fix in result.fixes)


def test_internal_pure_function_reading_immutable_becomes_view() -> None:
    source = """# @version 0.3.10
N_COINS: immutable(int128)

@pure
@internal
def checked_coin(i: int128) -> int128:
    assert i < N_COINS
    return i
"""

    result = apply_rules(source, config())

    assert "@view\n@internal\ndef checked_coin" in result.source
    assert any(fix.rule == "VY015" for fix in result.fixes)


def test_pure_function_without_immutable_read_stays_pure() -> None:
    source = """# @version 0.3.10
N_COINS: immutable(int128)

@pure
@internal
def add_one(i: int128) -> int128:
    return i + 1
"""

    result = apply_rules(source, config())

    assert "@pure\n@internal\ndef add_one" in result.source
    assert not any(fix.rule == "VY015" for fix in result.fixes)


def test_compound_assignment_integer_division() -> None:
    source = """# @version 0.3.7
MAX_BPS: constant(uint256) = 10_000

struct Fee:
    performance_fee: uint16

@external
def f(gain: uint256, fee: Fee) -> uint256:
    total_fees: uint256 = 0
    total_fees += (gain * fee.performance_fee) / MAX_BPS
    return total_fees
"""

    result = apply_rules(source, config())

    assert "total_fees += (gain * fee.performance_fee) // MAX_BPS" in result.source


def test_indexed_storage_compound_assignment_integer_division() -> None:
    source = """# @version 0.3.7
tokens_per_week: public(HashMap[uint256, uint256])

@external
def f(this_week: uint256, to_distribute: uint256, t: uint256, since_last: uint256):
    self.tokens_per_week[this_week] += to_distribute * (block.timestamp - t) / since_last
"""

    result = apply_rules(source, config())

    assert "self.tokens_per_week[this_week] += to_distribute * (block.timestamp - t) // since_last" in result.source


def test_integer_division_inside_storage_subscript() -> None:
    source = """# @version 0.3.10
packed_factory_versions: HashMap[uint256, uint256]

@internal
@view
def _enabled(_version: uint256) -> bool:
    return self.packed_factory_versions[_version / 256] & (1 << (_version % 256)) > 0
"""

    result = apply_rules(source, config())

    assert "self.packed_factory_versions[_version // 256]" in result.source


def test_multiline_function_scope_integer_division_assignment() -> None:
    source = """# @version 0.3.7
rates: public(uint256[3])

@external
def exchange(
    i: uint256,
    j: uint256,
    amount: uint256,
) -> uint256:
    rates: uint256[3] = self.rates
    dy: uint256 = amount
    dy = dy * 10**18 / rates[j]
    return dy
"""

    result = apply_rules(source, config())

    assert "dy = dy * 10**18 // rates[j]" in result.source


def test_struct_attribute_integer_division() -> None:
    source = """# @version 0.3.7
struct Loan:
    initial_debt: uint256
    rate_mul: uint256

@external
def f(loan: Loan, rate_mul: uint256) -> (uint256, uint256):
    return (loan.initial_debt * rate_mul / loan.rate_mul, rate_mul)
"""

    result = apply_rules(source, config())

    assert "return (loan.initial_debt * rate_mul // loan.rate_mul, rate_mul)" in result.source


def test_external_call_integer_division_operand() -> None:
    source = """# @version 0.3.7
interface Pool:
    def virtual_balance(asset: uint256) -> uint256: view
    def rate(asset: uint256) -> uint256: view

@external
def f(pool: Pool, asset: uint256, rate: uint256) -> uint256:
    return pool.virtual_balance(asset) * rate / pool.rate(asset)
"""

    result = apply_rules(source, config())

    assert "return staticcall pool.virtual_balance(asset) * rate // staticcall pool.rate(asset)" in result.source


def test_multiline_return_internal_call_integer_division() -> None:
    source = """# @version 0.2.12
totalSupply: public(uint256)

@internal
def _totalAssets() -> uint256:
    return 1

@external
def f(amount: uint256) -> uint256:
    return (
        amount
        * self.totalSupply
        / self._totalAssets()
    )
"""

    result = apply_rules(source, config())

    assert "* self.totalSupply\n        // self._totalAssets()" in result.source


def test_multiline_parenthesized_assignment_integer_division() -> None:
    source = """# @version 0.3.1
struct StrategyParams:
    performanceFee: uint256

strategies: HashMap[address, StrategyParams]

@internal
def f(strategy: address, gain: uint256) -> uint256:
    strategist_fee: uint256 = 0
    strategist_fee = (
        gain * self.strategies[strategy].performanceFee
    ) / 10_000
    return strategist_fee
"""

    result = apply_rules(source, config())

    assert ") // 10_000" in result.source


def test_return_integer_division_uses_function_return_type() -> None:
    source = """# @version 0.3.7
votes_used: HashMap[address, uint256]
voted: uint256

@external
def claimable(user: address, amount: uint256) -> uint256:
    return amount * self.votes_used[user] / self.voted
"""

    result = apply_rules(source, config())

    assert "return amount * self.votes_used[user] // self.voted" in result.source


def test_tab_indented_return_integer_division_uses_function_return_type() -> None:
    source = """# @version 0.3.7
@external
def f(x: int128) -> int128:
\treturn 1 / x
"""

    result = apply_rules(source, config())

    assert "return 1 // x" in result.source


def test_multiline_reassignment_integer_division_uses_target_type() -> None:
    source = """# @version 0.3.10
@external
def f(x: uint256, y: uint256, z: uint256) -> uint256:
    value: uint256 = x
    value = (
        x * y
        /
        z
    )
    return value
"""

    result = apply_rules(source, config())

    assert "        //\n" in result.source


def test_integerish_call_argument_division_is_rewritten() -> None:
    source = """# @version 0.3.10
@external
def f(x: uint256, y: uint256) -> DynArray[uint256, 4]:
    values: DynArray[uint256, 4] = []
    values.append((10**18 * unsafe_div(x, y)) / (x + y))
    return values
"""

    result = apply_rules(source, config())

    assert "values.append((10**18 * unsafe_div(x, y)) // (x + y))" in result.source


def test_unsigned_constant_exponent_uses_folded_integer_constants() -> None:
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


def test_int128_max_literal_rewrites_to_max_value() -> None:
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


def test_dynamic_uint_exponent_rewrites_to_pow_mod256() -> None:
    source = """# @version 0.3.10
@external
def f(base: int128, exponent: int128) -> uint256:
    return convert(base, uint256) ** convert(exponent, uint256)
"""

    result = apply_rules(source, config())

    assert "pow_mod256(convert(base, uint256), convert(exponent, uint256))" in result.source
    assert any(fix.rule == "VY055" for fix in result.fixes)


def test_runtime_exponent_uses_folded_integer_constants() -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 2

@external
def f(x: uint256) -> uint256:
    return (10**18 * N_COINS**N_COINS) * x
"""

    result = apply_rules(source, config())

    assert "(10**18 * 2**2) * x" in result.source
    assert any(fix.rule == "VY054" for fix in result.fixes)


def test_unsigned_range_bound_converts_signed_constant() -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 3

@external
def f() -> uint256:
    total: uint256 = 0
    for j: uint256 in range(2, N_COINS + 1):
        total += j
    for k: uint256 in range(N_COINS - 1):
        total += k
    return total
"""

    result = apply_rules(source, config())

    assert "range(2, convert(N_COINS, uint256) + 1, bound=2)" in result.source
    assert "range(convert(N_COINS, uint256) - 1, bound=2)" in result.source
    assert any(fix.rule == "VY056" for fix in result.fixes)


def test_signed_constant_converted_in_uint_arithmetic() -> None:
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

    assert "fee * convert(N_COINS, uint256) // (4 * (convert(N_COINS, uint256) - 1))" in result.source
    assert "for i: int128 in range(N_COINS):" in result.source


def test_signed_constant_converted_in_uint_assignment() -> None:
    source = """# @version 0.2.8
MAX_COINS: constant(int128) = 8

@external
def f() -> uint256:
    n_coins: uint256 = MAX_COINS
    return n_coins
"""

    result = apply_rules(source, config())

    assert "n_coins: uint256 = convert(MAX_COINS, uint256)" in result.source


def test_unsigned_assignment_keeps_unsigned_numerator_when_signed_denominator_converts() -> None:
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


def test_signed_loop_variable_converted_in_uint_assignment() -> None:
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


def test_signed_loop_index_converted_in_uint_index_arithmetic() -> None:
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


def test_signed_constant_converted_in_nested_uint_call_argument() -> None:
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


def test_signed_time_constant_converted_in_uint_comparison() -> None:
    source = """# @version 0.2.8
BASE_CACHE_EXPIRES: constant(int128) = 600
base_cache_updated: public(uint256)

@external
def f() -> bool:
    return block.timestamp > self.base_cache_updated + BASE_CACHE_EXPIRES
"""

    result = apply_rules(source, config())

    assert "self.base_cache_updated + convert(BASE_CACHE_EXPIRES, uint256)" in result.source


def test_signed_constant_converted_in_unsigned_index_comparison() -> None:
    source = """# @version 0.2.8
PRECISION: constant(int128) = 10 ** 18

@external
def f(rate_multipliers: uint256[4]):
    for i in range(4):
        assert rate_multipliers[i] == PRECISION
"""

    result = apply_rules(source, config())

    assert "assert rate_multipliers[i] == convert(PRECISION, uint256)" in result.source


def test_signed_constant_not_converted_in_signed_loop_comparison() -> None:
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


def test_comment_identifier_does_not_create_unsigned_context() -> None:
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


def test_unsigned_constant_converted_in_signed_integer_division() -> None:
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

    assert "u_old.slope = old_locked.amount // convert(MAXTIME, int128)" in result.source


def test_unsigned_constant_converted_in_signed_param_comparison() -> None:
    source = """# @version 0.2.8
MAX_PCT: constant(uint256) = 10_000

@external
def f(percentage: int256):
    assert percentage <= MAX_PCT
"""

    result = apply_rules(source, config())

    assert "assert percentage <= convert(MAX_PCT, int256)" in result.source


def test_event_field_name_does_not_force_signed_param_to_uint() -> None:
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


def test_pure_static_raw_call_relaxes_to_view() -> None:
    source = """# @version 0.3.10
IDENTITY_PRECOMPILE: constant(address) = 0x0000000000000000000000000000000000000004

@pure
@internal
def f(value: Bytes[1]) -> Bytes[1]:
    return raw_call(IDENTITY_PRECOMPILE, value, max_outsize=1, is_static_call=True)
"""

    result = apply_rules(source, config())

    assert "@view\n@internal\ndef f" in result.source


def test_signed_lhs_keeps_signed_operand_in_mixed_arithmetic() -> None:
    source = """# @version 0.3.10
@external
def f(n1: int256, n: uint256) -> int256:
    n2: int256 = n1 + convert(n - 1, int256)
    return n2
"""

    result = apply_rules(source, config())

    assert "n2: int256 = n1 + convert(n - 1, int256)" in result.source
    assert "convert(n1, uint256)" not in result.source


def test_unsigned_constant_inside_final_signed_convert_stays_unsigned() -> None:
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


def test_unsigned_array_literal_expression_keeps_unsigned_constants() -> None:
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


def test_signed_array_constant_assigned_to_unsigned_array_becomes_unsigned() -> None:
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


def test_signed_array_constant_kept_when_used_as_signed_array() -> None:
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


def test_boolean_comparison_prefers_casting_unsigned_constant_peer() -> None:
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


def test_redundant_convert_uses_nearest_local_decl() -> None:
    source = """# @version 0.2.16
MAX_COINS: constant(int128) = 4

@external
def f(coins: address[MAX_COINS], base_coin_offset: uint256) -> address:
    coin: address = empty(address)
    for i in range(MAX_COINS):
        if i >= base_coin_offset:
            x: uint256 = convert(i, uint256) - base_coin_offset
            coin = coins[convert(x, uint256)]
    return coin
"""

    result = apply_rules(source, config())

    assert "coin = coins[x]" in result.source
    assert "coins[convert(x, uint256)]" not in result.source


def test_loop_type_uses_nearest_loop_after_same_name_local_decl() -> None:
    source = """# @version 0.2.16
MAX_COINS: constant(int128) = 4

@external
def f(base_n_coins: uint256) -> bool:
    x: uint256 = 0
    for x in range(MAX_COINS):
        if x == base_n_coins:
            return True
    return False
"""

    result = apply_rules(source, config())

    assert "if convert(x, uint256) == base_n_coins:" in result.source


def test_array_literal_elements_cast_to_exact_integer_type() -> None:
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


def test_unsigned_range_loop_converted_in_signed_comparison() -> None:
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


def test_signed_parameter_not_converted_in_uint_arithmetic() -> None:
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


def test_signed_attribute_fields_are_not_rewritten_as_methods() -> None:
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


def test_signed_internal_call_argument_not_converted_for_uint_assignment() -> None:
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


def test_signed_constant_not_converted_when_external_param_is_signed() -> None:
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


def test_external_call_argument_casts_to_interface_param_type() -> None:
    source = """# @version 0.2.16
interface CurveBase:
    def calc_withdraw_one_coin(_token_amount: uint256, i: int128) -> uint256: view

@external
def f(pool: address, amount: uint256, i: uint256) -> uint256:
    return CurveBase(pool).calc_withdraw_one_coin(amount, i)
"""

    result = apply_rules(source, config())

    assert "staticcall CurveBase(pool).calc_withdraw_one_coin(amount, convert(i, int128))" in result.source


def test_signed_cast_argument_preserves_uint_arithmetic_inside_convert() -> None:
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


def test_signed_constant_casted_in_unsigned_subscript_arithmetic() -> None:
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


def test_signed_loop_index_casted_in_unsigned_subscript_arithmetic() -> None:
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


def test_signed_assignment_after_subscript_is_not_treated_as_index_context() -> None:
    source = """# @version 0.2.16
gauge_types_: HashMap[address, int128]

@external
def f(addr: address, gauge_type: int128):
    self.gauge_types_[addr] = gauge_type + 1
"""

    result = apply_rules(source, config())

    assert "self.gauge_types_[addr] = gauge_type + 1" in result.source
    assert "convert(gauge_type, uint256)" not in result.source


def test_signed_negation_assigned_to_uint_is_converted() -> None:
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


def test_external_call_arg_uses_nearest_loop_type_for_reused_name() -> None:
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


def test_signed_loop_type_is_not_overwritten_by_later_loop_same_name() -> None:
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


def test_redundant_convert_keeps_signed_integer_expression() -> None:
    source = """# @version 0.3.10
E18: constant(int256) = 10 ** 18

@external
def f(x: int256) -> uint256:
    return convert(E18 * E18 / (E18 + 10 * x), uint256)
"""

    result = apply_rules(source, config())

    assert "return convert(E18 * E18 // (E18 + 10 * x), uint256)" in result.source


def test_redundant_convert_to_same_integer_type() -> None:
    source = """# @version 0.3.10
PRECISION: constant(uint256) = 10**18

@external
def f() -> uint256:
    return convert(PRECISION, uint256)
"""

    result = apply_rules(source, config())

    assert "return PRECISION" in result.source
    assert any(fix.rule == "VY051" for fix in result.fixes)


def test_literal_convert_kept_for_abi_encoding_context() -> None:
    source = """# @version 0.3.10
@external
def f() -> Bytes[96]:
    return abi_encode(convert(0, uint256), method_id=method_id("deposit(uint256)"))
"""

    result = apply_rules(source, config())

    assert "abi_encode(convert(0, uint256), method_id=" in result.source


def test_redundant_convert_removed_from_constant_initializers() -> None:
    source = """# @version 0.3.10
ZERO: constant(uint256) = convert(0, uint256)
PRECISION_MUL: constant(uint256[2]) = [convert(1, uint256), convert(10 ** 6, uint256)]
"""

    result = apply_rules(source, config())

    assert "ZERO: constant(uint256) = 0" in result.source
    assert "PRECISION_MUL: constant(uint256[2]) = [1, 10 ** 6]" in result.source


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
    assert '@nonreentrant("a")' not in result.source
    assert '@nonreentrant("b")' not in result.source
    assert result.source.count("@nonreentrant") == 2


def test_create_from_blueprint_adds_code_offset_by_default() -> None:
    source = """# @version 0.3.10
@external
def f(target: address):
    create_from_blueprint(target)
"""

    result = apply_rules(source, config())

    assert "create_from_blueprint(target, code_offset=0)" in result.source
    assert any(fix.rule == "VY080" for fix in result.fixes)


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


def test_struct_keyword_constructor_reorders_to_declaration_order() -> None:
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


def test_struct_constructor_casts_integer_field_arguments() -> None:
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


def test_struct_literal_with_comments_is_left_source_preserving() -> None:
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


def test_loop_variable_type_annotation_for_self_storage_array() -> None:
    source = """# @version 0.3.10
queue: address[10]

@external
def f():
    for strategy in self.queue:
        pass
"""

    result = apply_rules(source, config())

    assert "for strategy: address in self.queue:" in result.source


def test_loop_variable_type_annotation_after_multiline_param_comment() -> None:
    source = """# @version 0.3.10
interface ERC20:
    def totalSupply() -> uint256: view

@external
def __init__(
    owner: address,  # admin
    accepted_tokens: DynArray[ERC20, 20],
):
    for token in accepted_tokens:
        pass
"""

    result = apply_rules(source, config())

    assert "for token: ERC20 in accepted_tokens:" in result.source


def test_loop_variable_type_annotation_for_literal_address_list() -> None:
    source = """# @version 0.3.10
@external
def f(_from: address, _to: address):
    for addr in [_from, _to]:
        pass
"""

    result = apply_rules(source, config())

    assert "for addr: address in [_from, _to]:" in result.source


def test_loop_variable_type_annotation_for_literal_interface_list() -> None:
    source = """# @version 0.3.10
interface Registry:
    def numTokens() -> uint256: view

@external
def f(registry_a: Registry, registry_b: Registry):
    for registry in [registry_a, registry_b]:
        n: uint256 = staticcall registry.numTokens()
"""

    result = apply_rules(source, config())

    assert "for registry: Registry in [registry_a, registry_b]:" in result.source


def test_loop_variable_type_annotation_for_literal_empty_address() -> None:
    source = """# @version 0.3.10
@external
def f(_gauge: address):
    for target in [_gauge, empty(address)]:
        pass
"""

    result = apply_rules(source, config())

    assert "for target: address in [_gauge, empty(address)]:" in result.source


def test_loop_variable_type_annotation_for_struct_array_attribute() -> None:
    source = """# @version 0.3.10
struct Action:
    target: address

struct Proposal:
    actions: DynArray[Action, 10]

proposals: HashMap[uint256, Proposal]

@external
def f(pid: uint256):
    for action in self.proposals[pid].actions:
        pass
"""

    result = apply_rules(source, config())

    assert "for action: Action in self.proposals[pid].actions:" in result.source


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


def test_internal_nonreentrant_removed_after_global_lock_migration() -> None:
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


def test_decimal_division_inside_integer_convert_is_not_rewritten() -> None:
    source = """# @version 0.3.10
@external
def f(x: decimal, y: decimal) -> uint256:
    z: uint256 = convert(x / y, uint256)
    return z
"""

    result = apply_rules(source, config())

    assert "convert(x / y, uint256)" in result.source
    assert "convert(x // y, uint256)" not in result.source
    assert any(diag.rule == "VYD004" for diag in result.diagnostics)


def test_dynamic_bytes_hex_literal_becomes_byte_string_literal() -> None:
    source = """# @version 0.3.10
@external
def f() -> Bytes[256]:
    call_data: Bytes[256] = 0x00
    return call_data
"""

    result = apply_rules(source, config())

    assert 'call_data: Bytes[256] = b"\\x00"' in result.source


def test_fixed_bytes_hex_literal_is_left_alone() -> None:
    source = """# @version 0.3.10
value: bytes32 = 0x00
"""

    result = apply_rules(source, config())

    assert "value: bytes32 = 0x00" in result.source


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


def test_range_runtime_stop_with_constant_delta_gets_bound_keyword() -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@external
def f():
    for i in range(MAX_COINS):
        for x in range(i, i + MAX_COINS):
            pass
"""

    result = apply_rules(source, config())

    assert "range(i, i + MAX_COINS, bound=8)" in result.source
    assert "for i: int128 in range" in result.source
    assert "for x: int128 in range" in result.source


def test_unsigned_range_bound_does_not_convert_existing_bound_keyword() -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@external
def f(i2: uint256):
    for x: uint256 in range(i2, i2 + MAX_COINS, bound=MAX_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "range(i2, i2 + convert(MAX_COINS, uint256), bound=MAX_COINS)" in result.source
    assert "bound=convert(MAX_COINS, uint256)" not in result.source


def test_range_loop_type_ignores_bound_keyword() -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 2

@external
def f():
    for i in range(N_COINS, bound=N_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "for i: int128 in range(N_COINS, bound=N_COINS):" in result.source


def test_range_loop_type_uses_signed_stop_after_literal_start() -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@external
def f():
    for i in range(1, MAX_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "for i: int128 in range(1, MAX_COINS):" in result.source


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


def test_sqrt_rewrite_skips_local_function_shadowing() -> None:
    source = """# @version ^0.4.0
@external
@pure
def sqrt(x: uint256) -> uint256:
    return x

@external
@pure
def f(x: uint256) -> uint256:
    return sqrt(x)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "def sqrt(x: uint256)" in result.source
    assert "def math.sqrt" not in result.source
    assert "return sqrt(x)" in result.source
    assert "import math" not in result.source


def test_sqrt_rewrite_skips_import_shadowing() -> None:
    source = """# @version ^0.4.0
from snekmate.utils import sqrt

@external
@pure
def f(x: uint256) -> uint256:
    return sqrt(x)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return sqrt(x)" in result.source
    assert "import math" not in result.source


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


def test_pragma_rewrite_bumps_to_enabled_target_version() -> None:
    source = """# @version 0.3.8
@external
def f():
    pass
"""

    before = apply_rules(source, config(target_version="0.3.9"))
    after = apply_rules(source, config(target_version="0.3.10"))

    assert "# @version 0.3.8" in before.source
    assert "#pragma version 0.3.10" in after.source


def test_pragma_updates_existing_pragma_version() -> None:
    source = """# pragma version 0.3.10
@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "#pragma version 0.4.3" in result.source


def test_pragma_does_not_report_unchanged_fix() -> None:
    source = """#pragma version 0.3.10
x: uint256
"""

    result = apply_rules(source, config(target_version="0.3.10"))

    assert result.source == source
    assert not [fix for fix in result.fixes if fix.rule == "VY001"]


def test_pragma_is_added_when_missing() -> None:
    source = """@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3", source_version="0.3.10"))

    assert result.source.startswith("#pragma version 0.4.3\n")


def test_missing_source_pragma_still_reports_unknown_source_version() -> None:
    source = """@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert result.source.startswith("#pragma version 0.4.3\n")
    assert [diagnostic.rule for diagnostic in result.diagnostics] == ["VYD005"]


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


def test_legacy_event_after_blank_line_does_not_add_blank_field_lines() -> None:
    source = """# @version 0.2.1

Transfer: event({_from: indexed(address), _to: indexed(address), _value: uint256})
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert (
        "event Transfer:\n"
        "    _from: indexed(address)\n"
        "    _to: indexed(address)\n"
        "    _value: uint256\n"
    ) in result.source
    assert "event Transfer:\n\n" not in result.source


def test_legacy_interface_storage_assignment_lowers_to_address() -> None:
    source = """# @version 0.1.0b16
contract ERC20m:
    def transfer(_to: address, _value: uint256) -> bool: modifying

token: ERC20m

@public
def __init__(_pool_token: address):
    self.token = ERC20m(_pool_token)

@public
def transfer(_to: address, _value: uint256):
    self.token.transfer(_to, _value)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "token: address" in result.source
    assert "self.token = _pool_token" in result.source
    assert "ERC20m(self.token).transfer(_to, _value)" in result.source


def test_inline_interface_comment_keeps_call_keyword_inference() -> None:
    source = """# pragma version 0.3.10
interface Controller:  # legacy deployment
    def liquidate(user: address): nonpayable

controller: address

@external
def run(user: address):
    Controller(self.controller).liquidate(user)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "extcall Controller(self.controller).liquidate(user)" in result.source


def test_shifted_method_id_uses_bytes4_output_type() -> None:
    source = """# pragma version 0.3.1
@external
def f() -> uint256:
    return convert(method_id("callback(address)", output_type=bytes32), uint256) << 224
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert 'method_id("callback(address)")' in result.source
    assert "output_type=bytes32" not in result.source


def test_chained_call_after_staticcall_gets_keyword() -> None:
    source = """# pragma version 0.3.10
interface Bridger:
    def check(addr: address) -> bool: view

interface Gauge:
    def bridger() -> Bridger: view

@external
def f(gauge: Gauge) -> bool:
    return gauge.bridger().check(msg.sender)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "return staticcall (staticcall gauge.bridger()).check(msg.sender)" in result.source


def test_struct_array_interface_call_gets_keyword() -> None:
    source = """# pragma version 0.3.10
interface ERC20:
    def decimals() -> uint256: view
    def transfer(_to: address, _value: uint256) -> bool: nonpayable

struct SwapData:
    coins: DynArray[ERC20, 5]

@internal
@view
def decimals(data: SwapData, i: uint256) -> uint256:
    return data.coins[i].decimals()

@external
def send(data: SwapData, i: uint256, receiver: address):
    assert data.coins[i].transfer(receiver, 1)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "return staticcall data.coins[i].decimals()" in result.source
    assert "assert extcall data.coins[i].transfer(receiver, 1)" in result.source


def test_pure_function_with_view_external_call_becomes_view() -> None:
    source = """# pragma version 0.3.10
interface Coin:
    def token() -> address: view

@internal
@pure
def burns_to(coin: Coin) -> address:
    return coin.token()
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "@view\ndef burns_to" in result.source
    assert "return staticcall coin.token()" in result.source


def test_signed_hashmap_key_is_not_rewritten_to_uint_index() -> None:
    source = """# pragma version 0.3.10
values: HashMap[int128, uint256]

@external
def get(period: int128) -> uint256:
    return self.values[period]
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "self.values[period]" in result.source
    assert "convert(period, uint256)" not in result.source


def test_signed_storage_hashmap_key_ignores_shadowing_local_name() -> None:
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


def test_integer_constant_initializer_casts_to_declared_type() -> None:
    source = """# pragma version 0.3.10
N_COINS: constant(uint256) = 3
PRICE_SIZE: constant(uint128) = 256 / (N_COINS - 1)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "PRICE_SIZE: constant(uint128) = 128" in result.source


def test_unsigned_loop_assignment_to_signed_local_is_converted() -> None:
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


def test_legacy_map_rewrite_handles_nested_map_type() -> None:
    source = """allowances: map(address, map(address, uint256))
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "allowances: HashMap[address, HashMap[address, uint256]]" in result.source


def test_legacy_map_rewrite_handles_subscript_map_type() -> None:
    source = """balances: uint256[address]
allowances: (uint256[address])[address]
id_to_token: address[uint256]
fixed_coins: address[2]
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "balances: HashMap[address, uint256]" in result.source
    assert "allowances: HashMap[address, HashMap[address, uint256]]" in result.source
    assert "id_to_token: HashMap[uint256, address]" in result.source
    assert "fixed_coins: address[2]" in result.source


def test_legacy_interface_signature_mutability_rewrites() -> None:
    source = """contract Controller():
    def gauges(gauge_id: int128) -> address: constant

contract Token:
    def mint(_to: address, _value: uint256): modifying
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "interface Controller:" in result.source
    assert "def gauges(gauge_id: int128) -> address: view" in result.source
    assert "def mint(_to: address, _value: uint256): nonpayable" in result.source


def test_legacy_address_interface_type_rewrites_to_imported_interface() -> None:
    source = """# @version 0.1.0b4
interface Factory:
    def getExchange(token: address) -> address: view

token: address(ERC20)

@public
@constant
def balance() -> uint256:
    return self.token.balanceOf(self)

@public
@constant
def factoryAddress() -> address(Factory):
    return empty(address)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "from ethereum.ercs import IERC20" in result.source
    assert "token: address" in result.source
    assert "def factoryAddress() -> address:" in result.source
    assert "staticcall IERC20(self.token).balanceOf(self)" in result.source


def test_legacy_interface_value_calls_rewrite_interface_payable() -> None:
    source = """# @version 0.1.0b4
contract Exchange():
    def ethToTokenTransferInput(min_tokens: uint256, deadline: timestamp, recipient: address) -> uint256: modifying

@public
def f(exchange_addr: address, value: uint256):
    Exchange(exchange_addr).ethToTokenTransferInput(1, block.timestamp, msg.sender, value=value)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "def ethToTokenTransferInput(min_tokens: uint256, deadline: uint256, recipient: address) -> uint256: payable" in result.source
    assert "extcall Exchange(exchange_addr).ethToTokenTransferInput" in result.source


def test_legacy_interface_storage_type_rewrites_to_address_with_casts() -> None:
    source = """# @version 0.1.0b4
contract Factory():
    def getExchange(token: address) -> address: constant

factory: Factory

@public
@constant
def f(token: address) -> address:
    assert self.factory != ZERO_ADDRESS
    return self.factory.getExchange(token)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "factory: address" in result.source
    assert "assert self.factory != empty(address)" in result.source
    assert "staticcall Factory(self.factory).getExchange(token)" in result.source


def test_legacy_create_with_code_of_renames_to_create_copy_of() -> None:
    source = """# @version 0.1.0b4
template: address

@public
def create() -> address:
    return create_with_code_of(self.template)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "create_copy_of(self.template)" in result.source
    assert any(fix.rule == "VY208" for fix in result.fixes)


def test_legacy_timestamp_type_rewrites_in_type_positions() -> None:
    source = """event CommitNewAdmin:
    deadline: indexed(timestamp)

event Start:
    timestamp: uint256

struct Ramp:
    initial_time: timestamp

period_timestamp: public(HashMap[int128, timestamp])
last_epoch_time: public(timestamp)

@external
def f() -> timestamp:
    log Start(timestamp=block.timestamp)
    self.point_history[0] = Point({ts: block.timestamp})
    return block.timestamp
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "deadline: indexed(uint256)" in result.source
    assert "timestamp: uint256" in result.source
    assert "log Start(timestamp=block.timestamp)" in result.source
    assert "Point({ts: block.timestamp})" in result.source
    assert "initial_time: uint256" in result.source
    assert "period_timestamp: public(HashMap[int128, uint256])" in result.source
    assert "last_epoch_time: public(uint256)" in result.source
    assert "def f() -> uint256:" in result.source
    assert "return block.timestamp" in result.source


def test_legacy_as_unitless_number_is_unwrapped() -> None:
    source = """# @version 0.2.1
@external
def f(start: uint256) -> uint256:
    return as_unitless_number(block.timestamp - start)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "return block.timestamp - start" in result.source
    assert "as_unitless_number" not in result.source


def test_reserved_value_parameter_is_renamed_with_function_references() -> None:
    source = """# @version 0.2.1
@internal
def _deposit_for(addr: address, value: uint256):
    self.balance += value
    log Deposit(addr, value=value)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "def _deposit_for(addr: address, _value: uint256):" in result.source
    assert "self.balance += _value" in result.source
    assert "log Deposit(addr, value=_value)" in result.source
    assert any(fix.rule == "VY212" for fix in result.fixes)


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


def test_shift_builtin_rewrites_positive_convert_amount() -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, i: int128) -> uint256:
    return shift(x, convert(i * 8, int128))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x << convert(i * 8, uint256))" in result.source


def test_shift_builtin_rewrites_negative_dynamic_amount() -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, i: uint256) -> uint256:
    return shift(x, -8 * i)
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x >> (8 * i))" in result.source


def test_shift_builtin_casts_signed_dynamic_amount_before_rewrite() -> None:
    source = """# @version 0.3.10
@external
def f(x: uint256) -> uint256:
    total: uint256 = 0
    for i: int128 in range(4):
        total += shift(x, -8 * i)
    return total
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "total += (x >> (8 * convert(i, uint256)))" in result.source


def test_shift_builtin_rewrites_negative_signed_convert_amount_to_unsigned() -> None:
    source = """# @version 0.4.1
@external
def f(x: uint256, i: uint256) -> uint256:
    return shift(x, -128 * convert(i - 1, int256))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x >> (128 * i - 1))" not in result.source
    assert "return (x >> (128 * (i - 1)))" in result.source


def test_shift_builtin_folds_constants_inside_dynamic_amount() -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 3
PRICE_SIZE: constant(int128) = 256 // (N_COINS - 1)

@external
def f(x: uint256) -> uint256:
    total: uint256 = 0
    for i in range(1, N_COINS):
        total += shift(x, -PRICE_SIZE * convert(i - 1, int256))
    return total
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "total += (x >> (128 * convert(i - 1, uint256)))" in result.source
    assert "PRICE_SIZE * (i - 1)" not in result.source


def test_shift_builtin_rewrites_signed_constant_amounts() -> None:
    source = """# @version 0.4.1
BAL_SHIFT: constant(int128) = -16

@external
def pack(x: uint256) -> uint256:
    return shift(x, -BAL_SHIFT)

@external
def unpack(x: uint256) -> uint256:
    return shift(x, BAL_SHIFT)

@external
def unpack_converted(x: uint256) -> uint256:
    return shift(x, convert(BAL_SHIFT, uint256))
"""

    result = apply_rules(source, config(target_version="0.4.2"))

    assert "return (x << 16)" in result.source
    assert result.source.count("return (x >> 16)") == 2


def test_shift_amount_constants_are_not_cast_before_shift_rewrite() -> None:
    source = """# @version 0.3.10
PREVIOUS_SHIFT: constant(int128) = -120
EPOCH_SHIFT: constant(int128) = -240

@external
def pack(previous: uint256, epoch: uint256) -> uint256:
    return shift(previous, -PREVIOUS_SHIFT) | shift(epoch, -EPOCH_SHIFT)

@external
def unpack(packed: uint256, epoch: uint256) -> uint256:
    if epoch < shift(packed, EPOCH_SHIFT):
        return shift(packed, PREVIOUS_SHIFT)
    return 0
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "convert(PREVIOUS_SHIFT, uint256)" not in result.source
    assert "convert(EPOCH_SHIFT, uint256)" not in result.source
    assert "return (previous << 120) | (epoch << 240)" in result.source
    assert "if epoch < (packed >> 240):" in result.source
    assert "return (packed >> 120)" in result.source


def test_legacy_method_id_output_type_is_removed() -> None:
    source = """# @version 0.2.1
SIG: constant(bytes4) = method_id("transfer(address,uint256)", output_type=bytes4)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert 'method_id("transfer(address,uint256)")' in result.source
    assert "output_type=bytes4" not in result.source
    assert any(fix.rule == "VY209" for fix in result.fixes)


def test_legacy_method_id_bytes32_comparison_converts_to_bytes4() -> None:
    source = """# @version 0.2.1
@external
def f(return_value: bytes32):
    assert return_value == method_id("onERC721Received(address,address,uint256,bytes)", output_type=bytes32)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert (
        'assert convert(return_value, bytes4) == method_id("onERC721Received(address,address,uint256,bytes)")'
        in result.source
    )
    assert "output_type=" not in result.source


def test_constructor_nonreentrant_is_removed_before_deploy_rewrite() -> None:
    source = """# @version 0.2.15
@nonreentrant("lock")
@external
def __init__():
    pass
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "@nonreentrant" not in result.source
    assert "@deploy\ndef __init__" in result.source
    assert {fix.rule for fix in result.fixes} >= {"VY002", "VY210"}


def test_block_difficulty_alias_is_rewritten_when_crossing_0_3_7() -> None:
    source = """# @version 0.3.6
@external
def f() -> uint256:
    return block.difficulty
"""

    result = apply_rules(source, config(target_version="0.3.7"))

    assert "block.prevrandao" in result.source
    assert "block.difficulty" not in result.source
    assert any(fix.rule == "VY220" for fix in result.fixes)


def test_unary_plus_and_numeric_not_rewrite_when_crossing_0_3_8() -> None:
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


def test_numeric_not_unknown_type_is_diagnostic_only() -> None:
    source = """# @version 0.3.7
@external
def f():
    if not amount:
        pass
"""

    result = apply_rules(source, config(target_version="0.3.8"))

    assert "if not amount:" in result.source
    assert any(diag.rule == "VYD013" for diag in result.diagnostics)


def test_dynamic_single_argument_range_gets_bound_diagnostic_at_0_3_10() -> None:
    source = """# @version 0.3.9
@external
def f(stop: uint256):
    for i in range(stop):
        pass
"""

    result = apply_rules(source, config(target_version="0.3.10"))

    assert "range(stop, bound=" not in result.source
    assert any(diag.rule == "VYD014" for diag in result.diagnostics)


def test_not_in_comparator_rewrites_when_crossing_0_2_8() -> None:
    source = """# @version 0.2.7
@external
def f(x: uint256, values: uint256[3]) -> bool:
    return not (x in values)
"""

    result = apply_rules(source, config(target_version="0.2.8"))

    assert "return x not in values" in result.source
    assert any(fix.rule == "VY211" for fix in result.fixes)


def test_legacy_0_2_1_hard_cases_emit_diagnostics() -> None:
    source = '''# @version 0.2.1
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
'''

    result = apply_rules(source, config(target_version="0.2.1"))
    rules = {diag.rule for diag in result.diagnostics}

    assert {"VYD210", "VYD212", "VYD213", "VYD214", "VYD215"} <= rules
    assert "VYD211" not in rules
    assert "def f(_value: int128" in result.source


def test_early_beta_syntax_cleanup_rewrites_safe_forms() -> None:
    source = '''# @version 0.1.0b4
payload: bytes <= 32
amounts: num[3]
counter: num256
signed: signed256

@public
def f(data: bytes <= 32, amount: num128, target: address) -> uint256:
    x: uint256 = convert(amount, "uint256")
    digest: bytes32 = sha3(data)
    reset(self.counter)
    del self.signed
    raw_call(target, data, outsize=32)
    sliced: bytes <= 4 = slice(data, start=0, len=4)
    return as_wei_value(1, wei)
'''

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "payload: Bytes[32]" in result.source
    assert "amounts: int128[3]" in result.source
    assert "counter: uint256" in result.source
    assert "signed: int256" in result.source
    assert "data: Bytes[32]" in result.source
    assert "amount: int128" in result.source
    assert 'convert(amount, "uint256")' not in result.source
    assert "convert(amount, uint256)" in result.source
    assert "keccak256(data)" in result.source
    assert "clear(self.counter)" in result.source
    assert "clear(self.signed)" in result.source
    assert "max_outsize=32" in result.source
    assert "slice(data, 0, 4)" in result.source
    assert 'as_wei_value(1, "wei")' in result.source
    assert {fix.rule for fix in result.fixes} >= {"VY216", "VY217", "VY218", "VY219", "VY221"}


def test_early_beta_cleanup_skips_comments_strings_and_identifiers() -> None:
    source = '''# @version 0.1.0b4
note: String[64] = "sha3 convert(x, \\"uint256\\") reset bytes <= 32"
# sha3(convert(x, "uint256"))

@public
def f(num: uint256) -> uint256:
    return num
'''

    result = apply_rules(source, config(target_version="0.2.1"))

    assert '"sha3 convert(x, \\"uint256\\") reset bytes <= 32"' in result.source
    assert '# sha3(convert(x, "uint256"))' in result.source
    assert "def f(num: uint256)" in result.source
    assert "return num" in result.source
    assert not ({"VY216", "VY217", "VY218", "VY219", "VY221"} & {fix.rule for fix in result.fixes})


def test_nested_bare_import_is_diagnostic_when_crossing_0_4_1() -> None:
    source = """# @version 0.4.0
import sibling
import math
"""

    result = apply_rules(
        source,
        config(paths=(Path("contracts"),), target_version="0.4.1"),
        Path("contracts/subdir/foo.vy"),
    )

    assert [diag.rule for diag in result.diagnostics] == ["VYD015"]


def test_top_level_bare_import_is_not_absolute_relative_diagnostic() -> None:
    source = """# @version 0.4.0
import sibling
"""

    result = apply_rules(
        source,
        config(paths=(Path("contracts"),), target_version="0.4.1"),
        Path("contracts/foo.vy"),
    )

    assert not [diag for diag in result.diagnostics if diag.rule == "VYD015"]
