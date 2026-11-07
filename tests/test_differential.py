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
    return _deploy(path, source, batch.files[0].source_compiler), _deploy(path, target, "0.4.3")


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


def test_interface_wildcard_keeps_and_widening_changes_accepted_returns(tmp_path):
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
    # Widening breaks this narrower caller at compile time, so only the wildcard
    # migration gets it.
    assigned = """
@external
@view
def assigned(other: address) -> uint256:
    value: Bytes[1] = staticcall IFoo(other).data()
    return len(value)

@external
@view
def wide(other: address) -> uint256:
    value: Bytes[10] = staticcall IFoo(other).data()
    return len(value)
"""
    wildcard = apply_rules(
        source + assigned, Config(paths=(tmp_path,), target_version="0.5.0a3")
    )
    assert "def data() -> Bytes[...]: view" in wildcard.source
    widened = apply_rules(
        source,
        Config(
            paths=(tmp_path,),
            target_version="0.5.0a3",
            aggressive=True,
            ignore=frozenset({"VY125"}),
        ),
    )
    assert "def data() -> Bytes[5]: view" in widened.source
    path = tmp_path / "entry.vy"
    original = _deploy(path, source + assigned, "0.4.3")
    kept = _deploy(path, wildcard.source, "0.5.0a3")
    changed = _deploy(path, widened.source, "0.5.0a3")
    for deployment in (original, kept):
        for signature in ("read(address)", "assigned(address)", "wide(address)"):
            with pytest.raises(TransactionFailed):
                _call(deployment, signature, int(deployment[2], 16))
    assert _call(changed, "read(address)", int(changed[2], 16)) == 5


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


def test_internal_guard_rejects_reentrant_callback(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.10
@external
def f(n: uint256) -> bool:
    return self._guard(n)

@internal
@nonreentrant("lock")
def _guard(n: uint256) -> bool:
    if n == 0:
        return True
    return raw_call(self, concat(method_id("f(uint256)"), empty(bytes32)), revert_on_failure=False)
""")
    assert [_call(d, "f(uint256)", 1) for d in deployments] == [0] * 2


def test_raw_call_preserves_literal_payload(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.10
@external
def f() -> uint256:
    return convert(raw_call(self, b"outsize=", max_outsize=32), uint256)

@external
def __default__() -> uint256:
    return len(msg.data)
""")
    assert [_call(d, "f()") for d in deployments] == [8] * 2


def test_bitwise_operators_preserve_operand_grouping(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.10
@external
@pure
def f(x: uint256, y: uint256) -> uint256:
    return bitwise_not(x + y)

@external
@pure
def both(x: uint256, y: uint256) -> uint256:
    return bitwise_and(x | y, x | 4)
""")
    assert [_call(d, "f(uint256,uint256)", 1, 2) for d in deployments] == [-4] * 2
    assert [_call(d, "both(uint256,uint256)", 1, 2) for d in deployments] == [1] * 2


def test_boundary_folding_respects_modulo_precedence(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.10
@external
@pure
def f() -> int128:
    return 2**127 - 1 % 2
""")
    assert [_call(d, "f()") for d in deployments] == [2**127 - 1] * 2


def test_array_comparison_evaluates_index_once(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.1
items: uint256[2][2]
num: uint256
@internal
def idx() -> uint256:
    self.num += 1
    return 0
@external
def f(skip: bool) -> uint256:
    assert skip or self.items[self.idx()] == empty(uint256[2])
    return self.num
""")
    for skip, calls in ((True, 0), (False, 1)):
        assert [_call(d, "f(bool)", skip) for d in deployments] == [calls] * 2


def test_shift_amount_uses_current_branch_type(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.10
@external
@pure
def f(x: uint256, n: uint256, wide: bool) -> uint256:
    if wide:
        amount: uint256 = n
        return shift(x, amount + 0)
    else:
        amount: uint8 = 1
        return shift(x, amount)
""")
    for wide, expected in ((True, 8), (False, 32)):
        assert [
            _call(d, "f(uint256,uint256,bool)", 16, 2**256 - 1, wide) for d in deployments
        ] == [expected] * 2


def test_method_id_preserves_all_32_bytes(tmp_path):
    deployments = _migration_pair(tmp_path, """# @version 0.3.1
@external
@pure
def f(data: bytes32) -> bool:
    return data == method_id("transfer(address,uint256)", output_type=bytes32)
""")
    selector = int.from_bytes(keccak(text="transfer(address,uint256)")[:4], "big")
    for word in (selector, selector << 224, selector << 224 | 1, 0):
        assert [_call(d, "f(bytes32)", word) for d in deployments] == [int(word == selector)] * 2


def test_max_length_array_to_mapping_blocks_write(tmp_path):
    path = tmp_path / "entry.vy"
    source = """# @version 0.3.7
slots: public(uint256[max_value(uint256)])
@external
def write(i: uint256, x: uint256):
    self.slots[i] = x
"""
    path.write_text(source)
    config = Config(paths=(path,), target_version="0.4.3", aggressive=True)
    batch = engine.prepare_migrations([engine.bounded_migration_request(path, source, config)], config)
    decision = engine.validate_migrations(batch, config)
    report = batch.files[0].report
    assert report.source_compile == report.target_compile == "passed"
    assert report.abi_equal and report.method_ids_equal
    assert report.storage_layout_equal is False
    assert [blocker.code for blocker in decision.blockers] == ["storage_layout_changed"]


def test_beta_factory_deploys_forwarder_after_explicit_opt_in(tmp_path):
    path = tmp_path / "factory.vy"
    source = """# @version 0.1.0b8
@public
def child(target: address) -> address:
    return create_with_code_of(target)
"""
    migrated = apply_rules(source, Config(paths=(path,), aggressive=True))
    for code, version in ((source, "0.1.0b8"), (migrated.source, "0.4.3")):
        vm, sender, address = _deploy(path, code, version)
        message = {
            "from": sender, "to": address, "gas": 1_000_000,
            "data": _calldata("child(address)", (int(address, 16),)),
        }
        child = "0x" + vm.call(message)[-40:]
        assert vm.get_transaction_receipt(vm.send_transaction(message))["status"] == 1
        child_code = vm.get_code(child)
        assert address[2:].lower() in child_code.lower()
        assert child_code != vm.get_code(address)


def test_stateless_initializer_removal_preserves_embedded_nonreentrancy_pragma(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.vy"
    (tmp_path / "lib.vy").write_text("#pragma version >=0.5.0b1\nX: constant(uint256) = 1\n")
    source = """#pragma version 0.5.0b1
import lib
initializes: (
    #pragma nonreentrancy on
    lib
)

@internal
@nonreentrant
def keep_lock_slot():
    pass

@external
def probe() -> bool:
    return raw_call(self, method_id("ping()"), revert_on_failure=False)

@external
def ping():
    pass
"""
    path.write_text(source)
    config = Config(
        paths=(path,),
        target_version="0.5.0b2",
        source_snapshot=compiler.resolve_import_closure({path: source}),
    )
    result = apply_rules(source, config, path)
    assert any(fix.rule == "VY126" for fix in result.fixes)
    original_compile = compiler.compile_source_file(path, config, "0.5.0b1")
    migrated_compile = compiler.compile_target_source(path, result.source, config)
    assert original_compile.status == migrated_compile.status == "passed"
    assert compiler.compare_artifacts(original_compile, migrated_compile) == (True, True, True)

    original = _deploy(path, source, "0.5.0b1")
    migrated = _deploy(path, result.source, "0.5.0b2")
    assert _call(original, "probe()") == 0
    assert _call(migrated, "probe()") == 0
