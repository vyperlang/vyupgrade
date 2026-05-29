from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

from .compiler import compare_artifacts, compile_source_file, compile_target_source
from .models import Config, Diagnostic, FileReport, RunReport
from .project import discover_files
from .reporting import render_text, write_json_report
from .rules import apply_rules
from .versions import MigrationContext, infer_pragma


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pyproject = _load_pyproject_config(Path(args.config) if args.config else Path("pyproject.toml"))
    paths = [Path(path) for path in args.paths] or [Path(path) for path in pyproject.get("paths", [])]
    if not paths:
        print("no paths supplied", file=sys.stderr)
        return 4
    config = Config(
        paths=tuple(paths),
        target_version=args.target_version or pyproject.get("target-version", "0.4.3"),
        source_version=args.source_version or _none_if_infer(pyproject.get("source-version")),
        write=args.write,
        check=args.check,
        diff=args.diff,
        report_json=Path(args.report_json or pyproject["report-json"]) if args.report_json or pyproject.get("report-json") else None,
        select=_split_rules(args.select),
        ignore=_split_rules(args.ignore),
        aggressive=args.aggressive or bool(pyproject.get("aggressive", False)),
        test_command=args.test_command,
        source_vyper=args.source_vyper,
        target_vyper=args.target_vyper,
        source_python=args.source_python or _string_or_none(pyproject.get("source-python")),
        target_python=args.target_python or _string_or_none(pyproject.get("target-python")),
        compiler_search_paths=tuple(Path(path) for path in (args.compiler_search_paths or pyproject.get("compiler-search-paths", []))),
        enable_decimals=args.enable_decimals,
        bump_pragma=args.bump_pragma,
        format=args.format or pyproject.get("format", "none"),
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
        source_version = config.source_version or infer_pragma(original)
        source_compile = compile_source_file(path, config, source_version)
        source_ast = source_compile.artifacts.get("ast") if source_compile.artifacts else None
        file_config = replace(config, source_ast=source_ast if isinstance(source_ast, dict) else None)
        rewrite = apply_rules(original, file_config, path)
        changed = original != rewrite.source
        file_report = FileReport(path=path, changed=changed, fixes=rewrite.fixes, diagnostics=rewrite.diagnostics)
        file_report.source_compile = source_compile.status
        file_report.source_error = source_compile.stderr if source_compile.status == "failed" else None
        any_source_failed = any_source_failed or source_compile.status == "failed"

        target_compile = compile_target_source(path, rewrite.source, config)
        file_report.target_compile = target_compile.status
        file_report.target_error = target_compile.stderr if target_compile.status == "failed" else None
        any_target_failed = any_target_failed or target_compile.status == "failed"

        abi_equal, method_ids_equal, storage_layout_equal = compare_artifacts(source_compile, target_compile)
        file_report.abi_equal = abi_equal
        file_report.method_ids_equal = method_ids_equal
        file_report.storage_layout_equal = storage_layout_equal
        _add_validation_diagnostics(file_report, source_version, config)

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
        if config.format == "mamushi" and write_back:
            _run_mamushi([path for path, _ in write_back], run_report)

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
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--target-version")
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
    parser.add_argument("--source-python")
    parser.add_argument("--target-python")
    parser.add_argument("--compiler-search-paths", nargs="*", default=[])
    parser.add_argument("--enable-decimals", action="store_true")
    parser.add_argument("--bump-pragma", action="store_true")
    parser.add_argument("--format", choices=["none", "mamushi"])
    parser.add_argument("--config", help="path to a pyproject.toml file")
    return parser


def _split_rules(raw: str) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _load_pyproject_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return {}
    config = tool.get("vyupgrade", {})
    return config if isinstance(config, dict) else {}


def _none_if_infer(value: object) -> str | None:
    if value in {None, "infer"}:
        return None
    return str(value)


def _string_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _add_validation_diagnostics(file_report: FileReport, source_version: str | None, config: Config) -> None:
    if file_report.source_compile == "failed":
        file_report.diagnostics.append(Diagnostic("VYD006", 1, "source compile failed under declared or inferred source compiler"))
    if file_report.abi_equal is False:
        file_report.diagnostics.append(Diagnostic("VYD007", 1, "ABI changed after migration"))
    if file_report.method_ids_equal is False:
        file_report.diagnostics.append(Diagnostic("VYD007", 1, "method identifiers changed after migration"))
    if file_report.storage_layout_equal is False:
        file_report.diagnostics.append(Diagnostic("VYD008", 1, "storage layout changed after migration"))
    context = MigrationContext.from_specs(source_version, config.target_version)
    if context.crosses("0.4.0"):
        file_report.diagnostics.append(Diagnostic("VYD009", 1, "target compiler default EVM version differs from source-era default; review or pin explicitly"))


def _run_mamushi(paths: list[Path], report: RunReport) -> None:
    proc = subprocess.run(["mamushi", *map(str, paths)], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        report.test_status = "failed"
        report.test_output = (proc.stdout + proc.stderr).strip()


if __name__ == "__main__":
    raise SystemExit(main())
