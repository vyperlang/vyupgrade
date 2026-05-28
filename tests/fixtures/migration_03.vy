# @version 0.3.10

from vyper.interfaces import ERC20

interface Strategy:
    def totalAssets() -> uint256: view
    def withdraw(amount: uint256) -> uint256: nonpayable

struct Position:
    shares: uint256
    assets: uint256

token: public(ERC20)
MAX_BPS: constant(uint256) = 10_000

@external
def __init__():
    pass

@external
def migrate(strategy: Strategy, amount: uint256, price: uint256):
    shares: uint256 = amount / price
    position: Position = Position({shares: shares, assets: amount})
    balance: uint256 = self.token.balanceOf(msg.sender)
    total: uint256 = strategy.totalAssets()
    extcall strategy.withdraw(position.assets)
    for i in range(3):
        balance += i

