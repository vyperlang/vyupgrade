from hashlib import sha256
from pathlib import Path

import pytest
from eth_tester import EthereumTester
from eth_utils import keccak

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


@pytest.mark.parametrize("deleted", [False, True])
def test_source_compilation_uses_captured_bytes_even_after_live_changes(tmp_path, deleted):
    entry, helper = _project(tmp_path)
    source = entry.read_text()
    snapshot = compiler.resolve_import_closure({entry: source})
    entry.write_text("invalid source\n")
    if deleted:
        helper.unlink()
    else:
        helper.write_text("invalid dependency\n")
    config = Config(paths=(entry,), target_version="0.4.2")
    batch = engine.prepare_migrations(
        [engine.bounded_migration_request(entry, source, config)], config, snapshot=snapshot
    )
    assert batch.files[0].source_compile.status == "passed", batch.files[0].source_compile.stderr
    assert engine.validate_migrations(batch, config).status == "passed"


@pytest.mark.parametrize("retarget", [False, True], ids=["unlinked", "retargeted"])
def test_source_compilation_uses_captured_symlink_import(tmp_path, retarget):
    project = tmp_path / "project"
    search_path = tmp_path / "site-packages"
    dependency = search_path / "depkg" / "mod.vy"
    competing = search_path / "competing" / "mod.vy"
    dependency.parent.mkdir(parents=True)
    competing.parent.mkdir()
    project.mkdir()
    alias = project / "alias"
    alias.symlink_to(dependency.parent, target_is_directory=True)
    dependency.write_text(
        "#pragma version 0.4.3\n@internal\n@pure\ndef f() -> uint256:\n    return 1\n"
    )
    competing.write_text("invalid dependency\n")
    entry = project / "Main.vy"
    source = (
        "#pragma version 0.4.3\nfrom alias import mod\n"
        "@external\n@pure\ndef g() -> uint256:\n    return mod.f()\n"
    )
    entry.write_text(source)
    snapshot = compiler.resolve_import_closure({entry: source}, (search_path,))
    assert snapshot.layouts[dependency.resolve()] == Path("alias/mod.vy")
    alias.unlink()
    if retarget:
        alias.symlink_to(competing.parent, target_is_directory=True)

    config = Config(
        paths=(entry,),
        target_version="0.4.3",
        compiler_search_paths=(search_path,),
        source_snapshot=snapshot,
    )
    result = compiler.compile_source_file(entry, config, "0.4.3")
    assert result.status == "passed", result.stderr
    assert snapshot.resolve_import(entry, "alias.mod") == dependency.resolve()


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


def test_independent_projects_with_same_named_files_compile_separately(tmp_path):
    paths = []
    for name, value in (("a", 1), ("b", 2)):
        root = tmp_path / name
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'p'\nversion = '0.0.0'\n")
        (root / "helper.vy").write_text(
            f"#pragma version ^0.4.2\n@internal\n@pure\ndef g() -> uint256:\n    return {value}\n"
        )
        path = root / "Main.vy"
        path.write_text(
            "#pragma version 0.4.2\nimport helper\n"
            "@external\n@pure\ndef f() -> uint256:\n    return helper.g()\n"
        )
        paths.append(path)
    config = Config(paths=tuple(paths), target_version="0.4.3")
    requests = [engine.bounded_migration_request(p, p.read_text(), config) for p in paths]
    batch = engine.prepare_migrations(requests, config)
    assert [f.source_compile.status for f in batch.files] == ["passed", "passed"]
    assert engine.validate_migrations(batch, config).status == "passed"


def _nested_projects(tmp_path, additional_source=""):
    entries = {}
    for name, value in (("a", 1), ("b", 2)):
        project = tmp_path / name
        entry = project / "contracts" / "Main.vy"
        entry.parent.mkdir(parents=True)
        (project / "pyproject.toml").write_text(f"[project]\nname = '{name}'\nversion = '0.0.0'\n")
        (project / "helper.vy").write_text(
            f"#pragma version ^0.4.2\n@internal\n@pure\ndef g() -> uint256:\n    return {value}\n"
        )
        entry.write_text(
            "#pragma version 0.4.2\nimport helper\n"
            "@external\n@pure\ndef f() -> uint256:\n    return helper.g()\n"
            + additional_source
        )
        entries[entry] = value
    return entries


def _compiled_return_value(result, signature):
    assert result.status == "passed", result.stderr
    vm = EthereumTester()
    sender = vm.get_accounts()[0]
    transaction = vm.send_transaction({
        "from": sender,
        "gas": 1_000_000,
        "data": result.artifacts["bytecode"],
    })
    receipt = vm.get_transaction_receipt(transaction)
    assert receipt["status"] == 1
    return int(vm.call({
        "from": sender,
        "to": receipt["contract_address"],
        "data": "0x" + keccak(text=signature)[:4].hex(),
    }), 16)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_target_validation_keeps_the_entry_projects_imports(tmp_path, reverse):
    interface = "\ninterface Reader:\n    def g() -> uint256: view\n"
    shared_import = "\nimport shared\n"
    shared_function = "\n@external\n@pure\ndef shared_value() -> uint256:\n    return shared.n()\n"
    entries = _nested_projects(tmp_path, interface + shared_import + shared_function)
    search_path = tmp_path / "vendor"
    search_path.mkdir()
    shared = search_path / "shared.vy"
    shared.write_text("#pragma version ^0.4.2\n@internal\n@pure\ndef n() -> uint256:\n    return 9\n")
    paths = tuple(sorted(entries, reverse=reverse))
    config = Config(
        paths=(*paths, shared),
        target_version="0.4.3",
        split_interfaces=True,
        compiler_search_paths=(search_path,),
    )
    requests = [engine.bounded_migration_request(path, path.read_text(), config) for path in config.paths]
    batch = engine.prepare_migrations(requests, config)
    formatted = "# formatted shared candidate\n"
    decision = engine.validate_migrations(
        batch, config, lambda path, source: source + formatted if path == shared else source
    )
    assert decision.status == "passed", batch.reports

    shared_candidate = batch.files[-1].rewrite.source + formatted
    for migration in batch.files[:len(entries)]:
        assert _compiled_return_value(migration.source_compile, "f()") == entries[migration.path]
        assert _compiled_return_value(migration.target_compile, "f()") == entries[migration.path]
        assert _compiled_return_value(migration.target_compile, "shared_value()") == 9
        expected = {
            str(migration.path),
            str(migration.path.parents[1] / "helper.vy"),
            str(migration.path.with_name("Reader.vyi")),
            str(shared),
        }
        attestation = migration.report.target_attestation
        staged = {identity.path: identity.sha256 for identity in attestation.declared_spec.sources}
        assert set(staged) == expected
        assert staged[str(shared)] == sha256(shared_candidate.encode()).hexdigest()
    assert len(batch.generated) == 2
    assert all(generated.target_compile.status == "passed" for generated in batch.generated)


def test_conflicting_projects_cannot_export_one_flat_closure(tmp_path, capsys):
    entries = _nested_projects(tmp_path)
    original = {path: path.read_bytes() for path in entries}
    output = tmp_path / "export"
    assert main([
        *map(str, entries), "--include-dependencies", "--closure-output", str(output), "--write"
    ]) == 4
    assert "cannot export a shared closure" in capsys.readouterr().err
    assert not output.exists()
    assert {path: path.read_bytes() for path in entries} == original


@pytest.mark.parametrize(
    "import_line",
    ["from.lib import Main as m", "from . lib import Main as m", "import lib . Main as m"],
)
def test_source_compile_follows_import_spelled_with_any_spacing(tmp_path, import_line):
    # ``.`` is its own token, so spacing around it is free.
    (tmp_path / "lib").mkdir()
    library = tmp_path / "lib" / "Main.vy"
    library.write_text(
        "#pragma version 0.4.3\n@internal\n@pure\ndef f(x: uint256) -> uint256:\n    return x\n"
    )
    entry = tmp_path / "Main.vy"
    entry.write_text(
        f"#pragma version 0.4.3\n{import_line}\n"
        "@external\n@pure\ndef g(x: uint256) -> uint256:\n    return m.f(x)\n"
    )
    assert main([str(entry), str(library), "--check", "--target-version", "0.4.3"]) == 0


@pytest.mark.parametrize(
    "import_line",
    ["from . import(helper)", "from . import (\n    helper,\n)", "from . import \\\n    helper"],
)
def test_source_compile_follows_continued_from_import(tmp_path, import_line):
    helper = tmp_path / "helper.vy"
    helper.write_text("#pragma version 0.4.3\n@internal\n@pure\ndef f() -> uint256:\n    return 1\n")
    entry = tmp_path / "Main.vy"
    entry.write_text(
        f"#pragma version 0.4.3\n{import_line}\n"
        "@external\n@pure\ndef g() -> uint256:\n    return helper.f()\n"
    )
    assert main([str(entry), str(helper), "--check", "--target-version", "0.4.3"]) == 0


def test_source_compile_stages_its_project_when_the_import_scanner_misses(tmp_path):
    # Vyper NFKC-normalizes identifiers: fullwidth `\uff48elper` imports `helper.vy`.
    # ``lib.vy`` is not a batch input: only walking ``helper.vy`` finds it.
    (tmp_path / "lib.vy").write_text(
        "#pragma version 0.4.3\n@internal\n@pure\ndef one() -> uint256:\n    return 1\n"
    )
    helper = tmp_path / "helper.vy"
    helper.write_text(
        "#pragma version 0.4.3\nimport lib\n@internal\n@pure\ndef f() -> uint256:\n    return lib.one()\n"
    )
    entry = tmp_path / "Main.vy"
    entry.write_text(
        "#pragma version 0.4.3\nimport \uff48elper as h\n"
        "@external\n@pure\ndef g() -> uint256:\n    return h.f()\n"
    )
    assert main([str(entry), str(helper), "--check", "--target-version", "0.4.3"]) == 0


def test_search_path_dependency_does_not_resolve_against_the_entry_root(tmp_path):
    entry_dir, search_dir = tmp_path / "a", tmp_path / "b"
    entry_dir.mkdir()
    search_dir.mkdir()
    for directory, value in ((entry_dir, 1), (search_dir, 2)):
        (directory / "helper.vy").write_text(
            f"#pragma version 0.4.3\n@internal\n@pure\ndef g() -> uint256:\n    return {value}\n"
        )
    (search_dir / "lib.vy").write_text(
        "#pragma version 0.4.3\nimport helper\n"
        "@internal\n@pure\ndef f() -> uint256:\n    return helper.g()\n"
    )
    entry = entry_dir / "Main.vy"
    entry.write_text(
        "#pragma version 0.4.3\nimport lib\n"
        "@external\n@pure\ndef h() -> uint256:\n    return lib.f()\n"
    )
    args = [str(entry), "--check", "--target-version", "0.4.3", "--compiler-search-paths", str(search_dir)]
    assert main(args) == 0


def test_source_compile_falls_back_to_a_sibling_project_root(tmp_path):
    # ``helper`` isn't in ``app``, so it resolves through the other input's root.
    app, lib = tmp_path / "app", tmp_path / "libs" / "mylib"
    for directory in (app, lib):
        directory.mkdir(parents=True)
        (directory / "pyproject.toml").write_text(
            f"[project]\nname = '{directory.name}'\nversion = '0.0.0'\n"
        )
    helper = lib / "helper.vy"
    helper.write_text("#pragma version 0.4.3\n@internal\n@pure\ndef g() -> uint256:\n    return 1\n")
    entry = app / "Main.vy"
    entry.write_text(
        "#pragma version 0.4.3\nimport helper\n"
        "@external\n@pure\ndef f() -> uint256:\n    return helper.g()\n"
    )
    assert main([str(entry), str(helper), "--check", "--target-version", "0.4.3"]) == 0


def test_nested_project_interface_resolves_imports_from_its_own_root(tmp_path):
    nested = tmp_path / "nested"
    (nested / "interfaces").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'outer'\nversion = '0.0.0'\n")
    (nested / "pyproject.toml").write_text("[project]\nname = 'nested'\nversion = '0.0.0'\n")
    helper = nested / "interfaces" / "IHelper.vyi"
    helper.write_text(
        "#pragma version 0.4.3\nfrom interfaces import IThing\n"
        "@external\n@view\ndef thing() -> IThing:\n    ...\n"
    )
    thing = nested / "interfaces" / "IThing.vyi"
    thing.write_text("#pragma version 0.4.3\n@external\n@view\ndef value() -> uint256:\n    ...\n")
    entry = tmp_path / "Main.vy"
    entry.write_text(
        "#pragma version 0.4.3\nfrom nested.interfaces import IHelper\n"
        "@external\ndef register(helper: IHelper):\n    pass\n"
    )
    paths = (entry, helper, thing)
    assert main([*map(str, paths), "--check", "--target-version", "0.4.3"]) == 0


@pytest.mark.parametrize("all_inputs", [True, False], ids=["batch", "dependencies"])
def test_import_closures_scan_each_file_once(tmp_path, monkeypatch, all_inputs):
    # Per-entry closures re-walked every file's imports, quadratic in project size.
    paths = []
    for index in range(20):
        path = tmp_path / f"m{index}.vy"
        imports = f"import m{index - 1}\n" if index else ""
        path.write_text(f"#pragma version 0.4.3\n{imports}")
        paths.append(path)
    scans = []
    scan = compiler._validation_import_sources
    monkeypatch.setattr(
        compiler,
        "_validation_import_sources",
        lambda path, *args, **kwargs: scans.append(path) or scan(path, *args, **kwargs),
    )
    inputs = paths if all_inputs else paths[-1:]
    snapshot = compiler.resolve_import_closure({p: p.read_text() for p in inputs}, (tmp_path,))
    entries = snapshot.roots + snapshot.dependencies
    assert len(entries) == len(paths)
    assert len(scans) <= 3 * len(paths)
    scans.clear()
    for entry in entries:
        compiler._entry_closure(snapshot, entry)
    assert scans == []
