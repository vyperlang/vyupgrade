from __future__ import annotations

from pathlib import Path

from vyupgrade.rules import apply_rules


def test_implements_declarations_merge_for_0_5_alpha_target(config) -> None:
    source = """#pragma version 0.4.3
implements: IERC20
implements: IERC4626

@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.5.0a1"))

    assert "implements: (IERC20, IERC4626)" in result.source
    assert "implements: IERC20\nimplements: IERC4626" not in result.source
    assert any(fix.rule == "VY121" for fix in result.fixes)


def test_duplicate_implements_declarations_collapse_for_0_5_alpha_target(config) -> None:
    source = """#pragma version 0.4.3
implements: IERC20
implements: IERC20

@external
def f():
    pass
"""

    result = apply_rules(source, config(target_version="0.5.0a1"))

    assert result.source.count("implements: IERC20") == 1
    assert any(fix.rule == "VY121" for fix in result.fixes)


def test_interface_defaults_become_ellipsis_for_0_5_alpha_target(config) -> None:
    source = """#pragma version 0.4.3
interface Vault:
    def deposit(amount: uint256 = 0, receiver: address = msg.sender): nonpayable
"""

    result = apply_rules(source, config(target_version="0.5.0a1"))

    assert "amount: uint256 = ..." in result.source
    assert "receiver: address = ..." in result.source
    assert any(fix.rule == "VY122" for fix in result.fixes)


def test_modern_erc_interface_imports(config) -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC4626, ERC721

asset: public(ERC4626)
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC4626, IERC721" in result.source
    assert "asset: public(IERC4626)" in result.source


def test_modern_erc_interface_imports_alias_when_new_name_exists(config) -> None:
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


def test_modern_erc_interface_imports_preserve_existing_alias(config) -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC20 as ERC20Spec

implements: ERC20Spec
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC20 as ERC20Spec" in result.source
    assert "implements: ERC20Spec" in result.source


def test_legacy_implemented_erc721_import_becomes_local_interface(config) -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC721

implements: ERC721

@view
@external
def balanceOf(_owner: address) -> uint256:
    return 0

@view
@external
def ownerOf(_tokenId: uint256) -> address:
    return empty(address)

@view
@external
def getApproved(_tokenId: uint256) -> address:
    return empty(address)

@view
@external
def isApprovedForAll(_owner: address, _operator: address) -> bool:
    return False

@external
def transferFrom(_from: address, _to: address, _tokenId: uint256):
    pass

@external
def safeTransferFrom(
    _from: address, _to: address, _tokenId: uint256, _data: Bytes[1024] = b""
):
    pass

@external
def approve(_approved: address, _tokenId: uint256):
    pass

@external
def setApprovalForAll(_operator: address, _approved: bool):
    pass
"""

    result = apply_rules(source, config())

    assert "from ethereum.ercs import IERC721" not in result.source
    assert "interface ERC721:" in result.source
    assert "def safeTransferFrom" in result.source
    assert "implements: ERC721" in result.source
    assert any(fix.rule == "VY020" for fix in result.fixes)


def test_legacy_implemented_erc721_import_preserves_payable_methods(config) -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC721

implements: ERC721

@view
@external
def balanceOf(_owner: address) -> uint256:
    return 0

@view
@external
def ownerOf(_tokenId: uint256) -> address:
    return empty(address)

@view
@external
def getApproved(_tokenId: uint256) -> address:
    return empty(address)

@view
@external
def isApprovedForAll(_owner: address, _operator: address) -> bool:
    return False

@external
@payable
def transferFrom(_from: address, _to: address, _tokenId: uint256):
    pass

@external
@payable
def safeTransferFrom(
    _from: address, _to: address, _tokenId: uint256, _data: Bytes[1024] = b""
):
    pass

@external
@payable
def approve(_approved: address, _tokenId: uint256):
    pass

@external
def setApprovalForAll(_operator: address, _approved: bool):
    pass
"""

    result = apply_rules(source, config())

    assert "def transferFrom(_from: address, _to: address, _tokenId: uint256): payable" in result.source
    assert "def safeTransferFrom" in result.source
    assert "Bytes[1024] = b\"\"): payable" in result.source
    assert "def approve(_approved: address, _tokenId: uint256): payable" in result.source
    assert (
        "def setApprovalForAll(_operator: address, _approved: bool): nonpayable"
        in result.source
    )


def test_pure_implemented_erc165_method_preserves_legacy_mutability(config) -> None:
    source = """# @version 0.3.3
from vyper.interfaces import ERC165

implements: ERC165

@pure
@external
def supportsInterface(interface_id: bytes4) -> bool:
    return True
"""

    result = apply_rules(source, config())

    assert "interface ERC165:" in result.source
    assert "def supportsInterface(interface_id: bytes4) -> bool: pure" in result.source
    assert "@pure\n@external\ndef supportsInterface" in result.source
    assert not any(fix.rule == "VY014" for fix in result.fixes)


def test_pure_local_interface_view_implementation_becomes_view(config) -> None:
    source = """# @version 0.3.3
interface ERC165:
    def supportsInterface(interface_id: bytes4) -> bool: view

implements: ERC165

@pure
@external
def supportsInterface(interface_id: bytes4) -> bool:
    return True
"""

    result = apply_rules(source, config())

    assert "@view\n@external\ndef supportsInterface" in result.source
    assert any(fix.rule == "VY014" for fix in result.fixes)


def test_snekmate_create2_address_import_renamed(config) -> None:
    source = """# @version 0.4.0
from snekmate.utils import create2_address

@external
def f(salt: bytes32, init_hash: bytes32, factory: address) -> address:
    return create2_address._compute_address(salt, init_hash, factory)
"""

    result = apply_rules(source, config())

    assert "from snekmate.utils import create2" in result.source
    assert "create2._compute_create2_address(salt, init_hash, factory)" in result.source
    assert any(fix.rule == "VY018" for fix in result.fixes)


def test_erc4626_builtin_calls(config) -> None:
    source = """# @version 0.3.10
from vyper.interfaces import ERC4626

@external
def f(vault: address) -> uint256:
    return ERC4626(vault).convertToAssets(10**18)
"""

    result = apply_rules(source, config())

    assert "return staticcall IERC4626(vault).convertToAssets(10**18)" in result.source


def test_immutable_accessor_collision_renames_backing_variable(config) -> None:
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


def test_immutable_accessor_collision_preserves_keyword_and_attribute_names(config) -> None:
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


def test_immutable_accessor_collision_avoids_existing_local_names(config) -> None:
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


def test_constant_accessor_collision_renames_backing_variable(config) -> None:
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


def test_constant_accessor_collision_handles_uppercase_names(config) -> None:
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


def test_local_interface_nonpayable_matches_view_function(config) -> None:
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


def test_local_interface_nonpayable_matches_public_getter(config) -> None:
    source = """# @version 0.3.10
interface RateProvider:
    def rate(_asset: address) -> uint256: nonpayable

implements: RateProvider

rate: public(HashMap[address, uint256])
"""

    result = apply_rules(source, config())

    assert "def rate(_asset: address) -> uint256: view" in result.source
    assert any(fix.rule == "VY014" for fix in result.fixes)


def test_pure_function_reading_immutable_becomes_view(config) -> None:
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


def test_internal_pure_function_reading_immutable_becomes_view(config) -> None:
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


def test_pure_function_without_immutable_read_stays_pure(config) -> None:
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


def test_view_function_emitting_log_becomes_nonpayable(config) -> None:
    source = """# @version 0.3.10
event Redemption:
    amount: uint256

@view
@internal
def amount_claimable(amount: uint256) -> uint256:
    log Redemption(amount=amount)
    return amount
"""

    result = apply_rules(source, config())

    assert "@view" not in result.source
    assert "@internal\ndef amount_claimable" in result.source
    assert any(fix.rule == "VY017" for fix in result.fixes)


def test_view_function_calling_log_helper_becomes_nonpayable(config) -> None:
    source = """# @version 0.3.10
event Redemption:
    amount: uint256

@view
@internal
def amount_claimable(amount: uint256) -> uint256:
    log Redemption(amount=amount)
    return amount

@view
@external
def redeemable(amount: uint256) -> uint256:
    return self.amount_claimable(amount)
"""

    result = apply_rules(source, config())

    assert "@view" not in result.source
    assert "@internal\ndef amount_claimable" in result.source
    assert "@external\ndef redeemable" in result.source
    assert sum(fix.rule == "VY017" for fix in result.fixes) == 2


def test_view_function_without_log_stays_view(config) -> None:
    source = """# @version 0.3.10
@view
@external
def amount_claimable(amount: uint256) -> uint256:
    return amount
"""

    result = apply_rules(source, config())

    assert "@view\n@external\ndef amount_claimable" in result.source
    assert not any(fix.rule == "VY017" for fix in result.fixes)


def test_legacy_numeric_constants(config) -> None:
    source = """# @version 0.3.3
@external
def f(amount: uint256 = MAX_UINT256) -> bool:
    return amount == MAX_UINT256
"""

    result = apply_rules(source, config())

    assert "amount: uint256 = max_value(uint256)" in result.source
    assert "amount == max_value(uint256)" in result.source
    assert (
        "ZERO_ADDRESS"
        not in apply_rules("# @version 0.3.3\nx: address = ZERO_ADDRESS\n", config()).source
    )


def test_pure_static_raw_call_relaxes_to_view(config) -> None:
    source = """# @version 0.3.10
IDENTITY_PRECOMPILE: constant(address) = 0x0000000000000000000000000000000000000004

@pure
@internal
def f(value: Bytes[1]) -> Bytes[1]:
    return raw_call(IDENTITY_PRECOMPILE, value, max_outsize=1, is_static_call=True)
"""

    result = apply_rules(source, config())

    assert "@view\n@internal\ndef f" in result.source


def test_pure_function_with_view_external_call_becomes_view(config) -> None:
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


def test_nested_bare_import_is_diagnostic_when_crossing_0_4_1(config) -> None:
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


def test_top_level_bare_import_is_not_absolute_relative_diagnostic(config) -> None:
    source = """# @version 0.4.0
import sibling
"""

    result = apply_rules(
        source,
        config(paths=(Path("contracts"),), target_version="0.4.1"),
        Path("contracts/foo.vy"),
    )

    assert not [diag for diag in result.diagnostics if diag.rule == "VYD015"]
