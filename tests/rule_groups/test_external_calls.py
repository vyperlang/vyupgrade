from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_ignored_external_call_result_is_assigned(config) -> None:
    source = """# @version 0.3.10
interface VeDelegation:
    def adjusted_balance_of(_account: address) -> uint256: view

@external
def set_delegation(delegation: address):
    VeDelegation(delegation).adjusted_balance_of(msg.sender)  # validation call
"""

    result = apply_rules(source, config())

    assert (
        "__vyupgrade_discard_7: uint256 = staticcall VeDelegation(delegation).adjusted_balance_of(msg.sender)  # validation call"
        in result.source
    )
    assert any(fix.rule == "VY057" for fix in result.fixes)


def test_ignored_external_call_results_ignore_non_code_text(config) -> None:
    source = '''# @version 0.3.10
interface Target:
    def balanceOf(owner: address) -> uint256: view

@external
def f(target: address):
    """
    staticcall Target(target).balanceOf(self)
    """
    note: String[64] = "staticcall Target(target).balanceOf(self)"
    # staticcall Target(target).balanceOf(self)
    staticcall Target(target).balanceOf(self)
'''
    selected = config(source_version="0.3.10", select=frozenset({"VY057"}))

    first = apply_rules(source, selected)
    second = apply_rules(first.source, selected)

    assert '"""\n    staticcall Target(target).balanceOf(self)\n    """' in first.source
    assert 'note: String[64] = "staticcall Target(target).balanceOf(self)"' in first.source
    assert "# staticcall Target(target).balanceOf(self)" in first.source
    assert "__vyupgrade_discard_12: uint256 = staticcall Target(target).balanceOf(self)" in first.source
    assert second.source == first.source


def test_ignored_external_call_without_return_stays_statement(config) -> None:
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


def test_extcall_added_for_interface_cast_with_string_subscript_target(config) -> None:
    source = """# @version 0.3.3
interface Registry:
    def set_governances(governance: DynArray[address, 10]) -> DynArray[uint256, 10]: nonpayable

targets: HashMap[String[32], address]

@external
def f(governance: address) -> uint256:
    ids: DynArray[uint256, 10] = Registry(self.targets["registry"]).set_governances([governance])
    return ids[0]
"""

    result = apply_rules(source, config())

    assert (
        'ids: DynArray[uint256, 10] = extcall Registry(self.targets["registry"]).set_governances([governance])'
        in result.source
    )


def test_external_call_inside_multiline_expression_is_not_discard_assigned(config) -> None:
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


def test_external_call_in_backslash_assignment_is_not_discard_assigned(config) -> None:
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


def test_ignored_staticcall_array_result_keeps_full_return_type(config) -> None:
    source = """# @version 0.3.10
interface Synth:
    def settle(key: bytes32) -> uint256[3]: view

@external
def f(target: address, key: bytes32):
    Synth(target).settle(key)
"""

    result = apply_rules(source, config())

    assert (
        "__vyupgrade_discard_7: uint256[3] = staticcall Synth(target).settle(key)" in result.source
    )


def test_lowercase_c_prefix_interface_calls(config) -> None:
    source = """# @version 0.2.4
interface cERC20:
    def balanceOf(owner: address) -> uint256: view

@external
def f(token: address) -> uint256:
    return cERC20(token).balanceOf(self)
"""

    result = apply_rules(source, config())

    assert "return staticcall cERC20(token).balanceOf(self)" in result.source


def test_nested_interface_cast_calls(config) -> None:
    source = """# @version 0.3.7
interface Token:
    def balanceOf(owner: address) -> uint256: view
    def allowance(owner: address, spender: address) -> uint256: view

@external
def f(token: address, owner: address) -> uint256:
    return min(Token(token).balanceOf(owner), Token(token).allowance(owner, self))
"""

    result = apply_rules(source, config())

    assert (
        "min(staticcall Token(token).balanceOf(owner), staticcall Token(token).allowance(owner, self))"
        in result.source
    )


def test_multiline_interface_method_mutability(config) -> None:
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


def test_legacy_interface_body_mutability_updates_keyword(config) -> None:
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


def test_immutable_interface_storage_var_calls(config) -> None:
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


def test_legacy_interface_header_calls(config) -> None:
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


def test_multiline_assert_interface_cast_call(config) -> None:
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

    assert (
        "assert extcall Token(self.coins[0])\\\n        .transferFrom(msg.sender, self, amount)"
        in result.source
    )


def test_multiline_interface_def_calls(config) -> None:
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


def test_nested_cast_expression_call(config) -> None:
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

    assert (
        "return staticcall Token(staticcall Vault(self.vault).asset()).balanceOf(self)"
        in result.source
    )


def test_external_call_subscript_parentheses(config) -> None:
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


def test_external_call_cast_subscript_parentheses(config) -> None:
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


def test_external_call_result_attribute_parentheses(config) -> None:
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


def test_interface_after_struct_keeps_method_mutability(config) -> None:
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


def test_external_call_on_interface_struct_field(config) -> None:
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


def test_external_call_on_typed_loop_variable(config) -> None:
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


def test_external_call_on_local_interface_variable_after_returning_call(config) -> None:
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


def test_external_call_on_aliased_builtin_interface_cast(config) -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC20Detailed as ERC20

@external
def f(token: address) -> uint8:
    return ERC20(token).decimals()
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC20Detailed as ERC20" in result.source
    assert "return staticcall ERC20(token).decimals()" in result.source


def test_external_call_on_lowercase_storage_interface(config) -> None:
    source = """# @version 0.3.7
interface iHoloYield:
    def balanceOf(owner: address) -> uint256: view

holoyield: iHoloYield

@external
def f() -> uint256:
    return self.holoyield.balanceOf(self)
"""

    result = apply_rules(source, config())

    assert "return staticcall self.holoyield.balanceOf(self)" in result.source


def test_external_call_interface_method_with_space_before_params(config) -> None:
    source = """# @version 0.3.7
interface Tax:
    def allowance (_spender: address, _to: address) -> uint256 : view

@internal
def allowance_bnb(_to: address) -> uint256:
    abi: Tax = Tax(0x64544969ed7EBf5f083679233325356EbE738930)
    return abi.allowance(_to, self)
"""

    result = apply_rules(source, config())

    assert "return staticcall abi.allowance(_to, self)" in result.source


def test_external_call_on_internal_call_returning_interface(config) -> None:
    source = """# @version 0.3.10
interface Staking:
    def getNodeRPLStake(node: address) -> uint256: view

@internal
def _staking() -> Staking:
    return Staking(0x0000000000000000000000000000000000000001)

@external
def f(node: address) -> uint256:
    return self._staking().getNodeRPLStake(node)
"""

    result = apply_rules(source, config())

    assert "return staticcall self._staking().getNodeRPLStake(node)" in result.source


def test_external_call_on_parenthesized_storage_interface(config) -> None:
    source = """# @version 0.3.10
interface ControlTower:
    def isStakingContract(addr: address) -> bool: view

control_tower: public(ControlTower)

@external
def f(addr: address):
    assert (self.control_tower).isStakingContract(addr)
"""

    result = apply_rules(source, config())

    assert "assert staticcall (self.control_tower).isStakingContract(addr)" in result.source


def test_struct_literal_field_does_not_shadow_interface_local(config) -> None:
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


def test_self_storage_interface_not_shadowed_by_later_parameter(config) -> None:
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


def test_pr_2938_extcall_and_staticcall_keywords(config) -> None:
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


def test_inline_interface_comment_keeps_call_keyword_inference(config) -> None:
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


def test_inline_interface_method_comment_keeps_call_keyword_inference(config) -> None:
    source = """# @version 0.3.10
struct Slot:
    tick: int24

interface Pool:
    def tickSpacing() -> int24: view # v3 tick spacing
    def slot0() -> Slot: view # v3 slot data

@external
@view
def f(target: address) -> int24:
    pool: Pool = Pool(target)
    slot: Slot = pool.slot0()
    return slot.tick + pool.tickSpacing()
"""

    result = apply_rules(source, config())

    assert "slot: Slot = staticcall pool.slot0()" in result.source
    assert "return slot.tick + staticcall pool.tickSpacing()" in result.source


def test_chained_call_after_staticcall_gets_keyword(config) -> None:
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


def test_struct_array_interface_call_gets_keyword(config) -> None:
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
