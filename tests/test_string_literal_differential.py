import pytest
from eth_abi import decode, encode
from eth_tester import EthereumTester
from eth_utils import keccak

from vyupgrade import engine
from vyupgrade.models import Config


@pytest.mark.parametrize("source_version", ["0.3.1", "0.3.7", "0.3.10"])
def test_non_ascii_return_and_revert_bytes_survive_migration(tmp_path, source_version):
    source = rf'''# @version {source_version}
@external
@pure
def text() -> String[8]:
    return (
        "é\xff"
    )

@external
def fail():
    raise "é\xff"

@external
def guard():
    assert msg.sender != self, "é\xff"

@external
def observe(check_guard: bool) -> Bytes[100]:
    selector: Bytes[4] = method_id("fail()")
    if check_guard:
        selector = method_id("guard()")
    ok: bool = False
    data: Bytes[100] = b""
    ok, data = raw_call(self, selector, max_outsize=100, revert_on_failure=False)
    assert not ok
    return data
'''
    path = tmp_path / "reasons.vy"
    path.write_text(source)
    config = Config(paths=(path,), target_version="0.4.3")
    request = engine.bounded_migration_request(path, source, config)
    batch = engine.prepare_migrations([request], config)
    assert engine.validate_migrations(batch, config).status == "passed", batch.reports
    migration = batch.files[0]
    assert [fix.rule for fix in migration.rewrite.fixes].count("VY224") == 3

    # Decode as bytes because the legacy payload is not valid UTF-8.
    payload = b"\xe9\xff"
    revert_data = keccak(text="Error(string)")[:4] + encode(["bytes"], [payload])
    cases = [
        (keccak(text="text()")[:4], payload),
        (keccak(text="observe(bool)")[:4] + encode(["bool"], [False]), revert_data),
        (keccak(text="observe(bool)")[:4] + encode(["bool"], [True]), revert_data),
    ]
    for result in (migration.source_compile, migration.target_compile):
        vm = EthereumTester()
        sender = vm.get_accounts()[0]
        tx = vm.send_transaction({
            "from": sender, "gas": 3_000_000, "data": result.artifacts["bytecode"],
        })
        receipt = vm.get_transaction_receipt(tx)
        assert receipt["status"] == 1
        for calldata, expected in cases:
            out = vm.call({
                "from": sender, "to": receipt["contract_address"], "data": "0x" + calldata.hex(),
            })
            assert decode(["bytes"], bytes.fromhex(out[2:])) == (expected,)
