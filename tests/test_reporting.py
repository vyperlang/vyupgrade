from __future__ import annotations

from pathlib import Path

from vyupgrade.models import FileReport, RunReport
from vyupgrade.reporting import render_text


def test_render_text_includes_compile_errors() -> None:
    file_report = FileReport(
        path=Path("bad.vy"),
        changed=True,
        source_compile="passed",
        target_compile="failed",
        target_error='Version specification "0.2.11" is not compatible',
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])

    text = render_text(report)

    assert "target compile: failed" in text
    assert "target error:" in text
    assert 'Version specification "0.2.11" is not compatible' in text


def test_render_text_hides_stderr_for_successful_compiles() -> None:
    file_report = FileReport(
        path=Path("ok.vy"),
        changed=True,
        source_compile="passed",
        source_error="uv cache warning",
        target_compile="passed",
        target_error="warning output",
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])

    text = render_text(report)

    assert "source compile: passed" in text
    assert "target compile: passed" in text
    assert "source error:" not in text
    assert "target error:" not in text
    assert "uv cache warning" not in text
    assert "warning output" not in text
