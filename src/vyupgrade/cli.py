from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from .compiler import (
    CompileResult,
    compare_artifact_details,
    compare_artifacts,
    compile_source_file,
    compile_target_source,
    target_overlay,
)
from .interfaces import GeneratedInterface, split_interfaces_to_vyi
from .models import Config, Diagnostic, FileReport, RunReport
from .project import discover_files
from .reporting import HumanReporter, write_json_report
from .rules import RewriteResult, apply_rules
from .versions import (
    MigrationContext,
    compiler_version_for_spec,
    default_evm_version_for_spec,
    infer_pragma,
)


@dataclass
class RewriteWork:
    path: Path
    original: str
    rewrite: RewriteResult
    report: FileReport
    source_compile: CompileResult
    source_version: str | None
    generated_interfaces: tuple[GeneratedInterface, ...]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pyproject = _load_pyproject_config(Path(args.config) if args.config else Path("pyproject.toml"))
    paths = [Path(path) for path in args.paths] or [
        Path(path) for path in pyproject.get("paths", [])
    ]
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
        report_json=Path(args.report_json or pyproject["report-json"])
        if args.report_json or pyproject.get("report-json")
        else None,
        select=_split_rules(args.select),
        ignore=_split_rules(args.ignore),
        aggressive=args.aggressive or bool(pyproject.get("aggressive", False)),
        test_command=args.test_command,
        source_vyper=args.source_vyper,
        target_vyper=args.target_vyper,
        source_python=args.source_python or _string_or_none(pyproject.get("source-python")),
        target_python=args.target_python or _string_or_none(pyproject.get("target-python")),
        compiler_search_paths=tuple(
            Path(path)
            for path in (args.compiler_search_paths or pyproject.get("compiler-search-paths", []))
        ),
        enable_decimals=args.enable_decimals,
        split_interfaces=args.split_interfaces or bool(pyproject.get("split-interfaces", False)),
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
    human_reporter = None if config.diff else HumanReporter(sys.stdout)
    if human_reporter:
        human_reporter.start(config.source_version, config.target_version)

    rewrites = _prepare_rewrites(files, config)
    any_source_failed = any(work.source_compile.status == "failed" for work in rewrites)

    target_sources = {work.path: work.rewrite.source for work in rewrites}
    for work in rewrites:
        target_sources.update(
            {interface.path: interface.source for interface in work.generated_interfaces}
        )
    with target_overlay(target_sources, config.target_version) as overlay:
        for work in rewrites:
            target_compile = compile_target_source(work.path, work.rewrite.source, config, overlay)
            work.report.target_compile = target_compile.status
            work.report.target_error = (
                target_compile.stderr if target_compile.status == "failed" else None
            )
            any_target_failed = any_target_failed or target_compile.status == "failed"

            abi_equal, method_ids_equal, storage_layout_equal = compare_artifacts(
                work.source_compile, target_compile
            )
            work.report.abi_equal = abi_equal
            work.report.method_ids_equal = method_ids_equal
            work.report.storage_layout_equal = storage_layout_equal
            abi_diff, method_id_diff, storage_layout_diff = compare_artifact_details(
                work.source_compile,
                target_compile,
            )
            work.report.abi_diff = abi_diff
            work.report.method_id_diff = method_id_diff
            work.report.storage_layout_diff = storage_layout_diff
            _add_validation_diagnostics(work.report, work.source_version, config)

            if work.report.changed:
                _record_change(
                    work.path, work.original, work.rewrite.source, write_back, diff_chunks
                )
            reports.append(work.report)
            if human_reporter:
                human_reporter.file(work.report)
            for interface in work.generated_interfaces:
                previous = (
                    interface.path.read_text(encoding="utf-8") if interface.path.exists() else ""
                )
                if not _record_change(
                    interface.path, previous, interface.source, write_back, diff_chunks
                ):
                    continue
                generated_report = FileReport(
                    path=interface.path, changed=True, fixes=[interface.fix]
                )
                reports.append(generated_report)
                if human_reporter:
                    human_reporter.file(generated_report)

    run_report = RunReport(
        source_version=config.source_version,
        target_version=config.target_version,
        files=reports,
        write_requested=config.write,
        wrote_changes=config.write and not any_target_failed,
        test_command=config.test_command,
    )

    if config.diff and diff_chunks:
        _write_diff(diff_chunks, sys.stdout)

    if config.write and not any_target_failed:
        for path, content in write_back:
            path.write_text(content, encoding="utf-8")
        if config.format == "mamushi" and write_back:
            _run_mamushi([path for path, _ in write_back], run_report)

    if config.test_command and config.write and not any_target_failed:
        proc = subprocess.run(
            config.test_command, shell=True, capture_output=True, text=True, timeout=600
        )
        run_report.test_status = "passed" if proc.returncode == 0 else "failed"
        run_report.test_output = (proc.stdout + proc.stderr).strip()

    if config.report_json:
        write_json_report(config.report_json, run_report)

    if human_reporter:
        human_reporter.summary(run_report)

    if any_target_failed:
        return 2
    if any_source_failed:
        return 3
    if config.check and any(file.changed for file in reports):
        return 1
    if any(diag.severity == "error" for file in reports for diag in file.diagnostics):
        return 5
    return 0


def _prepare_rewrites(files: list[Path], config: Config) -> list[RewriteWork]:
    rewrites: list[RewriteWork] = []
    for path in files:
        original = path.read_text(encoding="utf-8")
        source_version = config.source_version or infer_pragma(original)
        source_compile = compile_source_file(path, config, source_version)
        source_ast = source_compile.artifacts.get("ast") if source_compile.artifacts else None
        file_config = replace(
            config, source_ast=source_ast if isinstance(source_ast, dict) else None
        )
        rewrite = apply_rules(original, file_config, path)
        generated_interfaces: tuple[GeneratedInterface, ...] = ()
        if config.split_interfaces and path.suffix == ".vy":
            split = split_interfaces_to_vyi(rewrite.source, path)
            rewrite.source = split.source
            rewrite.fixes.extend(split.fixes)
            generated_interfaces = split.generated
        changed = original != rewrite.source
        file_report = FileReport(
            path=path, changed=changed, fixes=rewrite.fixes, diagnostics=rewrite.diagnostics
        )
        file_report.source_compile = source_compile.status
        file_report.source_error = (
            source_compile.stderr if source_compile.status == "failed" else None
        )
        rewrites.append(
            RewriteWork(
                path,
                original,
                rewrite,
                file_report,
                source_compile,
                source_version,
                generated_interfaces,
            )
        )
    return rewrites


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
    parser.add_argument("--split-interfaces", "--interfaces-to-vyi", action="store_true")
    parser.add_argument("--format", choices=["none", "mamushi"])
    parser.add_argument("--config", help="path to a pyproject.toml file")
    return parser


def _record_change(
    path: Path,
    previous: str,
    current: str,
    write_back: list[tuple[Path, str]],
    diff_chunks: list[str],
) -> bool:
    if previous == current:
        return False
    write_back.append((path, current))
    diff_chunks.extend(
        difflib.unified_diff(
            previous.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    return True


def _write_diff(diff_chunks: list[str], stream: TextIO) -> None:
    if _should_color(stream):
        text = "".join(_colorize_diff_line(line) for line in diff_chunks)
    else:
        text = "".join(diff_chunks)
    stream.write(text)
    if not diff_chunks[-1].endswith("\n"):
        stream.write("\n")


def _should_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") is not None or os.environ.get("CLICOLOR") == "0":
        return False
    return stream.isatty()


def _colorize_diff_line(line: str) -> str:
    if line.startswith(("--- ", "+++ ")):
        return _ansi(line, "1")
    if line.startswith("@@"):
        return _ansi(line, "36")
    if line.startswith("+"):
        return _ansi(line, "32")
    if line.startswith("-"):
        return _ansi(line, "31")
    if line.startswith("\\"):
        return _ansi(line, "2")
    return line


def _ansi(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


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


def _add_validation_diagnostics(
    file_report: FileReport, source_version: str | None, config: Config
) -> None:
    if file_report.source_compile == "failed":
        file_report.diagnostics.append(
            Diagnostic(
                "VYD006", 1, "source compile failed under declared or inferred source compiler"
            )
        )
    if file_report.abi_equal is False:
        file_report.diagnostics.append(Diagnostic("VYD007", 1, "ABI changed after migration"))
    if file_report.method_ids_equal is False:
        file_report.diagnostics.append(
            Diagnostic("VYD007", 1, "method identifiers changed after migration")
        )
    if file_report.storage_layout_equal is False:
        file_report.diagnostics.append(
            Diagnostic("VYD008", 1, "storage layout changed after migration")
        )
    evm_diagnostic = _evm_default_diagnostic(source_version, config.target_version)
    if evm_diagnostic is not None:
        file_report.diagnostics.append(evm_diagnostic)


def _evm_default_diagnostic(source_version: str | None, target_version: str) -> Diagnostic | None:
    source_evm = default_evm_version_for_spec(source_version)
    target_evm = default_evm_version_for_spec(target_version)
    if source_evm is not None and target_evm is not None:
        if source_evm == target_evm:
            return None
        source_compiler = compiler_version_for_spec(source_version) or "unknown"
        target_compiler = compiler_version_for_spec(target_version) or target_version
        return Diagnostic(
            "VYD009",
            1,
            f"default EVM version changed from {source_evm} (source compiler {source_compiler}) to {target_evm} (target compiler {target_compiler}); review or pin explicitly",
        )
    context = MigrationContext.from_specs(source_version, target_version)
    if not context.crosses("0.4.0"):
        return None
    if target_evm is not None:
        message = f"target compiler defaults to EVM {target_evm}; source-era default is unknown; review or pin explicitly"
    else:
        message = "target compiler default EVM version differs from source-era default; review or pin explicitly"
    return Diagnostic("VYD009", 1, message)


def _run_mamushi(paths: list[Path], report: RunReport) -> None:
    proc = subprocess.run(
        ["mamushi", *map(str, paths)], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        report.test_status = "failed"
        report.test_output = (proc.stdout + proc.stderr).strip()


if __name__ == "__main__":
    raise SystemExit(main())
