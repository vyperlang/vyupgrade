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
    compare_artifact_details,
    compare_artifacts,
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
        "moved storage to transient: nonreentrant.lock slot 0 nonreentrant lock -> $.nonreentrant_key slot 0 nonreentrant lock",
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
