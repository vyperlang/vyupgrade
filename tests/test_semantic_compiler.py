from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from packaging.version import Version
from uv import find_uv_bin

from vyupgrade import compiler, compiler_runner
from vyupgrade.ast_facts import iter_nodes, root_ast
from vyupgrade.cli import main
from vyupgrade.compiler import (
    CompileResult,
    compare_artifact_details,
    compare_artifacts,
    compile_source_ast,
    compile_source_file,
    compile_target_source,
    resolve_import_closure,
    target_overlay,
    unavailable_validation_artifacts,
)
from vyupgrade.models import Config


SCOPED_CALLS = """# @version 0.3.10
SCALE: constant(uint256) = 10**18

interface Read:
    def probe() -> uint256: view

interface Write:
    def probe() -> uint256: nonpayable

@external
def route(a: address, read_mode: bool) -> uint256:
    if read_mode:
        oracle: Read = Read(a)
        return oracle.probe() / SCALE
    else:
        oracle: Write = Write(a)
        return oracle.probe() / SCALE
"""


def explicit_vyper(version: str, directory: Path) -> str:
    environment = directory / "compiler"
    uv = find_uv_bin()
    subprocess.run([uv, "venv", "--python", "3.11", str(environment)], check=True)
    python = environment / "bin" / "python"
    subprocess.run([uv, "pip", "install", "--python", str(python), f"vyper=={version}"], check=True)
    return str(environment / "bin" / "vyper")


def validated_artifacts(result: CompileResult, version: str, authority: str) -> dict[str, object]:
    assert result.status == "passed", result.stderr
    assert result.completion_status == "completed"
    assert result.compiler_identity is not None
    assert Version(result.compiler_identity.version).public == version
    assert result.resolved_compiler == result.compiler_identity.version
    assert result.compiler_authority == authority
    assert not unavailable_validation_artifacts(result)
    assert result.artifacts is not None
    assert {"abi", "method_identifiers", "layout", "bytecode", "ast"} <= result.artifacts.keys()
    assert str(result.artifacts["bytecode"]).startswith("0x")
    return result.artifacts


@pytest.fixture
def compiler_processes(monkeypatch):
    run = compiler._run_compiler_process
    processes = []

    def record(*args, **kwargs):
        process = run(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(compiler, "_run_compiler_process", record)
    return processes


def test_private_facts_distinguish_identical_calls_by_source_node(
    tmp_path: Path, compiler_processes
) -> None:
    contract = tmp_path / "route.vy"
    contract.write_text(SCOPED_CALLS)
    config = Config(paths=(contract,), target_version="0.4.3")
    managed = compile_source_file(contract, config, "0.3.10")
    assert len(compiler_processes) == 1
    artifacts = validated_artifacts(managed, "0.3.10", "source-exact")
    facts = artifacts["external_calls_v1"]
    assert facts["source_sha256sum"] == hashlib.sha256(contract.read_bytes()).hexdigest()

    calls = sorted(facts["calls"], key=lambda call: int(call["src"].split(":")[0]))
    assert [(call["interface"], call["method"], call["mutability"]) for call in calls] == [
        ("Read", "probe", "view"),
        ("Write", "probe", "nonpayable"),
    ]
    assert all(call["return_type"] == "uint256" for call in calls)
    raw_nodes = {node["node_id"]: node for node in iter_nodes(root_ast(artifacts["ast"]), "Call")}
    assert len({call["node_id"] for call in calls}) == 2
    for call in calls:
        node = raw_nodes[call["node_id"]]
        assert node["src"] == call["src"]
        start, length, source_id = map(int, call["src"].split(":"))
        assert source_id == 0
        assert SCOPED_CALLS[start : start + length] == "oracle.probe()"

    explicit_config = Config(
        paths=(contract,), target_version="0.4.3", source_vyper=explicit_vyper("0.3.10", tmp_path)
    )
    result = compile_source_file(contract, explicit_config, "0.3.10")
    assert len(compiler_processes) == 2
    explicit = validated_artifacts(result, "0.3.10", "explicit-executable")
    assert "external_calls_v1" not in explicit
    for name in ("abi", "method_identifiers", "layout", "bytecode", "ast"):
        assert explicit[name] == artifacts[name]

    dependency = compile_source_ast(contract, config, "0.3.10")
    assert dependency.status == "passed"
    assert dependency.artifacts["external_calls_v1"] == facts
    assert len(compiler_processes) == 3
    command = compiler_processes[-1].command
    assert command[command.index("-f") + 1] == "ast"


def test_array_members_are_separate_from_same_named_interface_calls(tmp_path: Path) -> None:
    source = """# @version 0.3.10
interface Buffer:
    def append(x: uint256): nonpayable

queue: DynArray[uint256, 3]

@external
def route(api: Buffer, x: uint256):
    self.queue.append(x)
    api.append(self.queue.pop())
"""
    path = tmp_path / "buffer.vy"
    path.write_text(source)
    result = compile_source_file(path, Config(paths=(path,)), "0.3.10")
    assert result.status == "passed", result.stderr
    facts = result.artifacts["external_calls_v1"]
    nodes = {n["node_id"]: n for n in iter_nodes(root_ast(result.artifacts["ast"]), "Call")}

    def expression(site):
        node = nodes[site["node_id"]]
        assert node["src"] == site["src"]
        assert node["func"]["attr"] == site["method"]
        start, length, _ = map(int, node["src"].split(":"))
        return source[start : start + length]

    assert [expression(site) for site in facts["calls"]] == ["api.append(self.queue.pop())"]
    assert {expression(site) for site in facts["member_calls"]} == {
        "self.queue.append(x)",
        "self.queue.pop()",
    }


def test_managed_collector_failure_preserves_mandatory_output(
    tmp_path: Path, monkeypatch, compiler_processes
) -> None:
    # Old Vyper serializes some AST keys from sets. Fix the hash seed so the
    # control and injected-failure workers produce directly comparable stdout.
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    contract = tmp_path / "route.vy"
    contract.write_text(SCOPED_CALLS)
    config = Config(paths=(contract,), target_version="0.4.3")
    control = compile_source_file(contract, replace(config, select=frozenset({"VY001"})), "0.3.10")
    expected = validated_artifacts(control, "0.3.10", "source-exact")
    assert "external_calls_v1" not in expected

    # The real compiler executes in a child interpreter. Delegate to the same
    # adapter there with just the optional collector replaced.
    actual_runner = Path(compiler.__file__).with_name("compiler_runner.py")
    marker = tmp_path / "collector-ran"
    runner = tmp_path / "compiler_runner.py"
    runner.write_text(
        f"""import runpy
from pathlib import Path

main = runpy.run_path({str(actual_runner)!r})["main"]
main.__globals__["__file__"] = __file__

def fail(*args, **kwargs):
    Path({str(marker)!r}).touch()
    print("optional collector output")
    raise RuntimeError("injected collector failure")

main.__globals__["_external_calls_0310"] = fail
main()
"""
    )
    monkeypatch.setattr(compiler, "__file__", str(tmp_path / "compiler.py"))
    result = compile_source_file(contract, config, "0.3.10")
    assert marker.exists()
    assert validated_artifacts(result, "0.3.10", "source-exact") == expected
    assert len(compiler_processes) == 2
    assert compiler_processes[1].stdout == compiler_processes[0].stdout
    assert result.validated_sources == control.validated_sources
    assert result.compiler_identity == control.compiler_identity
    assert result.dependency_context == control.dependency_context
    assert result.compiler_declarations == control.compiler_declarations
    assert result.exit_status == control.exit_status


@pytest.mark.parametrize("required_flushed", [True, False])
def test_worker_timeout_recovers_only_a_flushed_compiler_result(monkeypatch, required_flushed):
    required = {
        "completion_status": "completed",
        "returncode": 0,
        "stdout": "0x00\n",
        "stderr": "compiler warning\n",
        "error_type": None,
        "failure_origin": None,
    }
    record = json.dumps(required).encode()
    # TimeoutExpired carries the bytes received so far, even in text mode.
    # The killed worker may have been writing either of its two JSON records.
    partial_output = record + b'\n{"calls":' if required_flushed else record[:-1]

    def interrupted(args, *, timeout, **kwargs):
        raise subprocess.TimeoutExpired(args, timeout, output=partial_output)

    monkeypatch.setattr(compiler_runner.subprocess, "run", interrupted)
    if required_flushed:
        assert compiler_runner._run_managed_compiler(["vyper"], 1, "external_calls_v1") == required
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            compiler_runner._run_managed_compiler(["vyper"], 1, "external_calls_v1")


def test_migration_writes_compiler_resolved_calls(tmp_path: Path) -> None:
    path = tmp_path / "route.vy"
    path.write_text(SCOPED_CALLS)
    assert main([str(path), "--write"]) == 0
    assert "return staticcall oracle.probe()" in path.read_text()
    assert "return extcall oracle.probe()" in path.read_text()
    assert main([str(path), "--check"]) == 0


def test_discarded_imported_return_uses_compiler_type(tmp_path: Path) -> None:
    abi = [{
        "type": "function",
        "name": "totalSupply",
        "inputs": [],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    }]
    (tmp_path / "Other.json").write_text(json.dumps(abi))
    path = tmp_path / "caller.vy"
    path.write_text("""# @version 0.3.10
import Other as IERC20

@external
def probe(a: address):
    IERC20(a).totalSupply()
""")

    assert main([str(path), "--write"]) == 0
    assert ": bool = staticcall IERC20(a).totalSupply()" in path.read_text()


def test_discarded_local_return_uses_widened_bound(tmp_path: Path) -> None:
    path = tmp_path / "caller.vy"
    path.write_text("""# @version 0.3.10
interface Reader:
    def data() -> Bytes[1]: view

implements: Reader

@external
@view
def data() -> Bytes[5]:
    return b"abcde"

@external
def probe(a: address):
    Reader(a).data()
""")

    assert main([str(path), "--write", "--aggressive", "--target-version", "0.5.0a3"]) == 0
    migrated = path.read_text()
    assert "def data() -> Bytes[5]: view" in migrated
    assert ": Bytes[5] = staticcall Reader(a).data()" in migrated


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            """interface Helpers:
    def update(): view
@external
def execute(h: address):
    Helpers(h).update()
""",
            "extcall Helpers(h).update()",
        ),
        (
            """interface Pricer:
    def price() -> uint256: nonpayable
implements: Pricer
@external
@view
def price() -> uint256:
    return 1
@external
def read_other(other: address) -> uint256:
    return Pricer(other).price()
""",
            "return staticcall Pricer(other).price()",
        ),
    ],
)
def test_aggressive_interface_edits_control_the_target_call_mode(tmp_path, body, expected):
    path = tmp_path / "contract.vy"
    path.write_text("# @version 0.3.10\n" + body)
    assert main([str(path), "--write", "--aggressive"]) == 0
    assert expected in path.read_text()


@pytest.mark.parametrize("mode", ["managed", "explicit", "optional-query-fails", "mandatory-fails"])
@pytest.mark.parametrize("version", ["0.5.0a3", "0.5.0b1"])
def test_public_error_resolves_to_an_imported_definition(
    tmp_path: Path, version: str, mode: str, compiler_processes
) -> None:
    contract = tmp_path / "main.vy"
    source = (
        f"#pragma version {version}\r\nimport errors\n\r\n@external\r"
        "def withdraw(amount: uint256):\r\n"
        "    raise errors.InsufficientBalance(self.balance, amount)\n"
    )
    if mode == "mandatory-fails":
        source = source.replace("self.balance, amount", "unknown_balance, amount")
    error_source = (
        f"#pragma version {version}\r\nerror InsufficientBalance:\n"
        "    available: uint256\r    required: uint256\r\n"
    )
    contract.write_bytes(source.encode())
    (tmp_path / "errors.vy").write_bytes(error_source.encode())
    executable = None if mode == "managed" else explicit_vyper(version, tmp_path)
    if mode == "optional-query-fails":
        wrapper = tmp_path / "limited-vyper"
        wrapper.write_text(
            '#!/bin/sh\ncase "$*" in *annotated_ast*) exit 1;; esac\n'
            f'exec {shlex.quote(executable)} "$@"\n'
        )
        wrapper.chmod(0o755)
        executable = str(wrapper)
    config = Config(
        paths=(contract,),
        target_version="0.5.0b2",
        source_vyper=executable,
        compiler_search_paths=(tmp_path,),
        select=frozenset({"VY127"}),
        source_snapshot=resolve_import_closure({contract: source}, (tmp_path,)),
    )
    result = compile_source_file(contract, config, version)
    requested = [process.command[process.command.index("-f") + 1] for process in compiler_processes]
    expected = ["abi,method_identifiers,layout,bytecode,ast"]
    if mode in {"explicit", "optional-query-fails"}:
        expected.append("annotated_ast")
    assert requested == expected
    paths = {Path(process.command[-1]) for process in compiler_processes}
    assert len(paths) == 1 and paths.pop().name == contract.name
    if mode == "mandatory-fails":
        assert result.command[0] == executable
        assert result.status == "failed"
        assert "unknown_balance" in result.stderr
        assert result.artifacts is None
        return
    authority = "source-exact" if mode == "managed" else "explicit-executable"
    artifacts = validated_artifacts(result, version, authority)
    assert result.validated_sources[0].sha256 == hashlib.sha256(contract.read_bytes()).hexdigest()
    assert "external_calls_v1" not in artifacts
    if mode == "optional-query-fails":
        assert "annotated_ast" not in artifacts
        return
    annotated = artifacts["annotated_ast"]
    (call,) = iter_nodes(root_ast(annotated), "Call")
    declaration = call["func"]["type"]["type_t"]["type_decl_node"]
    (imported,) = annotated["imports"]
    for module, original in ((root_ast(annotated), source), (imported, error_source)):
        normalized = original.replace("\r\n", "\n").replace("\r", "\n").encode()
        assert module["source_sha256sum"] == hashlib.sha256(normalized).hexdigest()
    (error,) = iter_nodes(imported, "ErrorDef")
    assert error["name"] == "InsufficientBalance"
    assert declaration == {"node_id": error["node_id"], "source_id": imported["source_id"]}
    assert root_ast(artifacts["ast"])["ast_type"] == "Module"

    if mode == "managed":
        dependency = compile_source_ast(contract, config, version)
        assert dependency.status == "passed"
        assert dependency.artifacts["annotated_ast"] == dependency.artifacts["ast"]
        (call,) = iter_nodes(root_ast(dependency.artifacts["annotated_ast"]), "Call")
        assert call["func"]["type"]["type_t"]["type_decl_node"] == declaration
        assert len(compiler_processes) == 2
        command = compiler_processes[-1].command
        assert command[command.index("-f") + 1] == "annotated_ast"


def _captured_source_compile(entry: Path, version: str) -> CompileResult:
    """Compile a source as validation does, from a snapshot of its imports."""
    snapshot = resolve_import_closure({entry: entry.read_text()}, (entry.parent,))
    config = Config(
        paths=(entry,),
        target_version=version,
        compiler_search_paths=(entry.parent,),
        source_snapshot=snapshot,
    )
    return compile_source_file(entry, config, version)


def _overlay_target_compile(entry: Path, version: str) -> CompileResult:
    """Compile a target as validation does, over staged copies of its imports."""
    config = Config(paths=(entry,), target_version=version, compiler_search_paths=(entry.parent,))
    sources = {entry: entry.read_text()}
    with target_overlay(sources, version, (entry.parent,)) as overlay:
        return compile_target_source(entry, entry.read_text(), config, overlay)


LEDGER_ENTRY = """#pragma version {version}
import ledger

initializes: ledger
exports: ledger.__interface__

@deploy
def __init__():
    ledger.__init__()
"""

LEDGER_MODULE = """#pragma version {version}

struct Record:
    {first}: uint256
    {second}: uint256

record: public(Record)

@deploy
def __init__():
    self.record.left = 1
    self.record.right = 2
"""


@pytest.mark.parametrize("version", ["0.4.3", "0.5.0b2"])
def test_imported_module_struct_member_order_is_compared(tmp_path: Path, version: str) -> None:
    def project(name: str, first: str, second: str) -> Path:
        root = tmp_path / name
        root.mkdir()
        module = LEDGER_MODULE.format(version=version, first=first, second=second)
        (root / "ledger.vy").write_text(module)
        entry = root / "main.vy"
        entry.write_text(LEDGER_ENTRY.format(version=version))
        return entry

    source_entry = project("source", "left", "right")
    copy_entry = project("copy", "left", "right")
    swapped_entry = project("swapped", "right", "left")
    source = _captured_source_compile(source_entry, version)
    changed = ("changed storage type definition: ledger.record (Record)",)

    for compile_entry in (_captured_source_compile, _overlay_target_compile):
        copy = compile_entry(copy_entry, version)
        swapped = compile_entry(swapped_entry, version)
        assert copy.status == swapped.status == "passed"
        assert compare_artifact_details(source, copy)[2] == []
        # The layout names only the struct, so its members come from the module's AST.
        assert tuple(compare_artifact_details(source, swapped)[2]) == changed


def test_module_evidence_requires_the_layout_compile_bytecode(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "ledger.vy").write_text(
        LEDGER_MODULE.format(version="0.4.3", first="left", second="right")
    )
    entry = tmp_path / "main.vy"
    entry.write_text(LEDGER_ENTRY.format(version="0.4.3"))
    run = compiler._run_compile_with_formats
    probes = []

    def change_module_before_probe(command, path, config, formats, *args, **kwargs):
        if "annotated_ast" not in formats:
            return run(command, path, config, formats, *args, **kwargs)
        # A module read live (not captured) changes between the two compiles.
        module = path.parent / "ledger.vy"
        module.write_text(module.read_text().replace("left: uint256", "left: uint128"))
        probe = run(command, path, config, formats, *args, **kwargs)
        probes.append(probe)
        return probe

    monkeypatch.setattr(compiler, "_run_compile_with_formats", change_module_before_probe)
    config = Config(paths=(entry,), target_version="0.4.3", compiler_search_paths=(tmp_path,))
    with target_overlay({entry: entry.read_text()}, "0.4.3", (tmp_path,)) as overlay:
        result = compile_target_source(entry, entry.read_text(), config, overlay)

    # The probe itself succeeded with module ASTs; only the bytecode differs.
    [probe] = probes
    assert result.status == probe.status == "passed"
    assert isinstance(probe.artifacts["annotated_ast"].get("imports"), list)
    assert probe.artifacts["bytecode"] != result.artifacts["bytecode"]
    assert compiler.STORAGE_TYPE_AST not in result.artifacts


TYPES_MODULE = """#pragma version 0.4.3

struct Record:
    {fields}

flag Permission:
    {members}
"""

TYPES_ENTRY = """#pragma version 0.4.3
import types

record: public(types.Record)
permission: public(types.Permission)
"""


def test_imported_types_stored_by_the_root_compare_member_order(tmp_path: Path) -> None:
    def project(name: str, fields: str, members: str) -> Path:
        root = tmp_path / name
        root.mkdir()
        (root / "types.vy").write_text(
            TYPES_MODULE.format(fields="\n    ".join(fields), members="\n    ".join(members))
        )
        entry = root / "main.vy"
        entry.write_text(TYPES_ENTRY)
        return entry

    struct = ("left: uint256", "right: uint256")
    flag = ("READ", "WRITE")
    source_entry = project("source", struct, flag)
    variants = {
        project("copy", struct, flag): [],
        project("struct", struct[::-1], flag): [
            "changed storage type definition: record (Record)",
        ],
        project("flag", struct, flag[::-1]): [
            "changed storage type definition: permission (Permission)",
        ],
    }
    source = _captured_source_compile(source_entry, "0.4.3")

    for compile_entry in (_captured_source_compile, _overlay_target_compile):
        for entry, differences in variants.items():
            target = compile_entry(entry, "0.4.3")
            assert target.status == "passed"
            # The layout names only Record and Permission; their members live in types.vy.
            assert sorted(compare_artifact_details(source, target)[2]) == differences
            assert compare_artifacts(source, target)[2] is (not differences)


def test_struct_field_of_a_module_interface_stays_comparable(tmp_path: Path) -> None:
    (tmp_path / "iface.vy").write_text(
        "#pragma version 0.4.3\n@external\ndef f():\n    pass\n"
    )
    types = TYPES_MODULE.format(fields="x: uint256\n    i: iface.__interface__", members="A")
    # An imported struct's field may itself be a module interface.
    (tmp_path / "types.vy").write_text(types.replace("\n\n", "\nimport iface\n\n", 1))
    entry = tmp_path / "main.vy"
    entry.write_text(TYPES_ENTRY)

    source = _captured_source_compile(entry, "0.4.3")
    target = _overlay_target_compile(entry, "0.4.3")

    assert source.status == target.status == "passed"
    assert compare_artifact_details(source, target)[2] == []
    assert compare_artifacts(source, target)[2] is True


def test_local_struct_with_an_imported_field_compares_its_members(tmp_path: Path) -> None:
    entry_text = """#pragma version 0.4.3
import types

struct Outer:
    inner: types.Record

outer: public(Outer)
"""

    def project(name: str, fields: tuple[str, ...]) -> Path:
        root = tmp_path / name
        root.mkdir()
        (root / "types.vy").write_text(
            TYPES_MODULE.format(fields="\n    ".join(fields), members="A")
        )
        entry = root / "main.vy"
        entry.write_text(entry_text)
        return entry

    fields = ("left: uint256", "right: uint256")
    source = _captured_source_compile(project("source", fields), "0.4.3")

    for entry, differences in (
        (project("copy", fields), []),
        (project("swapped", fields[::-1]), ["changed storage type definition: outer (Outer)"]),
    ):
        target = _overlay_target_compile(entry, "0.4.3")
        assert source.status == target.status == "passed"
        assert compare_artifact_details(source, target)[2] == differences
        assert compare_artifacts(source, target)[2] is (not differences)
