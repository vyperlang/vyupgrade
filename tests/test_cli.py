from __future__ import annotations

import json
import shutil
from io import StringIO
from pathlib import Path

from vyupgrade.cli import _add_validation_diagnostics, _evm_default_diagnostic, _write_diff, main
from vyupgrade.models import Config, FileReport


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_check_mode_reports_changes(tmp_path: Path) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )

    report = tmp_path / "report.json"
    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code in {1, 2}
    data = json.loads(report.read_text())
    assert data["write_requested"] is False
    assert data["wrote_changes"] is False
    assert data["files"][0]["changed"] is True
    assert any(fix["rule"] == "VY002" for fix in data["files"][0]["fixes"])


def test_write_mode_is_idempotent_with_target_compile(tmp_path: Path) -> None:
    contract = tmp_path / "migration_03.vy"
    shutil.copyfile(Path("tests/fixtures/migration_03.vy"), contract)

    report = tmp_path / "report.json"
    code = main([str(contract), "--write", "--report-json", str(report)])

    assert code in {0, 3}
    rewritten = contract.read_text()
    assert "#pragma version 0.4.3" in rewritten
    assert "staticcall self.token.balanceOf(msg.sender)" in rewritten
    assert "for i: uint256 in range(3):" in rewritten
    data = json.loads(report.read_text())
    assert data["write_requested"] is True
    assert data["wrote_changes"] is True
    assert data["files"][0]["validation"]["target_compile"] == "passed"

    second_report = tmp_path / "second.json"
    second = main([str(contract), "--check", "--report-json", str(second_report)])
    assert second in {0, 3}
    assert json.loads(second_report.read_text())["files"][0]["changed"] is False


def test_write_mode_does_not_write_when_target_compile_fails(tmp_path: Path) -> None:
    contract = tmp_path / "bad.vy"
    original = """# @version 0.3.10
@external
def f(target: address):
    target.unknown()
"""
    contract.write_text(original, encoding="utf-8")

    code = main([str(contract), "--write"])

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original


def test_split_interfaces_writes_sibling_vyi_files(tmp_path: Path) -> None:
    contract = tmp_path / "Main.vy"
    contract.write_text(
        """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable

@external
def f(token: Token, owner: address) -> uint256:
    return staticcall token.balanceOf(owner)
""",
        encoding="utf-8",
    )

    code = main([str(contract), "--write", "--split-interfaces"])

    assert code == 0
    assert contract.read_text(encoding="utf-8") == """#pragma version 0.4.3

import Token
@external
def f(token: Token, owner: address) -> uint256:
    return staticcall token.balanceOf(owner)
"""
    assert (tmp_path / "Token.vyi").read_text(encoding="utf-8") == """@view
@external
def balanceOf(owner: address) -> uint256: ...
@external
def transfer(to: address, amount: uint256) -> bool: ...
"""


def test_split_interfaces_respects_rule_ignore(tmp_path: Path) -> None:
    contract = tmp_path / "Main.vy"
    contract.write_text(
        """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main(
        [
            str(contract),
            "--check",
            "--split-interfaces",
            "--ignore",
            "VY120",
            "--report-json",
            str(report),
        ]
    )

    assert code in {0, 2}
    data = json.loads(report.read_text())
    assert len(data["files"]) == 1
    assert data["files"][0]["changed"] is False
    assert not (tmp_path / "Token.vyi").exists()
    assert not any(fix["rule"] == "VY120" for fix in data["files"][0]["fixes"])


def test_target_validation_uses_rewritten_import_overlay(tmp_path: Path) -> None:
    (tmp_path / "lib.vy").write_text(
        """# pragma version 0.4.0
X: constant(uint256) = 1
""",
        encoding="utf-8",
    )
    (tmp_path / "main.vy").write_text(
        """# pragma version 0.4.0
import lib

@external
def x() -> uint256:
    return lib.X
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(tmp_path), "--check", "--report-json", str(report)])

    assert code == 1
    data = json.loads(report.read_text())
    assert {
        file["path"].rsplit("/", 1)[-1]: file["validation"]["target_compile"]
        for file in data["files"]
    } == {"lib.vy": "passed", "main.vy": "passed"}


def test_pyproject_config_paths(tmp_path: Path, monkeypatch) -> None:
    contract = tmp_path / "migration_03.vy"
    shutil.copyfile(Path("tests/fixtures/migration_03.vy"), contract)
    report = tmp_path / "configured-report.json"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f"""[tool.vyupgrade]
paths = ["{contract}"]
report-json = "{report}"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    code = main(["--check"])

    assert code in {1, 3}
    assert report.exists()


def test_select_limits_applied_rules(tmp_path: Path) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--select", "VY001", "--report-json", str(report)])

    assert code in {1, 2, 3}
    fixes = json.loads(report.read_text())["files"][0]["fixes"]
    assert {fix["rule"] for fix in fixes} == {"VY001"}


def test_diff_output_is_colored_for_tty(monkeypatch) -> None:
    stream = TtyStringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)

    _write_diff(
        [
            "--- old.vy\n",
            "+++ new.vy\n",
            "@@ -1 +1 @@\n",
            "-# @version 0.3.10\n",
            "+#pragma version 0.4.3\n",
            " unchanged\n",
        ],
        stream,
    )

    text = stream.getvalue()
    assert "\x1b[1m--- old.vy\n\x1b[0m" in text
    assert "\x1b[36m@@ -1 +1 @@\n\x1b[0m" in text
    assert "\x1b[31m-# @version 0.3.10\n\x1b[0m" in text
    assert "\x1b[32m+#pragma version 0.4.3\n\x1b[0m" in text
    assert " unchanged\n" in text


def test_diff_output_stays_plain_for_pipes() -> None:
    stream = StringIO()

    _write_diff(["-old\n", "+new\n"], stream)

    assert stream.getvalue() == "-old\n+new\n"


def test_diff_output_respects_no_color(monkeypatch) -> None:
    stream = TtyStringIO()
    monkeypatch.setenv("NO_COLOR", "1")

    _write_diff(["-old\n", "+new\n"], stream)

    assert stream.getvalue() == "-old\n+new\n"


def test_evm_default_diagnostic_reports_exact_change() -> None:
    diagnostic = _evm_default_diagnostic("0.3.7", "0.4.3")

    assert diagnostic is not None
    assert diagnostic.rule == "VYD009"
    assert diagnostic.message == (
        "default EVM version changed from paris (source compiler 0.3.7) "
        "to prague (target compiler 0.4.3); review or pin explicitly"
    )


def test_evm_default_diagnostic_tracks_patch_level_default_changes() -> None:
    diagnostic = _evm_default_diagnostic("0.4.2", "0.4.3")

    assert diagnostic is not None
    assert "cancun (source compiler 0.4.2) to prague (target compiler 0.4.3)" in diagnostic.message
    assert _evm_default_diagnostic("0.4.0", "0.4.2") is None


def test_validation_diagnostics_respect_rule_selection(tmp_path: Path) -> None:
    report = FileReport(path=tmp_path / "Contract.vy")
    report.source_compile = "failed"
    report.abi_equal = False
    report.storage_layout_equal = False
    config = Config(paths=(report.path,), select=frozenset({"VYD009"}))

    _add_validation_diagnostics(report, "0.3.7", config)

    assert [diagnostic.rule for diagnostic in report.diagnostics] == ["VYD009"]


def test_validation_diagnostics_respect_rule_ignore(tmp_path: Path) -> None:
    report = FileReport(path=tmp_path / "Contract.vy")
    config = Config(paths=(report.path,), ignore=frozenset({"VYD009"}))

    _add_validation_diagnostics(report, "0.3.7", config)

    assert not report.diagnostics


def test_source_newer_than_target_skips_compile_and_reports_error(tmp_path: Path) -> None:
    contract = tmp_path / "newer.vy"
    contract.write_text(
        """# pragma version >=0.5.0a1,<0.6.0

@external
def f() -> uint256:
    return 1
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code == 5
    data = json.loads(report.read_text())
    file_report = data["files"][0]
    assert file_report["changed"] is False
    assert file_report["diagnostics"][0]["rule"] == "VYD016"
    assert file_report["diagnostics"][0]["severity"] == "error"
    assert file_report["validation"]["source_compile"] == "skipped"
    assert file_report["validation"]["target_compile"] == "skipped"
