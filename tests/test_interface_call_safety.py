"""Check the CALL/STATICCALL consequences of changing a local interface."""
from eth_abi import encode
from eth_tester import EthereumTester
from eth_utils import keccak
import pytest

from vyupgrade import engine
from vyupgrade.models import Config


WRITER = """# @version 0.3.10
hits: public(uint256)
@external
def touch() -> uint256:
    self.hits += 1
    return self.hits
"""

NO_RETURN = """# @version 0.3.10
interface Checker:
    def touch(): view

@external
def run(a: address):
    Checker(a).touch()
"""

OUTGOING = """# @version 0.3.10
interface Checker:
    def touch() -> uint256: nonpayable

@external
@view
def touch() -> uint256:
    return 7

@external
def run(a: address) -> uint256:
    return Checker(a).touch()
"""
SHARED = OUTGOING.replace("@external", "implements: Checker\n\n@external", 1)


def _migrate(tmp_path, source, aggressive):
    files = {tmp_path / "router.vy": source, tmp_path / "writer.vy": WRITER}
    config = Config(paths=tuple(files), target_version="0.4.3", aggressive=aggressive)
    requests = []
    for path, content in files.items():
        path.write_text(content)
        requests.append(engine.bounded_migration_request(path, content, config))
    batch = engine.prepare_migrations(requests, config)
    return batch, engine.validate_migrations(batch, config)


def _run(router, writer):
    vm = EthereumTester()
    sender = vm.get_accounts()[0]

    def deploy(result):
        tx = vm.send_transaction({"from": sender, "gas": 1_000_000, "data": result.artifacts["bytecode"]})
        receipt = vm.get_transaction_receipt(tx)
        assert receipt["status"] == 1
        return receipt["contract_address"]

    caller, callee = deploy(router), deploy(writer)
    tx = vm.send_transaction({
        "from": sender, "to": caller, "gas": 300_000,
        "data": "0x" + (keccak(text="run(address)")[:4] + encode(["address"], [callee])).hex(),
    })
    receipt = vm.get_transaction_receipt(tx)
    hits = vm.call({"from": sender, "to": callee, "data": "0x" + keccak(text="hits()")[:4].hex()})
    return receipt["status"], int(hits, 16)


@pytest.mark.parametrize(("source", "before", "after"), [
    (NO_RETURN, (0, 0), (1, 1)),
    (SHARED, (1, 1), (0, 0)),
])
def test_behavior_changing_call_mode_requires_aggressive(tmp_path, source, before, after):
    batch, decision = _migrate(tmp_path, source, aggressive=False)
    router = batch.files[0]
    assert router.report.source_compile == "passed"
    assert router.report.target_compile == "failed"
    assert decision.status == "blocked" and not decision.write_allowed
    assert any(d.rule == "VYD023" for d in router.rewrite.diagnostics)
    assert not any(f.rule == "VY014" for f in router.rewrite.fixes)

    batch, decision = _migrate(tmp_path, source, aggressive=True)
    router, writer = batch.files
    assert decision.status == "passed", router.report.target_error
    assert router.report.abi_equal and router.report.method_ids_equal and router.report.storage_layout_equal
    assert any(d.rule == "VYD023" for d in router.rewrite.diagnostics)
    assert _run(router.source_compile, writer.source_compile) == before
    assert _run(router.target_compile, writer.target_compile) == after


def test_unrelated_view_function_preserves_remote_writes(tmp_path):
    batch, decision = _migrate(tmp_path, OUTGOING, aggressive=False)
    router, writer = batch.files
    assert decision.status == "passed", router.report.target_error
    assert "return extcall Checker(a).touch()" in router.rewrite.source
    assert not any(d.rule == "VYD023" for d in router.rewrite.diagnostics)
    assert _run(router.source_compile, writer.source_compile) == (1, 1)
    assert _run(router.target_compile, writer.target_compile) == (1, 1)


def test_payable_implementation_preserves_value_rejection_and_abi_gate(tmp_path):
    conformance_only = """# @version 0.3.10
interface Receiver:
    def receiveFunds(): payable

implements: Receiver

@external
def receiveFunds():
    pass
"""
    batch, decision = _migrate(tmp_path, conformance_only, aggressive=False)
    router = batch.files[0]
    assert decision.status == "passed", router.report.target_error
    assert router.report.abi_equal
    for result in (router.source_compile, router.target_compile):
        vm = EthereumTester()
        sender = vm.get_accounts()[0]
        tx = vm.send_transaction({"from": sender, "gas": 1_000_000, "data": result.artifacts["bytecode"]})
        address = vm.get_transaction_receipt(tx)["contract_address"]
        tx = vm.send_transaction({
            "from": sender, "to": address, "gas": 300_000, "value": 1,
            "data": "0x" + keccak(text="receiveFunds()")[:4].hex(),
        })
        assert vm.get_transaction_receipt(tx)["status"] == 0

    shared = conformance_only + """
@external
@payable
def relay(target: address):
    Receiver(target).receiveFunds(value=msg.value)
"""
    batch, decision = _migrate(tmp_path, shared, aggressive=True)
    router = batch.files[0]
    assert router.report.source_compile == router.report.target_compile == "passed"
    assert router.report.abi_equal is False
    assert [issue.code for issue in decision.blockers] == ["abi_changed"]
    assert not decision.write_allowed


@pytest.mark.parametrize("calls_above_floor", [False, True])
def test_method_conformance_is_separate_from_other_bucket_calls(tmp_path, calls_above_floor):
    # SplitBucket implements both entries, but funnels deposits to child buckets
    # through convert(). A read on the child would make above_floor's mode observable.
    source = """# @version 0.3.10
interface Bucket:
    def above_floor() -> bool: nonpayable
    def convert(child: address): nonpayable

implements: Bucket

@external
@view
def above_floor() -> bool:
    return False

@external
def convert(child: address):
    Bucket(child).convert(child)
"""
    if calls_above_floor:
        source += """
@external
def probe(child: address) -> bool:
    return Bucket(child).above_floor()
"""
    batch, decision = _migrate(tmp_path, source, aggressive=False)
    router = batch.files[0]
    assert router.report.source_compile == "passed"
    if calls_above_floor:
        assert "def above_floor() -> bool: nonpayable" in router.rewrite.source
        assert any(d.rule == "VYD023" for d in router.rewrite.diagnostics)
        assert decision.status == "blocked" and not decision.write_allowed
    else:
        assert "def above_floor() -> bool: view" in router.rewrite.source
        assert "extcall Bucket(child).convert(child)" in router.rewrite.source
        assert not any(d.rule == "VYD023" for d in router.rewrite.diagnostics)
        assert decision.status == "passed", router.report.target_error
        assert router.report.abi_equal and router.report.method_ids_equal and router.report.storage_layout_equal
