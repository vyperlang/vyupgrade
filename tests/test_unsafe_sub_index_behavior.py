"""Check that VY052 keeps a signed unsafe_sub array index's runtime behavior."""
from eth_abi import encode
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
from eth_utils import keccak

from vyupgrade import engine
from vyupgrade.models import Config


SOURCE = """# @version 0.3.10
items: uint256[2]

@external
def __init__():
    self.items = [11, 22]

@external
@view
def get(a: int256, b: int256) -> uint256:
    return self.items[unsafe_sub(a, b)]
"""


def _get(bytecode: str, a: int, b: int) -> int | None:
    vm = EthereumTester()
    sender = vm.get_accounts()[0]
    tx = vm.send_transaction({"from": sender, "gas": 1_000_000, "data": bytecode})
    receipt = vm.get_transaction_receipt(tx)
    assert receipt["status"] == 1
    data = keccak(text="get(int256,int256)")[:4] + encode(["int256", "int256"], [a, b])
    try:
        return int(vm.call({"from": sender, "to": receipt["contract_address"], "data": "0x" + data.hex()}), 16)
    except TransactionFailed:
        return None


def test_signed_unsafe_sub_index_keeps_results_for_negative_operands(tmp_path):
    path = tmp_path / "items.vy"
    path.write_text(SOURCE)
    config = Config(paths=(path,), target_version="0.4.3")
    batch = engine.prepare_migrations([engine.bounded_migration_request(path, SOURCE, config)], config)
    decision = engine.validate_migrations(batch, config)
    migrated = batch.files[0]
    assert decision.status == "passed", migrated.report.target_error

    source_bytecode = migrated.source_compile.artifacts["bytecode"]
    target_bytecode = migrated.target_compile.artifacts["bytecode"]
    for a, b, expected in [(-1, -2, 22), (1, 1, 11), (-2, -1, None), (0, 2, None)]:
        assert _get(source_bytecode, a, b) == expected
        assert _get(target_bytecode, a, b) == expected
