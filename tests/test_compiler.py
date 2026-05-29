from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vyupgrade.compiler import (
    CompileResult,
    _compiler_command,
    _run_compile,
    _supports_warning_policy,
    _uv_bin,
    compile_source_ast,
    compile_source_file,
    compile_target_source,
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
