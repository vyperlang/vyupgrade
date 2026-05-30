from __future__ import annotations

import json
import shutil
from pathlib import Path

from vyupgrade.cli import main


def test_write_mode_validates_against_target_compiler(tmp_path: Path) -> None:
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


def test_alpha_target_validation_uses_alpha_compiler(tmp_path: Path) -> None:
    contract = tmp_path / "sqrt.vy"
    contract.write_text(
        """#pragma version 0.4.3
@external
def f(x: uint256) -> uint256:
    return isqrt(x)
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main(
        [
            str(contract),
            "--check",
            "--target-version",
            "0.5.0a2",
            "--report-json",
            str(report),
        ]
    )

    assert code == 1
    data = json.loads(report.read_text())
    file = data["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert any(fix["rule"] == "VY101" for fix in file["fixes"])
