from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_legacy_interface_storage_assignment_lowers_to_address(config) -> None:
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


def test_legacy_map_rewrite_handles_nested_map_type(config) -> None:
    source = """allowances: map(address, map(address, uint256))
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "allowances: HashMap[address, HashMap[address, uint256]]" in result.source


def test_legacy_map_rewrite_handles_subscript_map_type(config) -> None:
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


def test_legacy_interface_signature_mutability_rewrites(config) -> None:
    source = """contract Controller():
    def gauges(gauge_id: int128) -> address: constant

contract Token:
    def mint(_to: address, _value: uint256): modifying
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert "interface Controller:" in result.source
    assert "def gauges(gauge_id: int128) -> address: view" in result.source
    assert "def mint(_to: address, _value: uint256): nonpayable" in result.source


def test_legacy_address_interface_type_rewrites_to_imported_interface(config) -> None:
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


def test_legacy_interface_value_calls_rewrite_interface_payable(config) -> None:
    source = """# @version 0.1.0b4
contract Exchange():
    def ethToTokenTransferInput(min_tokens: uint256, deadline: timestamp, recipient: address) -> uint256: modifying

@public
def f(exchange_addr: address, value: uint256):
    Exchange(exchange_addr).ethToTokenTransferInput(1, block.timestamp, msg.sender, value=value)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert (
        "def ethToTokenTransferInput(min_tokens: uint256, deadline: uint256, recipient: address) -> uint256: payable"
        in result.source
    )
    assert "extcall Exchange(exchange_addr).ethToTokenTransferInput" in result.source


def test_legacy_interface_storage_type_rewrites_to_address_with_casts(config) -> None:
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
