# @version 0.3.10
from vyper.interfaces import ERC20
from vyper.interfaces import ERC4626

implements: ERC4626

asset: public(ERC20)

event Deposit:
    depositor: indexed(address)
    receiver: indexed(address)
    assets: uint256
    shares: uint256

event Withdraw:
    withdrawer: indexed(address)
    receiver: indexed(address)
    owner: indexed(address)
    assets: uint256
    shares: uint256


@external
def __init__(_asset: ERC20):
    self.asset = _asset


@view
@external
def totalAssets() -> uint256:
    return 0


@view
@external
def convertToAssets(shareAmount: uint256) -> uint256:
    return shareAmount


@view
@external
def convertToShares(assetAmount: uint256) -> uint256:
    return assetAmount


@view
@external
def maxDeposit(owner: address) -> uint256:
    return 0


@view
@external
def previewDeposit(assets: uint256) -> uint256:
    return assets


@external
def deposit(assets: uint256, receiver: address = msg.sender) -> uint256:
    return assets


@view
@external
def maxMint(owner: address) -> uint256:
    return 0


@view
@external
def previewMint(shares: uint256) -> uint256:
    return shares


@external
def mint(shares: uint256, receiver: address = msg.sender) -> uint256:
    return shares


@view
@external
def maxWithdraw(owner: address) -> uint256:
    return 0


@view
@external
def previewWithdraw(assets: uint256) -> uint256:
    return assets


@external
def withdraw(
    assets: uint256,
    receiver: address = msg.sender,
    owner: address = msg.sender,
) -> uint256:
    return assets


@view
@external
def maxRedeem(owner: address) -> uint256:
    return 0


@view
@external
def previewRedeem(shares: uint256) -> uint256:
    return shares


@external
def redeem(
    shares: uint256,
    receiver: address = msg.sender,
    owner: address = msg.sender,
) -> uint256:
    return shares
