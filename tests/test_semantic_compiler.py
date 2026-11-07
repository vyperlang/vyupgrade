from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version
from uv import find_uv_bin

from vyupgrade.ast_facts import iter_nodes, root_ast
from vyupgrade.compiler import (
    CompileResult,
    compile_source_file,
    resolve_import_closure,
    unavailable_validation_artifacts,
)
from vyupgrade.models import Config


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
