from pathlib import Path

import pytest

from vyupgrade import compiler, engine
from vyupgrade.cli import main
from vyupgrade.closure import write_closure_output
from vyupgrade.models import Config, FileReport
from vyupgrade.write_plan import MigrationPlan, WriteTransactionError


def _project(tmp_path):
    entry = tmp_path / "main.vy"
    helper = tmp_path / "helper.vy"
    entry.write_text('''#pragma version 0.4.2
import helper
@external
@pure
def answer() -> uint256:
    return helper.answer()
''')
    helper.write_bytes(b"#pragma version 0.4.2\r\n@internal\r\n@pure\r\ndef answer() -> uint256:\r\n    return 42\r\n")
    return entry, helper


def test_single_file_write_blocks_incompatible_dependency(tmp_path):
    entry, helper = _project(tmp_path)
    before = entry.read_bytes(), helper.read_bytes()
    assert main([str(entry), "--write", "--target-version", "0.4.3"]) == 2
    assert (entry.read_bytes(), helper.read_bytes()) == before


def test_closure_migration_outputs_a_compilable_project(tmp_path):
    entry, helper = _project(tmp_path)
    original_helper = helper.read_bytes()
    output = tmp_path / "upgraded"
    assert main([str(entry), "--include-dependencies", "--closure-output", str(output)]) == 0
    config = Config(paths=(output / "main.vy",), compiler_search_paths=(output,))
    result = compiler.compile_source_file(output / "main.vy", config, "0.4.3")
    assert result.status == "passed", result.stderr
    assert result.artifacts["bytecode"].startswith("0x")
    assert helper.read_bytes() == original_helper


def test_backend_failure_is_not_successful_validation(tmp_path):
    entry = tmp_path / "bad.vy"
    source = '''#pragma version 0.3.10
@external
@pure
def f(x: address[2]) -> bool:
    return x == empty(address[2])
'''
    entry.write_text(source)
    config = Config(paths=(entry,), target_version="0.3.10")
    batch = engine.prepare_migrations([engine.bounded_migration_request(entry, source, config)], config)
    decision = engine.validate_migrations(batch, config)
    assert batch.files[0].source_compile.status == "failed"
    assert batch.files[0].target_compile.status == "failed"
    assert not decision.write_allowed


def test_snapshot_bytes_are_read_once_and_reused_for_validation_and_export(tmp_path, monkeypatch):
    entry, helper = _project(tmp_path)
    expected = helper.read_bytes()
    reads = []
    original_read = Path.read_bytes

    def read(path):
        if path.resolve() == helper.resolve():
            reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    snapshot = compiler.resolve_import_closure({entry: entry.read_text()})
    assert len(reads) == 1
    with pytest.raises(TypeError):
        snapshot.contents[helper.resolve()] = b"changed"
    helper.write_text("#pragma version 0.4.3\nBROKEN\n")
    with compiler.target_overlay(snapshot.sources, "0.4.3", snapshot=snapshot) as overlay:
        assert overlay.paths[helper.resolve()].read_bytes() == expected
    output = tmp_path / "export"
    result = write_closure_output(output, {entry: entry.read_text()}, "0.4.3", snapshot=snapshot)
    assert result.status == "written"
    assert (output / "helper.vy").read_bytes() == expected
    assert len(reads) == 1


def test_source_compilation_uses_captured_bytes_even_after_live_changes(tmp_path):
    entry, helper = _project(tmp_path)
    source = entry.read_text()
    snapshot = compiler.resolve_import_closure({entry: source})
    entry.write_text("invalid source\n")
    helper.write_text("invalid dependency\n")
    config = Config(paths=(entry,), target_version="0.4.2")
    batch = engine.prepare_migrations(
        [engine.bounded_migration_request(entry, source, config)], config, snapshot=snapshot
    )
    assert batch.files[0].source_compile.status == "passed", batch.files[0].source_compile.stderr
    assert engine.validate_migrations(batch, config).status == "passed"


def test_write_transaction_rechecks_unmodified_dependency(tmp_path):
    entry, helper = _project(tmp_path)
    original = entry.read_text()
    plan = MigrationPlan()
    plan.add_source(entry, original, original + "# changed\n", FileReport(path=entry))
    plan.add_dependency(helper, helper.read_bytes())
    helper.write_text("changed by another process\n")
    with pytest.raises(WriteTransactionError, match="changed before commit"):
        plan.commit()
    assert entry.read_text() == original


@pytest.mark.parametrize("raw", ["", "0x", "0x0", "0xzz", '"0x00"'])
def test_missing_or_malformed_bytecode_fails_output_parsing(raw):
    with pytest.raises(ValueError):
        compiler._parse_outputs(raw, ("bytecode",))
