from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
from pathlib import Path

from .compiler import compare_artifacts, compile_source_file, compile_target_source
from .models import Config, FileReport, RunReport
from .project import discover_files
from .reporting import render_text, write_json_report
from .rules import apply_rules
from .versions import infer_pragma


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = Config(
        paths=tuple(Path(path) for path in args.paths),
        target_version=args.target_version,
        source_version=args.source_version,
        write=args.write,
        check=args.check,
        diff=args.diff,
        report_json=Path(args.report_json) if args.report_json else None,
        select=_split_rules(args.select),
        ignore=_split_rules(args.ignore),
        aggressive=args.aggressive,
        test_command=args.test_command,
        source_vyper=args.source_vyper,
        target_vyper=args.target_vyper,
        compiler_search_paths=tuple(Path(path) for path in args.compiler_search_paths),
        enable_decimals=args.enable_decimals,
    )

    if config.write and config.check:
        print("--write and --check are mutually exclusive", file=sys.stderr)
        return 4

    files = discover_files(config.paths)
    reports: list[FileReport] = []
    diff_chunks: list[str] = []
    write_back: list[tuple[Path, str]] = []
    any_target_failed = False
    any_source_failed = False

    for path in files:
        original = path.read_text(encoding="utf-8")
        rewrite = apply_rules(original, config)
        changed = original != rewrite.source
        file_report = FileReport(path=path, changed=changed, fixes=rewrite.fixes, diagnostics=rewrite.diagnostics)

        source_version = config.source_version or infer_pragma(original)
        source_compile = compile_source_file(path, config, source_version)
        file_report.source_compile = source_compile.status
        file_report.source_error = source_compile.stderr
        any_source_failed = any_source_failed or source_compile.status == "failed"

        target_compile = compile_target_source(path, rewrite.source, config)
        file_report.target_compile = target_compile.status
        file_report.target_error = target_compile.stderr
        any_target_failed = any_target_failed or target_compile.status == "failed"

        abi_equal, method_ids_equal, storage_layout_equal = compare_artifacts(source_compile, target_compile)
        file_report.abi_equal = abi_equal
        file_report.method_ids_equal = method_ids_equal
        file_report.storage_layout_equal = storage_layout_equal

        if changed:
            write_back.append((path, rewrite.source))
            diff_chunks.extend(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    rewrite.source.splitlines(keepends=True),
                    fromfile=str(path),
                    tofile=str(path),
                )
            )
        reports.append(file_report)

    run_report = RunReport(
        source_version=config.source_version,
        target_version=config.target_version,
        files=reports,
        test_command=config.test_command,
    )

    if config.diff and diff_chunks:
        sys.stdout.write("".join(diff_chunks))
        if not diff_chunks[-1].endswith("\n"):
            sys.stdout.write("\n")

    if config.write and not any_target_failed:
        for path, content in write_back:
            path.write_text(content, encoding="utf-8")

    if config.test_command and config.write and not any_target_failed:
        proc = subprocess.run(config.test_command, shell=True, capture_output=True, text=True, timeout=600)
        run_report.test_status = "passed" if proc.returncode == 0 else "failed"
        run_report.test_output = (proc.stdout + proc.stderr).strip()

    if config.report_json:
        write_json_report(config.report_json, run_report)

    if not config.diff:
        sys.stdout.write(render_text(run_report))

    if any_target_failed:
        return 2
    if any_source_failed:
        return 3
    if config.check and any(file.changed for file in reports):
        return 1
    if any(diag.severity == "error" for file in reports for diag in file.diagnostics):
        return 5
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vyupgrade")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--target-version", default="0.4.3")
    parser.add_argument("--source-version")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--report-json")
    parser.add_argument("--select", default="")
    parser.add_argument("--ignore", default="")
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--test-command")
    parser.add_argument("--source-vyper")
    parser.add_argument("--target-vyper")
    parser.add_argument("--compiler-search-paths", nargs="*", default=[])
    parser.add_argument("--enable-decimals", action="store_true")
    parser.add_argument("--config", help="reserved for pyproject.toml support")
    return parser


def _split_rules(raw: str) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


if __name__ == "__main__":
    raise SystemExit(main())
