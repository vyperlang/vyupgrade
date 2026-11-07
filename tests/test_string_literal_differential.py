import hashlib
import json

import pytest
from eth_abi import decode, encode
from eth_tester import EthereumTester
from eth_utils import keccak

from vyupgrade import compiler, engine
from vyupgrade.cli import main
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


def test_mixed_line_endings_do_not_relocate_return_evidence_into_a_hash(tmp_path):
    body = '''@external
@pure
def hash_value() -> bytes32:
    return keccak256("é")

@external
@pure
def literal_value() -> String[1]:
    return "é"
'''
    # A compiler offset for the second "é" equals the *raw* offset of the first.
    # The legacy compiler folds the hash using UTF-8 but returns the literal as Latin-1.
    distance = body.rindex('"é"') - body.index('"é"')
    source = "# @version 0.3.7\n" + "\r\n" * distance + body.replace("\n", "\r")
    path = tmp_path / "hash.vy"
    path.write_bytes(source.encode())
    config = Config(paths=(path,), target_version="0.4.3")
    request = engine.bounded_migration_request(path, source, config)
    batch = engine.prepare_migrations([request], config)
    migration = batch.files[0]
    assert migration.source_compile.status == "passed", migration.source_compile.stderr
    assert 'return keccak256("é")' in migration.rewrite.source
    assert r"return convert(b'\xe9', String[1])" in migration.rewrite.source
    hash_line = source.splitlines().index('    return keccak256("é")') + 1
    assert [d.line for d in migration.rewrite.diagnostics if d.rule == "VYD022"] == [hash_line]


def test_cli_preserves_cr_only_source_and_multiline_literal_payload(tmp_path):
    path = tmp_path / "text.vy"
    original = '# @version 0.3.7\r@external\r@pure\rdef text() -> String[8]:\r    return """é\rü"""\r'
    path.write_bytes(original.encode())
    config = Config(paths=(path,), target_version="0.4.3")
    before = compiler.compile_source_file(path, config, "0.3.7")
    report_path = tmp_path / "report.json"

    assert main([str(path), "--write", "--report-json", str(report_path)]) == 0
    written = path.read_bytes()
    assert written.startswith(b"#pragma version 0.4.3\r@external\r")
    assert b"\n" not in written
    report = json.loads(report_path.read_text())
    (file,) = report["files"]
    assert file["original_sha256"] == hashlib.sha256(original.encode()).hexdigest()
    assert file["final_sha256"] == file["candidate_sha256"] == hashlib.sha256(written).hexdigest()
    assert file["validation"]["decision"]["status"] == "passed"
    fixes = {fix["rule"]: fix for fix in file["fixes"]}
    assert set(fixes) == {"VY001", "VY224"}
    assert fixes["VY224"]["before"] == '"""é\rü"""'

    after = compiler.compile_target_source(path, written.decode(), config)
    for result in (before, after):
        assert result.status == "passed", result.stderr
        vm = EthereumTester()
        sender = vm.get_accounts()[0]
        tx = vm.send_transaction({
            "from": sender, "gas": 3_000_000, "data": result.artifacts["bytecode"],
        })
        address = vm.get_transaction_receipt(tx)["contract_address"]
        out = vm.call({
            "from": sender, "to": address, "data": "0x" + keccak(text="text()")[:4].hex(),
        })
        assert decode(["bytes"], bytes.fromhex(out[2:])) == (b"\xe9\x0a\xfc",)
