from __future__ import annotations

from vyupgrade.compiler import _compiler_command


def test_compiler_command_pins_python_for_uv() -> None:
    assert _compiler_command(None, "0.3.7", None) == [
        "uv",
        "run",
        "--python",
        "3.11",
        "--with",
        "vyper==0.3.7",
        "vyper",
    ]


def test_compiler_command_allows_python_override() -> None:
    assert _compiler_command(None, "0.4.3", "3.12") == [
        "uv",
        "run",
        "--python",
        "3.12",
        "--with",
        "vyper==0.4.3",
        "vyper",
    ]


def test_explicit_compiler_path_skips_uv_wrapper() -> None:
    assert _compiler_command("/tmp/vyper", "0.3.7", "3.11") == ["/tmp/vyper"]

