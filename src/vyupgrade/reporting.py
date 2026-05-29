from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

from .models import FileReport, RunReport


THEME = Theme(
    {
        "vy.header": "bold cyan",
        "vy.path": "bold",
        "vy.muted": "dim",
        "vy.success": "green",
        "vy.warning": "yellow",
        "vy.error": "bold red",
        "vy.fix": "green",
        "vy.info": "cyan",
    }
)


@dataclass(frozen=True)
class ReportGroup:
    rule: str
    message: str
    lines: tuple[int, ...]
    style: str


def render_text(report: RunReport) -> str:
    lines = [
        "vyupgrade 0.1.0",
        f"source: {report.source_version or 'inferred per file'}",
        f"target: {report.target_version}",
        "",
        f"changed {report.changed_count} files",
        f"applied {report.fix_count} fixes",
        f"left {report.diagnostic_count} review diagnostics",
    ]

    for file in report.files:
        if not file.changed and not file.diagnostics and file.target_compile == "skipped":
            continue
        lines.append("")
        lines.append(str(file.path))
        for group in _group_items(file.fixes, lambda _fix: ""):
            lines.append(f"  {_group_text(group)}")
        for group in _group_items(file.diagnostics, lambda diag: _severity_style(diag.severity)):
            lines.append(f"  {_group_text(group)}")
        lines.append(f"  source compile: {file.source_compile}")
        if file.source_compile == "failed" and file.source_error:
            lines.append("  source error:")
            lines.extend(f"    {line}" for line in file.source_error.splitlines())
        lines.append(f"  target compile: {file.target_compile}")
        if file.target_compile == "failed" and file.target_error:
            lines.append("  target error:")
            lines.extend(f"    {line}" for line in file.target_error.splitlines())
        if file.abi_equal is not None:
            lines.append(f"  ABI unchanged: {file.abi_equal}")
        if file.method_ids_equal is not None:
            lines.append(f"  method IDs unchanged: {file.method_ids_equal}")
        if file.storage_layout_equal is not None:
            lines.append(f"  storage layout unchanged: {file.storage_layout_equal}")

    if report.test_command:
        lines.extend(["", f"test command: {report.test_status}"])
        if report.test_output:
            lines.append(report.test_output.rstrip())

    return "\n".join(lines) + "\n"


def write_human_report(report: RunReport, stream: TextIO) -> None:
    if stream.isatty():
        render_rich(report, Console(file=stream, theme=THEME))
        return
    stream.write(render_text(report))


def render_rich(report: RunReport, console: Console) -> None:
    console.print(Text("vyupgrade 0.1.0", style="vy.header"))
    console.print(_label_value("source", report.source_version or "inferred per file"))
    console.print(_label_value("target", report.target_version))
    console.print()
    console.print(_summary_line("changed", report.changed_count, "files", _count_style(report.changed_count)))
    console.print(_summary_line("applied", report.fix_count, "fixes", _count_style(report.fix_count)))
    console.print(_summary_line("left", report.diagnostic_count, "review diagnostics", _count_style(report.diagnostic_count)))

    for file in report.files:
        if not file.changed and not file.diagnostics and file.target_compile == "skipped":
            continue
        _render_file(console, file)

    if report.test_command:
        console.print()
        console.print(_label_value("test command", report.test_status, _status_style(report.test_status)))
        if report.test_output:
            console.print(report.test_output.rstrip())


def _render_file(console: Console, file: FileReport) -> None:
    console.print()
    console.print(Text(str(file.path), style="vy.path"))
    for group in _group_items(file.fixes, lambda _fix: "vy.fix"):
        console.print(_indented(_group_text(group), group.style))
    for group in _group_items(file.diagnostics, lambda diag: _severity_style(diag.severity)):
        console.print(_indented(_group_text(group), group.style))
    console.print(_compile_line("source compile", file.source_compile))
    if file.source_compile == "failed" and file.source_error:
        console.print(_indented("source error:", "vy.error"))
        for line in file.source_error.splitlines():
            console.print(_indented(f"  {line}", "vy.error"))
    console.print(_compile_line("target compile", file.target_compile))
    if file.target_compile == "failed" and file.target_error:
        console.print(_indented("target error:", "vy.error"))
        for line in file.target_error.splitlines():
            console.print(_indented(f"  {line}", "vy.error"))
    if file.abi_equal is not None:
        console.print(_bool_line("ABI unchanged", file.abi_equal))
    if file.method_ids_equal is not None:
        console.print(_bool_line("method IDs unchanged", file.method_ids_equal))
    if file.storage_layout_equal is not None:
        console.print(_bool_line("storage layout unchanged", file.storage_layout_equal))


def _label_value(label: str, value: object, value_style: str = "") -> Text:
    text = Text(f"{label}: ", style="vy.muted")
    text.append(str(value), style=value_style)
    return text


def _summary_line(verb: str, count: int, label: str, count_style: str) -> Text:
    text = Text(f"{verb} ")
    text.append(str(count), style=count_style)
    text.append(f" {label}")
    return text


def _compile_line(label: str, status: str) -> Text:
    text = Text(f"  {label}: ")
    text.append(status, style=_status_style(status))
    return text


def _bool_line(label: str, value: bool) -> Text:
    text = Text(f"  {label}: ")
    text.append(str(value), style="vy.success" if value else "vy.warning")
    return text


def _indented(value: str, style: str) -> Text:
    return Text(f"  {value}", style=style)


def _group_items(
    items: Iterable[object],
    style_for: Callable[[object], str],
) -> list[ReportGroup]:
    groups: dict[tuple[str, str, str], list[int]] = {}
    for item in items:
        rule = getattr(item, "rule")
        message = getattr(item, "message")
        style = style_for(item)
        groups.setdefault((rule, message, style), []).append(getattr(item, "line"))
    return [
        ReportGroup(rule, message, tuple(lines), style)
        for (rule, message, style), lines in groups.items()
    ]


def _group_text(group: ReportGroup) -> str:
    return f"{group.rule} {group.message} ({_line_list(group.lines)})"


def _line_list(lines: tuple[int, ...]) -> str:
    prefix = "line" if len(lines) == 1 else "lines"
    return f"{prefix} {', '.join(map(str, lines))}"


def _count_style(count: int) -> str:
    return "vy.warning" if count else "vy.success"


def _status_style(status: str) -> str:
    return {
        "passed": "vy.success",
        "failed": "vy.error",
        "skipped": "vy.muted",
    }.get(status, "vy.warning")


def _severity_style(severity: str) -> str:
    return {
        "error": "vy.error",
        "warning": "vy.warning",
        "info": "vy.info",
    }.get(severity, "vy.warning")


def write_json_report(path: Path, report: RunReport) -> None:
    path.write_text(json.dumps(report.to_json_obj(), indent=2, sort_keys=True) + "\n")
