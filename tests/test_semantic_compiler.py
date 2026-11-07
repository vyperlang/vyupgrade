from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version
from uv import find_uv_bin

from vyupgrade.ast_facts import iter_nodes, root_ast
from vyupgrade.cli import main
from vyupgrade.compiler import (
    CompileResult,
    compile_source_file,
    resolve_import_closure,
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


def test_private_facts_distinguish_identical_calls_by_source_node(tmp_path: Path) -> None:
    contract = tmp_path / "route.vy"
    contract.write_text(SCOPED_CALLS)
    config = Config(paths=(contract,), target_version="0.4.3")
    managed = compile_source_file(contract, config, "0.3.10")
    artifacts = validated_artifacts(managed, "0.3.10", "source-exact")
    facts = artifacts["external_calls_v1"]
    assert facts["source_sha256sum"] == hashlib.sha256(contract.read_bytes()).hexdigest()

    calls = sorted(facts["calls"], key=lambda call: int(call["src"].split(":")[0]))
    assert [(call["interface"], call["method"], call["mutability"]) for call in calls] == [
        ("Read", "probe", "view"),
        ("Write", "probe", "nonpayable"),
    ]
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
    explicit = validated_artifacts(result, "0.3.10", "explicit-executable")
    assert "external_calls_v1" not in explicit
    for name in ("abi", "method_identifiers", "layout", "bytecode", "ast"):
        assert explicit[name] == artifacts[name]


def test_migration_writes_compiler_resolved_calls(tmp_path: Path) -> None:
    path = tmp_path / "route.vy"
    path.write_text(SCOPED_CALLS)
    assert main([str(path), "--write"]) == 0
    assert "return staticcall oracle.probe()" in path.read_text()
    assert "return extcall oracle.probe()" in path.read_text()
    assert main([str(path), "--check"]) == 0


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


@pytest.mark.parametrize("mode", ["managed", "explicit", "optional-query-fails"])
def test_public_b1_error_resolves_to_an_imported_definition(tmp_path: Path, mode: str) -> None:
    contract = tmp_path / "main.vy"
    contract.write_text(
        "#pragma version 0.5.0b1\nimport errors\n\n@external\n"
        "def withdraw(amount: uint256):\n"
        "    raise errors.InsufficientBalance(self.balance, amount)\n"
    )
    (tmp_path / "errors.vy").write_text(
        "#pragma version 0.5.0b1\nerror InsufficientBalance:\n"
        "    available: uint256\n    required: uint256\n"
    )
    executable = None if mode == "managed" else explicit_vyper("0.5.0b1", tmp_path)
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
        source_snapshot=resolve_import_closure({contract: contract.read_text()}, (tmp_path,)),
    )
    result = compile_source_file(contract, config, "0.5.0b1")
    authority = "source-exact" if mode == "managed" else "explicit-executable"
    artifacts = validated_artifacts(result, "0.5.0b1", authority)
    assert "external_calls_v1" not in artifacts
    if mode == "optional-query-fails":
        assert "annotated_ast" not in artifacts
        return
    annotated = artifacts["annotated_ast"]
    (call,) = iter_nodes(root_ast(annotated), "Call")
    declaration = call["func"]["type"]["type_t"]["type_decl_node"]
    (imported,) = annotated["imports"]
    (error,) = iter_nodes(imported, "ErrorDef")
    assert error["name"] == "InsufficientBalance"
    assert declaration == {"node_id": error["node_id"], "source_id": imported["source_id"]}
    assert root_ast(artifacts["ast"])["ast_type"] == "Module"
