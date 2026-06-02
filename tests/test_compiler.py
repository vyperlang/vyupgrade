from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vyupgrade.compiler import (
    CompileResult,
    _compiler_command,
    _run_compile,
    _supports_warning_policy,
    _target_validation_source,
    _uv_bin,
    compare_artifact_details,
    compare_artifacts,
    compile_source_ast,
    compile_source_file,
    compile_target_source,
    target_overlay,
)
from vyupgrade.models import Config


@pytest.fixture(autouse=True)
def clear_uv_bin_cache():
    _uv_bin.cache_clear()
    yield
    _uv_bin.cache_clear()


def test_compiler_command_uses_packaged_uv_bin(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.3.7", None)[0] == "/tmp/uv"


def test_compiler_command_pins_python_for_uv(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.3.7", None) == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.11",
        "--with",
        "vyper==0.3.7",
        "vyper",
    ]


def test_compiler_command_allows_python_override(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.4.3", "3.12") == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.12",
        "--with",
        "vyper==0.4.3",
        "vyper",
    ]


def test_compiler_command_uses_older_python_for_legacy_vyper(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    assert _compiler_command(None, "0.2.16", None) == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.8",
        "--with",
        "vyper==0.2.16",
        "vyper",
    ]


def test_compiler_command_uses_typed_ast_runner_for_legacy_prerelease(monkeypatch) -> None:
    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", lambda: "/tmp/uv")

    command = _compiler_command(None, "0.1.0b4", None)

    assert command[:9] == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.8",
        "--with",
        "vyper==0.1.0b4",
        "--with",
        "typed-ast",
    ]
    assert command[9] == "python"
    assert command[10].endswith("legacy_vyper.py")


def test_compiler_command_falls_back_when_uv_lookup_is_broken(monkeypatch) -> None:
    def broken_find_uv_bin() -> str:
        raise TypeError('can only concatenate str (not "NoneType") to str')

    monkeypatch.setattr("vyupgrade.compiler.find_uv_bin", broken_find_uv_bin)
    monkeypatch.setattr("vyupgrade.compiler.shutil.which", lambda name: f"/fallback/{name}")

    assert _compiler_command(None, "0.4.3", None)[0] == "/fallback/uv"


def test_explicit_compiler_path_skips_uv_wrapper() -> None:
    assert _compiler_command("/tmp/vyper", "0.3.7", "3.11") == ["/tmp/vyper"]


def test_compile_retries_without_unsupported_layout(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "-f" in command and command[command.index("-f") + 1] == "abi,method_identifiers,layout":
            return subprocess.CompletedProcess(command, 1, "", "ValueError: Unsupported format type 'layout'")
        return subprocess.CompletedProcess(command, 0, "[]\n{}\n", "")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(["vyper"], Path("/tmp/contract.vy"), Config(paths=(Path("/tmp/contract.vy"),)))

    assert result.status == "passed"
    assert result.artifacts == {"abi": [], "method_identifiers": {}}
    assert calls[0][calls[0].index("-f") + 1] == "abi,method_identifiers,layout"
    assert calls[1][calls[1].index("-f") + 1] == "abi,method_identifiers"


def test_compile_retries_without_legacy_keyerror_format(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        requested = command[command.index("-f") + 1]
        if "method_identifiers" in requested:
            return subprocess.CompletedProcess(command, 1, "", "KeyError: 'method_identifiers'")
        if "ast" in requested:
            return subprocess.CompletedProcess(command, 1, "", "KeyError: 'ast'")
        return subprocess.CompletedProcess(command, 0, "[]\n", "")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile_with_formats_for_test(["vyper"], Path("/tmp/contract.vy"), ("abi", "method_identifiers", "ast"))

    assert result.status == "passed"
    assert calls[-1][calls[-1].index("-f") + 1] == "abi"


def test_compile_retries_without_ast_for_legacy_span_error(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        requested = command[command.index("-f") + 1]
        if requested in {
            "abi,method_identifiers,layout,ast",
            "abi,method_identifiers,layout",
            "abi,method_identifiers",
        }:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "ValueError: start (57,7) precedes previous end (58,0)",
            )
        return subprocess.CompletedProcess(command, 0, "[]\n", "")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile_with_formats_for_test(
        ["vyper"], Path("/tmp/contract.vy"), ("abi", "method_identifiers", "layout", "ast")
    )

    assert result.status == "passed"
    assert result.artifacts == {"abi": []}
    assert calls[-1][calls[-1].index("-f") + 1] == "abi"


def _run_compile_with_formats_for_test(command: list[str], path: Path, formats: tuple[str, ...]) -> CompileResult:
    from vyupgrade.compiler import _run_compile_with_formats

    return _run_compile_with_formats(command, path, Config(paths=(path,)), formats, (), False)


def test_compile_installs_declared_vyper_import_dependencies(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    contracts = project / "contracts"
    contracts.mkdir(parents=True)
    contract = contracts / "AMM.vy"
    contract.write_text(
        """#pragma version 0.4.3
from snekmate.utils import math
""",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        """[tool.poetry.dependencies]
python = ">=3.11,<4"
snekmate = "0.1.2"
""",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "[]\n{}\n{}\n", "")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(
        ["/tmp/uv", "run", "--no-project", "--python", "3.11", "--with", "vyper==0.4.3", "vyper"],
        contract,
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert calls[0][:9] == [
        "/tmp/uv",
        "run",
        "--no-project",
        "--python",
        "3.11",
        "--with",
        "vyper==0.4.3",
        "--with",
        "snekmate==0.1.2",
    ]
    assert calls[0][9] == "vyper"
    assert ["-p", str(project)] == calls[0][calls[0].index("-p") : calls[0].index("-p") + 2]


def test_compile_skips_search_paths_for_legacy_prerelease_cli(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "[]\n{}\n{}\n", "")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(
        ["/tmp/uv", "run", "--no-project", "--python", "3.8", "--with", "vyper==0.1.0b4", "vyper"],
        contract,
        Config(paths=(contract,), compiler_search_paths=(tmp_path,)),
        extra_paths=(tmp_path,),
    )

    assert result.status == "passed"
    assert "-p" not in calls[0]


def test_compile_inserts_import_dependencies_before_legacy_runner(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contracts" / "contract.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("from snekmate.utils import math\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        """[tool.poetry.dependencies]
python = ">=3.11,<4"
snekmate = "0.1.2"
""",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "[]\n{}\n{}\n", "")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(
        [
            "/tmp/uv",
            "run",
            "--no-project",
            "--python",
            "3.8",
            "--with",
            "vyper==0.1.0b4",
            "--with",
            "typed-ast",
            "python",
            "/tmp/legacy_vyper.py",
        ],
        contract,
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert calls[0][9:11] == ["--with", "snekmate==0.1.2"]
    assert calls[0][11:13] == ["python", "/tmp/legacy_vyper.py"]


def test_compile_can_suppress_modern_vyper_warnings(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "[]\n{}\n{}\n", "")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(
        ["vyper"],
        Path("/tmp/contract.vy"),
        Config(paths=(Path("/tmp/contract.vy"),)),
        suppress_warnings=True,
    )

    assert result.status == "passed"
    assert calls[0][calls[0].index("-W") + 1] == "none"


def test_compile_reports_timeout_as_failure(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(["vyper"], Path("/tmp/contract.vy"), Config(paths=(Path("/tmp/contract.vy"),)))

    assert result.status == "failed"
    assert result.stderr == "compiler timed out after 120 seconds"
    assert result.command is not None


def test_compile_retries_missing_pyproject_import_dependency(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contracts" / "utils" / "contract.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.3\nfrom snekmate.utils import math\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.poetry.dependencies]
python = ">=3.11,<4"
snekmate = "0.1.2"
curve-std = {git = "https://github.com/curvefi/curve-std.git", rev = "09ad21756cd573cd6ac7afb32fb299fef32429cc"}
""",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="vyper.exceptions.ModuleNotFound: curve_std.stableswap.lp_oracle_2",
            )
        return subprocess.CompletedProcess(command, 0, stdout="[]\n{}\n{}\n", stderr="")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(
        ["/tmp/uv", "run", "--no-project", "--python", "3.11", "--with", "vyper==0.4.3", "vyper"],
        contract,
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert len(calls) == 2
    assert "snekmate==0.1.2" in calls[0]
    assert "curve-std @ git+https://github.com/curvefi/curve-std.git@09ad21756cd573cd6ac7afb32fb299fef32429cc" in calls[1]


def test_compile_installs_common_import_dependency_without_pyproject(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contracts" / "contract.vy"
    contract.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.3\nfrom snekmate.tokens import erc721\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[]\n{}\n{}\n", stderr="")

    monkeypatch.setattr("vyupgrade.compiler.subprocess.run", fake_run)

    result = _run_compile(
        ["/tmp/uv", "run", "--no-project", "--python", "3.11", "--with", "vyper==0.4.3", "vyper"],
        contract,
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert "snekmate" in calls[0]


def test_compile_source_ast_requests_ast_format(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.4.3\n", encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_compiler_command(explicit: str | None, version: str | None, python: str | None) -> list[str]:
        calls["compiler"] = (explicit, version, python)
        return ["vyper"]

    def fake_run_compile_with_formats(
        command: list[str],
        path: Path,
        config: Config,
        formats: tuple[str, ...],
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls["run"] = (command, path, formats, extra_paths, suppress_warnings)
        return CompileResult("passed", artifacts={"ast": {"ast_type": "Module"}})

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", fake_compiler_command)
    monkeypatch.setattr("vyupgrade.compiler._run_compile_with_formats", fake_run_compile_with_formats)

    result = compile_source_ast(contract, Config(paths=(contract,)), None)

    assert result.status == "passed"
    assert calls["compiler"] == (None, "0.4.3", None)
    assert calls["run"] == (["vyper"], contract, ("ast",), (), True)


def test_compile_source_file_requests_ast_with_validation_outputs(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.4.3\n", encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_compiler_command(explicit: str | None, version: str | None, python: str | None) -> list[str]:
        calls["compiler"] = (explicit, version, python)
        return ["vyper"]

    def fake_run_compile_with_formats(
        command: list[str],
        path: Path,
        config: Config,
        formats: tuple[str, ...],
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls["run"] = (command, path, formats, extra_paths, suppress_warnings)
        return CompileResult("passed", artifacts={"ast": {"ast_type": "Module"}})

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", fake_compiler_command)
    monkeypatch.setattr("vyupgrade.compiler._run_compile_with_formats", fake_run_compile_with_formats)

    result = compile_source_file(contract, Config(paths=(contract,)), None)

    assert result.status == "passed"
    assert calls["compiler"] == (None, "0.4.3", None)
    assert calls["run"] == (["vyper"], contract, ("abi", "method_identifiers", "layout", "ast"), (), True)


def test_compile_source_file_retries_legacy_span_error_with_final_newline(
    monkeypatch, tmp_path
) -> None:
    contract = tmp_path / "contract.vy"
    source = "# @version 0.2.8\n# eof"
    contract.write_text(source, encoding="utf-8")
    calls: list[tuple[Path, bytes]] = []

    def fake_run_compile_with_formats(
        command: list[str],
        path: Path,
        config: Config,
        formats: tuple[str, ...],
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls.append((path, path.read_bytes()))
        if len(calls) == 1:
            return CompileResult(
                "failed", stderr="ValueError: start (2,0) precedes previous end (3,0)"
            )
        return CompileResult("passed", artifacts={"abi": []})

    monkeypatch.setattr("vyupgrade.compiler._prepare_command", lambda *args: (["vyper"], False))
    monkeypatch.setattr("vyupgrade.compiler._run_compile_with_formats", fake_run_compile_with_formats)

    result = compile_source_file(contract, Config(paths=(contract,)), "0.2.8")

    assert result.status == "passed"
    assert calls[0] == (contract, source.encode())
    assert calls[1][0].parent == contract.parent
    assert calls[1][0] != contract
    assert calls[1][1] == source.encode() + b"\n"
    assert not calls[1][0].exists()
    assert contract.read_text(encoding="utf-8") == source


def test_compile_target_source_enables_decimals_for_decimal_code(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.3.10\n", encoding="utf-8")
    calls: dict[str, object] = {}

    def fake_compiler_command(explicit: str | None, version: str | None, python: str | None) -> list[str]:
        calls["compiler"] = (explicit, version, python)
        return ["vyper"]

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls["run"] = (command, config.enable_decimals, extra_paths, suppress_warnings)
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", fake_compiler_command)
    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(
        contract,
        "#pragma version 0.4.3\n@external\ndef f(x: decimal) -> decimal:\n    return x / 2.0\n",
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert calls["compiler"] == (None, "0.4.3", None)
    assert calls["run"] == (["vyper"], True, (contract.parent,), True)


def test_compile_target_source_bumps_temp_pragma_for_validation(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.2.11\n", encoding="utf-8")
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls["source"] = path.read_text(encoding="utf-8")
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(
        contract,
        "#pragma version 0.2.11\n@external\ndef f():\n    pass\n",
        Config(paths=(contract,), target_version="0.4.3"),
    )

    assert result.status == "passed"
    assert "#pragma version 0.4.3" in calls["source"]
    assert "#pragma version 0.2.11" not in calls["source"]


def test_target_validation_source_removes_duplicate_vyper_pragmas() -> None:
    source = "# @version 0.3.10\n# \u00a0@version ^0.2.11\n# pragma solidity ^0.8.0\n"

    result = _target_validation_source(source, "0.4.3")

    assert result == "#pragma version 0.4.3\n\n"


def test_target_validation_source_strips_natspec_docstrings() -> None:
    source = '''# @version 0.3.10
"""
@custom:dev first
@custom:dev duplicate
"""

@external
def f():
    """
    @returns legacy tag
    """
    pass
'''

    result = _target_validation_source(source, "0.4.3")

    assert '"""' not in result
    assert "@custom:dev" not in result
    assert "@returns" not in result
    assert "@external\ndef f():" in result


def test_target_validation_source_keeps_assigned_triple_quoted_strings() -> None:
    source = '''# @version 0.3.10
VALUE: constant(String[32]) = """literal"""
'''

    result = _target_validation_source(source, "0.4.3")

    assert 'VALUE: constant(String[32]) = """literal"""' in result


def test_compile_target_source_uses_source_dir_for_relative_imports(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contracts" / "contract.vy"
    contract.parent.mkdir()
    contract.write_text("# @version 0.4.0\n", encoding="utf-8")
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls["target_parent"] = path.parent
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(contract, "#pragma version 0.4.3\n", Config(paths=(contract,)))

    assert result.status == "passed"
    assert calls["target_parent"] == contract.parent


def test_target_overlay_rewrites_imported_vyper_pragmas(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "exchanges" / "sfrxusd.vy"
    imported = project / "src" / "interfaces" / "IExchange.vyi"
    contract.parent.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.1\nfrom src.interfaces import IExchange\n",
        encoding="utf-8",
    )
    imported.write_text(
        '# @version 0.4.1\n@external\n@view\ndef quote() -> uint256:\n    """docs"""\n    return ...\n',
        encoding="utf-8",
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls["path"] = path
        calls["search_paths"] = config.compiler_search_paths
        imported_overlay = path.parents[1] / "interfaces" / "IExchange.vyi"
        calls["imported_source"] = imported_overlay.read_text(encoding="utf-8")
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom src.interfaces import IExchange\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        result = compile_target_source(
            contract,
            "#pragma version 0.4.3\nfrom src.interfaces import IExchange\n",
            Config(paths=(contract,), compiler_search_paths=(project,)),
            overlay,
        )

    assert result.status == "passed"
    assert calls["path"] != contract
    assert calls["search_paths"][0].name.startswith("vyupgrade-target-")
    assert "#pragma version 0.4.3" in calls["imported_source"]
    assert "# @version 0.4.1" not in calls["imported_source"]
    assert "def quote() -> uint256: ..." in calls["imported_source"]
    assert '"""docs"""' not in calls["imported_source"]
    assert "return ..." not in calls["imported_source"]


def test_target_overlay_rewrites_imported_dependency_modules(monkeypatch, tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "contracts" / "LpSugar.vy"
    imported = project / "contracts" / "modules" / "lp_shared.vy"
    contract.parent.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.0\nfrom contracts.modules import lp_shared\n",
        encoding="utf-8",
    )
    imported.write_text(
        "# @version 0.4.0\nfrom snekmate.utils import create2_address\n"
        "def f(salt: bytes32, init_hash: bytes32, factory: address) -> address:\n"
        "    return create2_address._compute_address(salt, init_hash, factory)\n",
        encoding="utf-8",
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        imported_overlay = path.parent / "modules" / "lp_shared.vy"
        calls["imported_source"] = imported_overlay.read_text(encoding="utf-8")
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom contracts.modules import lp_shared\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        result = compile_target_source(
            contract,
            "#pragma version 0.4.3\nfrom contracts.modules import lp_shared\n",
            Config(paths=(contract,), compiler_search_paths=(project,)),
            overlay,
        )

    assert result.status == "passed"
    assert "from snekmate.utils import create2" in calls["imported_source"]
    assert "create2._compute_create2_address(salt, init_hash, factory)" in calls[
        "imported_source"
    ]


def test_target_overlay_skips_unrelated_sibling_sources(tmp_path) -> None:
    project = tmp_path / "flat"
    contract = project / "main.vy"
    dependency = project / "dependency.vy"
    unrelated = project / "unrelated.vy"
    project.mkdir()
    contract.write_text("# @version 0.4.0\nimport dependency\n", encoding="utf-8")
    dependency.write_text("# @version 0.4.0\nx: uint256\n", encoding="utf-8")
    unrelated.write_text("# @version 0.4.0\ny: uint256\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nimport dependency\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_root = overlay_contract.parent
        assert (overlay_root / "dependency.vy").exists()
        assert not (overlay_root / "unrelated.vy").exists()


def test_target_overlay_preserves_shallow_absolute_import_roots(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "strategy.vy"
    dependency = project / "src" / "modules" / "constants.vy"
    contract.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom src.modules import constants\n", encoding="utf-8")
    dependency.write_text("# @version 0.4.0\nMAX_BPS: constant(uint256) = 10_000\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom src.modules import constants\n"},
        "0.4.3",
        (project, project / "src"),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_project = overlay_contract.parents[1]
        assert overlay_contract.relative_to(overlay_project) == Path("src/strategy.vy")
        assert (overlay_project / "src" / "modules" / "constants.vy").exists()


def test_target_overlay_copies_json_interfaces(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "validator.vy"
    interface = project / "src" / "interfaces" / "IValidator.json"
    contract.parent.mkdir(parents=True)
    interface.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.3\nfrom src.interfaces import IValidator\n", encoding="utf-8")
    interface.write_text("[]\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom src.interfaces import IValidator\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_project = overlay_contract.parents[1]
        assert (overlay_project / "src" / "interfaces" / "IValidator.json").exists()


def test_target_overlay_copies_create2_address_under_rewritten_name(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "factory.vy"
    dependency = project / "snekmate" / "utils" / "create2_address.vy"
    contract.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom snekmate.utils import create2_address\n", encoding="utf-8")
    dependency.write_text(
        "# @version 0.4.0\n"
        "def _compute_address(salt: bytes32, init_hash: bytes32, factory: address) -> address:\n"
        "    return empty(address)\n",
        encoding="utf-8",
    )

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom snekmate.utils import create2\n"},
        "0.4.3",
        (project,),
    ) as overlay:
        assert overlay is not None
        overlay_contract = overlay.paths[contract.resolve()]
        overlay_project = overlay_contract.parents[1]
        assert (overlay_project / "snekmate" / "utils" / "create2.vy").exists()


def test_target_overlay_resolves_dependency_imports_from_search_roots(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "token.vy"
    erc20 = project / "lib" / "pypi" / "snekmate" / "tokens" / "erc20.vy"
    permit = erc20.parent / "interfaces" / "IERC20Permit.vyi"
    contract.parent.mkdir(parents=True)
    erc20.parent.mkdir(parents=True)
    permit.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom snekmate.tokens import erc20\n", encoding="utf-8")
    erc20.write_text(
        "# @version 0.4.0\nimport interfaces.IERC20Permit as IERC20Permit\n",
        encoding="utf-8",
    )
    permit.write_text("# @version 0.4.0\n@external\ndef permit(): ...\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom snekmate.tokens import erc20\n"},
        "0.4.3",
        (project / "lib" / "pypi", project / "src", project),
    ) as overlay:
        assert overlay is not None
        assert (overlay.root / "lib" / "pypi" / "snekmate" / "tokens" / "erc20.vy").exists()
        assert (
            overlay.root
            / "lib"
            / "pypi"
            / "snekmate"
            / "tokens"
            / "interfaces"
            / "IERC20Permit.vyi"
        ).exists()


def test_target_overlay_preserves_nested_package_search_roots(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "gnosis" / "TokenizedValidator.vy"
    ownable = (
        project
        / "lib"
        / "github"
        / "pcaversaccio"
        / "snekmate"
        / "src"
        / "snekmate"
        / "auth"
        / "ownable.vy"
    )
    contract.parent.mkdir(parents=True)
    ownable.parent.mkdir(parents=True)
    contract.write_text(
        "# @version 0.4.3\n"
        "from pcaversaccio.snekmate.src.snekmate.auth import ownable\n",
        encoding="utf-8",
    )
    ownable.write_text("# @version 0.4.3\nOWNER: public(address)\n", encoding="utf-8")

    with target_overlay(
        {
            contract: (
                "#pragma version 0.4.3\n"
                "from pcaversaccio.snekmate.src.snekmate.auth import ownable\n"
            )
        },
        "0.4.3",
        (project / "lib", project / "lib" / "github", project / "src", project),
    ) as overlay:
        assert overlay is not None
        assert (
            overlay.root
            / "lib"
            / "github"
            / "pcaversaccio"
            / "snekmate"
            / "src"
            / "snekmate"
            / "auth"
            / "ownable.vy"
        ).exists()
        assert overlay.root / "lib" / "github" in overlay.search_paths


def test_target_overlay_copies_imported_site_package_sources(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "curve_stablecoin" / "mpolicies" / "AggMonetaryPolicy4.vy"
    ema = project / "venv" / "lib" / "python3.10" / "site-packages" / "curve_std" / "ema.vy"
    contract.parent.mkdir(parents=True)
    ema.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.3\nfrom curve_std import ema\n", encoding="utf-8")
    ema.write_text("# @version 0.4.3\nWINDOW: constant(uint256) = 10\n", encoding="utf-8")

    site_packages = project / "venv" / "lib" / "python3.10" / "site-packages"
    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom curve_std import ema\n"},
        "0.4.3",
        (project, site_packages),
    ) as overlay:
        assert overlay is not None
        assert (overlay.root / "venv" / "lib" / "python3.10" / "site-packages" / "curve_std" / "ema.vy").exists()
        assert overlay.root / "venv" / "lib" / "python3.10" / "site-packages" in overlay.search_paths


def test_target_overlay_resolves_relative_dependency_imports(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "token.vy"
    erc20 = project / "src" / "erc20.vy"
    interface = project / "src" / "utils" / "interfaces" / "IERC5267.vyi"
    contract.parent.mkdir(parents=True)
    interface.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom . import erc20\n", encoding="utf-8")
    erc20.write_text(
        "# @version 0.4.0\nfrom .utils.interfaces import IERC5267\n",
        encoding="utf-8",
    )
    interface.write_text("# @version 0.4.0\n@external\ndef eip712Domain(): ...\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom . import erc20\n"},
        "0.4.3",
        (project / "src", project),
    ) as overlay:
        assert overlay is not None
        assert (overlay.root / "src" / "erc20.vy").exists()
        assert (overlay.root / "src" / "utils" / "interfaces" / "IERC5267.vyi").exists()


def test_target_overlay_rewrites_standard_json_src_package_imports(tmp_path) -> None:
    project = tmp_path / "project"
    contract = project / "src" / "token.vy"
    erc20 = project / "src" / "erc20.vy"
    interface = project / "utils" / "interfaces" / "IERC5267.vyi"
    contract.parent.mkdir(parents=True)
    interface.parent.mkdir(parents=True)
    contract.write_text("# @version 0.4.0\nfrom . import erc20\n", encoding="utf-8")
    erc20.write_text(
        "# @version 0.4.0\nfrom .utils.interfaces import IERC5267\n",
        encoding="utf-8",
    )
    interface.write_text("# @version 0.4.0\n@external\ndef eip712Domain(): ...\n", encoding="utf-8")

    with target_overlay(
        {contract: "#pragma version 0.4.3\nfrom . import erc20\n"},
        "0.4.3",
        (project / "src", project),
    ) as overlay:
        assert overlay is not None
        erc20_overlay = overlay.root / "src" / "erc20.vy"
        assert "from ..utils.interfaces import IERC5267" in erc20_overlay.read_text(
            encoding="utf-8"
        )
        assert (overlay.root / "utils" / "interfaces" / "IERC5267.vyi").exists()


def test_compile_target_source_keeps_decimal_flag_off_without_decimal(monkeypatch, tmp_path) -> None:
    contract = tmp_path / "contract.vy"
    contract.write_text("# @version 0.3.10\n", encoding="utf-8")
    calls: dict[str, object] = {}

    monkeypatch.setattr("vyupgrade.compiler._compiler_command", lambda *_args: ["vyper"])

    def fake_run_compile(
        command: list[str],
        path: Path,
        config: Config,
        extra_paths: tuple[Path, ...],
        suppress_warnings: bool,
    ) -> CompileResult:
        calls["enable_decimals"] = config.enable_decimals
        return CompileResult("passed", artifacts={})

    monkeypatch.setattr("vyupgrade.compiler._run_compile", fake_run_compile)

    result = compile_target_source(
        contract,
        "#pragma version 0.4.3\n@external\ndef f(x: uint256) -> uint256:\n    return x // 2\n",
        Config(paths=(contract,)),
    )

    assert result.status == "passed"
    assert calls["enable_decimals"] is False


def test_warning_policy_is_only_used_for_modern_vyper() -> None:
    assert not _supports_warning_policy("0.4.0")
    assert _supports_warning_policy("0.4.1")
    assert _supports_warning_policy("0.4.3")
    assert not _supports_warning_policy("0.3.10")


def test_compare_artifacts_canonicalizes_abi_constructor_and_gas() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {"type": "constructor", "stateMutability": "nonpayable", "inputs": [], "outputs": []},
                {
                    "type": "function",
                    "name": "f",
                    "stateMutability": "view",
                    "inputs": [],
                    "outputs": [{"type": "uint256", "name": ""}],
                    "gas": 1234,
                },
            ],
            "method_identifiers": {"__init__(string,string,uint256)": "0xdeadbeef", "f()": "0x26121ff0"},
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "f",
                    "stateMutability": "view",
                    "inputs": [],
                    "outputs": [{"type": "uint256", "name": ""}],
                },
                {"type": "constructor", "stateMutability": "nonpayable", "inputs": [], "outputs": []},
            ],
            "method_identifiers": {"f()": "0x26121ff0"},
        },
    )

    assert compare_artifacts(source, target) == (True, True, None)


def test_compare_artifacts_treats_pure_and_view_as_readonly_abi() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "target",
                    "stateMutability": "pure",
                    "inputs": [],
                    "outputs": [{"type": "address", "name": ""}],
                }
            ]
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "target",
                    "stateMutability": "view",
                    "inputs": [],
                    "outputs": [{"type": "address", "name": ""}],
                }
            ]
        },
    )

    assert compare_artifacts(source, target) == (True, None, None)
    assert compare_artifact_details(source, target)[0] == []


def test_compare_artifact_details_ignores_constructor_selector() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "method_identifiers": {
                "__init__(string,string,uint256)": "0xcece287e",
                "f()": "0x26121ff0",
            },
        },
    )
    target = CompileResult(
        "passed",
        artifacts={"method_identifiers": {"f()": "0x26121ff0"}},
    )

    _abi_diff, method_diff, _storage_diff = compare_artifact_details(source, target)

    assert method_diff == []


def test_compare_artifact_details_reports_abi_output_shape_changes() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "points_sum",
                    "stateMutability": "view",
                    "inputs": [
                        {"name": "arg0", "type": "int128"},
                        {"name": "arg1", "type": "uint256"},
                    ],
                    "outputs": [
                        {"name": "bias", "type": "uint256"},
                        {"name": "slope", "type": "uint256"},
                    ],
                }
            ],
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "abi": [
                {
                    "type": "function",
                    "name": "points_sum",
                    "stateMutability": "view",
                    "inputs": [
                        {"name": "arg0", "type": "int128"},
                        {"name": "arg1", "type": "uint256"},
                    ],
                    "outputs": [
                        {
                            "name": "",
                            "type": "tuple",
                            "components": [
                                {"name": "bias", "type": "uint256"},
                                {"name": "slope", "type": "uint256"},
                            ],
                        }
                    ],
                }
            ],
        },
    )

    abi_diff, _method_diff, _storage_diff = compare_artifact_details(source, target)

    assert abi_diff == [
        "changed ABI entry: function points_sum(int128, uint256): outputs (bias: uint256, slope: uint256) -> (tuple(bias: uint256, slope: uint256))",
    ]


def test_compare_artifacts_normalizes_storage_layout_shapes() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "token": {"location": "storage", "slot": 0, "type": "ERC20"},
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
                "pool": {"location": "storage", "slot": 2, "type": "interface IPool"},
                "factories": {
                    "location": "storage",
                    "slot": 3,
                    "type": "HashMap[address, interface IFactory]",
                },
                "loan": {
                    "location": "storage",
                    "slot": 4,
                    "type": "HashMap[address, Loan declaration object]",
                },
                "token_owner": {
                    "location": "storage",
                    "slot": 5,
                    "type": "HashMap[uint256, address][uint256, address]",
                },
                "owner_tokens": {
                    "location": "storage",
                    "slot": 6,
                    "type": "HashMap[address, HashMap[uint256, uint256][uint256, uint256]][address, HashMap[uint256, uint256][uint256, uint256]]",
                },
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "token": {"slot": 0, "type": "/tmp/IERC20.vyi", "n_slots": 1},
                    "balance": {"slot": 1, "type": "uint256", "n_slots": 1},
                    "pool": {"slot": 2, "type": "IPool", "n_slots": 1},
                    "factories": {
                        "slot": 3,
                        "type": "HashMap[address, IFactory]",
                        "n_slots": 1,
                    },
                    "loan": {
                        "slot": 4,
                        "type": "HashMap[address, Loan]",
                        "n_slots": 1,
                    },
                    "token_owner": {
                        "slot": 5,
                        "type": "HashMap[uint256, address]",
                        "n_slots": 1,
                    },
                    "owner_tokens": {
                        "slot": 6,
                        "type": "HashMap[address, HashMap[uint256, uint256]]",
                        "n_slots": 1,
                    },
                },
                "transient_storage_layout": {
                    "$.nonreentrant_key": {"slot": 0, "type": "nonreentrant lock"},
                },
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifacts_flags_real_storage_slot_shift() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.withdraw": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "balance": {"slot": 0, "type": "uint256", "n_slots": 1},
                },
                "transient_storage_layout": {
                    "$.nonreentrant_key": {"slot": 0, "type": "nonreentrant lock"},
                },
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, False)


def test_compare_artifacts_normalizes_storage_nonreentrant_lock_names() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "$.nonreentrant_key": {
                        "slot": 0,
                        "type": "nonreentrant lock",
                        "n_slots": 1,
                    },
                    "balance": {"slot": 1, "type": "uint256", "n_slots": 1},
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifacts_normalizes_generated_nonreentrant_storage_gap() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "_vyupgrade_reentrancy_lock_slot": {
                        "slot": 0,
                        "type": "uint256",
                        "n_slots": 1,
                    },
                    "balance": {"slot": 1, "type": "uint256", "n_slots": 1},
                }
            }
        },
    )

    assert compare_artifacts(source, target) == (None, None, True)


def test_compare_artifact_details_reports_nonreentrant_lock_moved_to_transient_storage() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "nonreentrant.lock": {
                    "location": "storage",
                    "slot": 0,
                    "type": "nonreentrant lock",
                },
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            }
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "balance": {"slot": 0, "type": "uint256", "n_slots": 1},
                },
                "transient_storage_layout": {
                    "$.nonreentrant_key": {"slot": 0, "type": "nonreentrant lock", "n_slots": 1},
                },
            }
        },
    )

    _abi_diff, _method_diff, storage_diff = compare_artifact_details(source, target)

    assert storage_diff == [
        "moved storage to transient: $nonreentrant:0 slot 0 nonreentrant lock -> $nonreentrant:0 slot 0 nonreentrant lock",
        "changed storage: balance slot 1 uint256 -> 0 uint256",
    ]


def test_compare_artifact_details_reports_changed_selectors_and_storage() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "method_identifiers": {
                "old()": "0x11111111",
                "shared()": "0x22222222",
            },
            "layout": {
                "owner": {"location": "storage", "slot": 0, "type": "address"},
                "balance": {"location": "storage", "slot": 1, "type": "uint256"},
            },
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "method_identifiers": {
                "shared()": "0x33333333",
                "new()": "0x44444444",
            },
            "layout": {
                "storage_layout": {
                    "balance": {"slot": 0, "type": "uint256"},
                    "recipient": {"slot": 1, "type": "address"},
                },
            },
        },
    )

    _abi_diff, method_diff, storage_diff = compare_artifact_details(source, target)

    assert method_diff == [
        "removed selector: old() = 0x11111111",
        "added selector: new() = 0x44444444",
        "changed selector: shared() 0x22222222 -> 0x33333333",
    ]
    assert storage_diff == [
        "removed storage: owner slot 0 address",
        "added storage: recipient slot 1 address",
        "changed storage: balance slot 1 uint256 -> 0 uint256",
    ]


def test_compare_artifact_details_reports_full_storage_changes() -> None:
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                f"slot_{index:02d}": {
                    "location": "storage",
                    "slot": index,
                    "type": "uint256",
                }
                for index in range(15)
            },
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    f"slot_{index:02d}": {"slot": index + 1, "type": "uint256"}
                    for index in range(15)
                },
            },
        },
    )

    _abi_diff, _method_diff, storage_diff = compare_artifact_details(source, target)

    assert len(storage_diff) == 15
    assert storage_diff[-1] == "changed storage: slot_14 slot 14 uint256 -> 15 uint256"
    assert not any(line.startswith("... ") for line in storage_diff)
