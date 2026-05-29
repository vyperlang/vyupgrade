from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from vyupgrade.models import Diagnostic, FileReport, Fix, RunReport
from vyupgrade.reporting import THEME, render_rich, render_text, write_human_report


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


def test_render_text_groups_repeated_fixes_and_diagnostics_by_rule_message() -> None:
    file_report = FileReport(
        path=Path("grouped.vy"),
        changed=True,
        fixes=[
            Fix("VY112", 10, "changed positional event log to keyword arguments", "", ""),
            Fix("VY112", 20, "changed positional event log to keyword arguments", "", ""),
            Fix("VY001", 1, "modernized version pragma", "", ""),
        ],
        diagnostics=[
            Diagnostic("VYD014", 30, "range(stop) has a runtime bound; add bound=... manually"),
            Diagnostic("VYD014", 40, "range(stop) has a runtime bound; add bound=... manually"),
        ],
        source_compile="passed",
        target_compile="passed",
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])

    text = render_text(report)

    assert "VY112 changed positional event log to keyword arguments (lines 10, 20)" in text
    assert "VY001 modernized version pragma (line 1)" in text
    assert "VYD014 range(stop) has a runtime bound; add bound=... manually (lines 30, 40)" in text
    assert "VY112:10" not in text


def test_write_human_report_uses_plain_text_for_non_tty_streams() -> None:
    report = RunReport(
        source_version=None,
        target_version="0.4.3",
        files=[FileReport(path=Path("ok.vy"), changed=True, source_compile="passed", target_compile="passed")],
    )
    stream = StringIO()

    write_human_report(report, stream)

    assert stream.getvalue() == render_text(report)


def test_render_rich_marks_success_warning_and_error_output() -> None:
    file_report = FileReport(
        path=Path("mixed.vy"),
        changed=True,
        diagnostics=[
            Diagnostic("VYD001", 1, "warning message", "warning"),
            Diagnostic("VYD002", 2, "error message", "error"),
        ],
        source_compile="passed",
        target_compile="failed",
        target_error="compiler failed",
        abi_equal=True,
        storage_layout_equal=False,
    )
    report = RunReport(source_version=None, target_version="0.4.3", files=[file_report])
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="standard",
        no_color=False,
        theme=THEME,
        width=120,
    )

    render_rich(report, console)

    text = stream.getvalue()
    assert "\x1b[" in text
    assert "source compile: \x1b[32mpassed" in text
    assert "target compile: \x1b[1;31mfailed" in text
    assert "VYD001 warning message (line 1)" in text
    assert "VYD002 error message (line 2)" in text
    assert "storage layout unchanged: \x1b[33mFalse" in text
