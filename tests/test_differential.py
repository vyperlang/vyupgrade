"""Execute the actual source and migrated bytecode in independent local EVMs."""
import itertools
from pathlib import Path

import pytest
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
from eth_utils import keccak

from vyupgrade import compiler, engine
from vyupgrade.models import Config
from vyupgrade.rules import apply_rules


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


def test_shift_amount_conversions_and_signs_preserve_executed_results(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.10
@external
@pure
def signed_cast(x: uint256, n: int256) -> uint256:
    return shift(x, convert(n, int128))

@external
@pure
def unsigned_cast(x: uint256, n: uint256) -> uint256:
    return shift(x, convert(n, int128))

@external
@pure
def negated_cast(x: uint256, n: uint256) -> uint256:
    return shift(x, -convert(n, int128))

@external
@pure
def local_offset(x: uint256, n: int128) -> uint256:
    offset: int128 = -8 * n
    return shift(x, offset)

@external
@pure
def negated_offset(x: uint256, n: int128) -> uint256:
    offset: int128 = -8 * n
    return shift(x, -offset)

@external
@pure
def signed_loop(x: uint256) -> uint256:
    total: uint256 = 0
    for i in range(-1, 3):
        total += shift(x, -8 * i)
    return total

@external
@pure
def high_constant(x: uint256) -> uint256:
    return shift(x, 115792089237316195423570985008687907853269984665640564039457584007913129639935)

@external
@pure
def conditional_value(x: uint256, n: uint256) -> uint256:
    return shift(x if n == 0 else n, 1)
""")
    cases = (
        ("signed_cast(uint256,int256)", (-2**127 - 1, -2**127, -7, 0, 7, 2**127 - 1, 2**127)),
        ("unsigned_cast(uint256,uint256)", (0, 7, 2**127 - 1, 2**127, 2**256 - 1)),
        ("negated_cast(uint256,uint256)", (0, 7, 2**127 - 1, 2**127, 2**256 - 1)),
        ("local_offset(uint256,int128)", (-2**127, -7, 0, 7, 2**127 - 1)),
        ("negated_offset(uint256,int128)", (-2**127, -7, 0, 7, 2**127 - 1)),
    )
    for signature, amounts in cases:
        for n in amounts:
            actual = [_outcome(d, signature, 2**256 - 1, n) for d in deployments]
            assert actual[0] == actual[1], (signature, n, actual)
    for x in (0, 1, 255):
        expected = (x << 8) + x + (x >> 8) + (x >> 16)
        assert [_call(d, "signed_loop(uint256)", x) for d in deployments] == [expected, expected]
        assert [_call(d, "high_constant(uint256)", x) for d in deployments] == [x >> 1, x >> 1]
        assert [_call(d, "conditional_value(uint256,uint256)", x, 0) for d in deployments] == [
            x << 1, x << 1
        ]


def test_interface_widening_changes_accepted_returns_and_requires_aggressive(tmp_path):
    source = """#pragma version 0.4.3
interface IFoo:
    def data() -> Bytes[1]: view

implements: IFoo

@external
@view
def data() -> Bytes[5]:
    return b"abcde"

@external
@view
def read(other: address) -> uint256:
    return len(staticcall IFoo(other).data())
"""
    default = apply_rules(source, Config(paths=(tmp_path,), target_version="0.5.0a3"))
    assert "def data() -> Bytes[1]: view" in default.source
    assert any(diag.rule == "VYD019" for diag in default.diagnostics)
    aggressive = apply_rules(
        source, Config(paths=(tmp_path,), target_version="0.5.0a3", aggressive=True)
    )
    path = tmp_path / "entry.vy"
    original = _deploy(path, source, "0.4.3")
    migrated = _deploy(path, aggressive.source, "0.5.0a3")
    with pytest.raises(TransactionFailed):
        _call(original, "read(address)", int(original[2], 16))
    assert _call(migrated, "read(address)", int(migrated[2], 16)) == 5


def test_shift_preserves_legacy_constant_coercion(tmp_path):
    source = """# @version 0.2.12
WIDTH: constant(int128) = 8

@external
@pure
def f(x: uint256, n: uint256) -> uint256:
    return shift(x, -WIDTH * convert(n, int256))
"""
    rewritten = apply_rules(source, Config(paths=(tmp_path,), target_version="0.4.3"))
    path = tmp_path / "entry.vy"
    original = _deploy(path, source, "0.2.12")
    migrated = _deploy(path, rewritten.source, "0.4.3")
    for n in (0, 1, 7, 31, 32, 2**255 - 1, 2**255):
        actual = [_outcome(d, "f(uint256,uint256)", 2**256 - 1, n) for d in (original, migrated)]
        assert actual[0] == actual[1], (n, actual)
