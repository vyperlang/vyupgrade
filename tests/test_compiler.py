from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vyupgrade.compiler import _compiler_command, _run_compile, _supports_warning_policy, _uv_bin
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


def test_warning_policy_is_only_used_for_modern_vyper() -> None:
    assert _supports_warning_policy("0.4.0")
    assert _supports_warning_policy("0.4.3")
    assert not _supports_warning_policy("0.3.10")
