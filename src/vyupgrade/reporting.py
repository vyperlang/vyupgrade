from __future__ import annotations

import json
from pathlib import Path

from .models import RunReport


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
        for fix in file.fixes:
            lines.append(f"  {fix.rule}:{fix.line} {fix.message}")
        for diag in file.diagnostics:
            lines.append(f"  {diag.rule}:{diag.line} {diag.message}")
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


def write_json_report(path: Path, report: RunReport) -> None:
    path.write_text(json.dumps(report.to_json_obj(), indent=2, sort_keys=True) + "\n")
