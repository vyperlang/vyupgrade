from __future__ import annotations

import json
from pathlib import Path

from vyupgrade.cli import main


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
    assert data["files"][0]["changed"] is True
    assert any(fix["rule"] == "VY002" for fix in data["files"][0]["fixes"])

