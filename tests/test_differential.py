"""Execute the actual source and migrated bytecode in independent local EVMs."""
import itertools
from pathlib import Path

import pytest
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
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


def _legacy_shift(x: int, n: int) -> int:
    """Reference model of legacy shift(): ``if slt(n, 0) then gshr(0 - n, x) else shl(n, x)``."""
    word = n % 2**256
    if word >= 2**255:
        return (x >> (2**256 - word)) % 2**256
    return 0 if word >= 256 else (x << word) % 2**256


def _outcome(deployment, signature: str, *args: int) -> int | str:
    try:
        return _call(deployment, signature, *args) % 2**256
    except TransactionFailed:
        return "revert"


def test_runtime_shift_amounts_preserve_executed_results(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.10
@external
@pure
def signed_amount(x: uint256, n: int256) -> uint256:
    return shift(x, n)

@external
@pure
def signed_value(x: int256, n: int128) -> int256:
    return shift(x, n)

@external
@pure
def uint256_amount(x: int256, n: uint256) -> int256:
    return shift(x, n)

@external
@pure
def narrow_amount(x: uint256, n: uint8) -> uint256:
    return shift(x, n)

@external
@pure
def negated_amount(x: uint256, n: int128) -> uint256:
    return shift(x, -n)
""")
    edge_amounts = (0, 1, 7, 255, 256, 257, -1, -7, -255, -256, -257)
    int128_amounts = (*edge_amounts, -(2**127), 2**127 - 1)
    cases = (
        ("signed_amount(uint256,int256)", (2**256 - 1, 0xF0 << 128), (*edge_amounts, -(2**255))),
        ("signed_value(int256,int128)", (-(2**255), -8, 8), int128_amounts),
        (
            "uint256_amount(int256,uint256)",
            (-8, 2**255 - 1),
            (0, 1, 255, 256, 2**255 - 1, 2**255, 2**256 - 256, 2**256 - 1),
        ),
        ("narrow_amount(uint256,uint8)", (2**256 - 1,), (0, 1, 255)),
    )
    for signature, values, amounts in cases:
        for x, n in itertools.product(values, amounts):
            expected = _legacy_shift(x, n)
            actual = [_outcome(deployment, signature, x, n) for deployment in deployments]
            assert actual == [expected, expected], (signature, x, n)
    for n in int128_amounts:
        # `-n` overflows int128 only at its minimum, before either version shifts.
        expected = "revert" if n == -(2**127) else _legacy_shift(2**256 - 1, -n)
        actual = [_outcome(d, "negated_amount(uint256,int128)", 2**256 - 1, n) for d in deployments]
        assert actual == [expected, expected], n
