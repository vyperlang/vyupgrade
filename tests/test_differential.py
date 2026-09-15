"""Execute the actual source and migrated bytecode in independent local EVMs."""
from pathlib import Path

import pytest
from eth_tester import EthereumTester
from eth_utils import keccak

from vyupgrade import compiler, engine
from vyupgrade.models import Config


def _deploy(path: Path, source: str, version: str) -> tuple[EthereumTester, str, str]:
    path.write_text(source, encoding="utf-8")
    command, _ = compiler._prepare_command(None, version, None)
    result = compiler._run_compiler_process(
        [*command, "-f", "bytecode", str(path)],
        path,
        project_compiler=version == "0.3.10",
        compiler_timeout=120,
        network_timeout=300,
    )
    assert result.returncode == 0, result.stderr
    vm = EthereumTester()
    sender = vm.get_accounts()[0]
    transaction = vm.send_transaction({"from": sender, "gas": 3_000_000, "data": result.stdout.strip()})
    receipt = vm.get_transaction_receipt(transaction)
    assert receipt["status"] == 1
    return vm, sender, receipt["contract_address"]


def _migration_pair(tmp_path: Path, source: str):
    path = tmp_path / "entry.vy"
    path.write_text(source, encoding="utf-8")
    config = Config(paths=(path,), target_version="0.4.3")
    batch = engine.prepare_migrations([engine.bounded_migration_request(path, source, config)], config)
    decision = engine.validate_migrations(batch, config)
    assert decision.status == "passed", batch.reports
    target = batch.files[0].rewrite.source
    return _deploy(path, source, "0.3.10"), _deploy(path, target, "0.4.3")


def _call(deployment, signature: str, *args: int) -> int:
    vm, sender, address = deployment
    result = vm.call({"from": sender, "to": address, "data": _calldata(signature, args)})
    return int.from_bytes(bytes.fromhex(result[2:]), "big", signed=True)


def _calldata(signature: str, args: tuple[int, ...]) -> str:
    return "0x" + keccak(text=signature)[:4].hex() + "".join(
        (value % 2**256).to_bytes(32, "big").hex() for value in args
    )


@pytest.mark.parametrize(("expression", "expected"), [
    ("max_value(int128) % 7", 1),
    ("(-5) % 3", -2),
    ("5 % (-3)", 2),
    ("(-5) / 3", -1),
    ("5 / (-3)", -1),
])
def test_constant_rewrites_preserve_executed_results(tmp_path, expression, expected):
    source = f"""# @version 0.3.10
X: constant(int128) = {expression}
@external
@pure
def f() -> int128:
    return X
"""
    original, migrated = _migration_pair(tmp_path, source)
    assert _call(original, "f()") == _call(migrated, "f()") == expected


def test_rewrites_preserve_storage_transitions_and_reverts(tmp_path):
    original, migrated = _migration_pair(tmp_path, """# @version 0.3.10
counter: public(uint256)
@external
def step(x: int128) -> int128:
    self.counter += 1
    assert x != 0
    return x / 3
""")
    for value in (-5, 5, 0, -1):
        if value:
            assert _call(original, "step(int128)", value) == _call(migrated, "step(int128)", value)
        states = []
        for deployment in (original, migrated):
            vm, sender, address = deployment
            tx = vm.send_transaction({
                "from": sender, "to": address, "gas": 300_000,
                "data": _calldata("step(int128)", (value,)),
            })
            states.append((vm.get_transaction_receipt(tx)["status"], _call(deployment, "counter()")))
        assert states[0] == states[1]
        assert states[0][0] == int(value != 0)
    assert _call(original, "counter()") == 3
