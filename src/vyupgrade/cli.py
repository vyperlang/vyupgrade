from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
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
    unavailable_validation_artifacts,
)
from .interfaces import split_interfaces_to_vyi
from .models import (
    Config,
    Diagnostic,
    FileReport,
    GeneratedFile,
    RewriteResult,
    RunReport,
    ValidationDecision,
)
from .project import discover_files
from .reporting import HumanReporter, write_json_report
from .rule_registry import is_enabled
from .rules import RULE_CHANGES, apply_rules
from .validation import decide_run_validation, validation_exit_code
from .versions import (
    MigrationContext,
    compiler_version_for_source_validation,
    compiler_version_for_spec,
    default_evm_version_for_spec,
    infer_pragma,
)
from .write_plan import MigrationPlan, PlanConflictError, WriteTransactionError


@dataclass
class RewriteWork:
    path: Path
    original: str
    rewrite: RewriteResult
    report: FileReport
    source_compile: CompileResult
    source_version: str | None
    source_compiler: str | None


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
        allow_unvalidated_source=args.allow_unvalidated_source
        or bool(pyproject.get("allow-unvalidated-source", False)),
        allow_abi_change=args.allow_abi_change
        or bool(pyproject.get("allow-abi-change", False)),
        allow_method_id_change=args.allow_method_id_change
        or bool(pyproject.get("allow-method-id-change", False)),
        allow_storage_layout_change=args.allow_storage_layout_change
        or bool(pyproject.get("allow-storage-layout-change", False)),
    )

    if config.write and config.check:
        print("--write and --check are mutually exclusive", file=sys.stderr)
        return 4

    files = discover_files(config.paths)
    human_reporter = None if config.diff else HumanReporter(sys.stdout)
    if human_reporter:
        human_reporter.start(config.source_version, config.target_version)

    rewrites = _prepare_rewrites(files, config)
    reports, generated_reports, plan, plan_error = _build_migration_plan(rewrites)
    if plan_error is None:
        _verify_rewrites(rewrites, generated_reports, config, plan)
        validation_decision = decide_run_validation(reports, config)
    else:
        validation_decision = decide_run_validation((), config)

    run_report = RunReport(
        source_version=config.source_version,
        target_version=config.target_version,
        files=reports,
        write_requested=config.write,
        validation_decision=validation_decision,
        test_command=config.test_command,
    )
    if plan_error is not None:
        run_report.write_status = "failed"
        run_report.write_output = plan_error

    if config.write and plan_error is None and validation_decision.write_allowed:
        if config.format == "mamushi" and plan.writes:
            _run_mamushi(plan, run_report)
            if run_report.formatter_status == "passed":
                _verify_rewrites(rewrites, generated_reports, config, plan)
                validation_decision = decide_run_validation(reports, config)
                run_report.validation_decision = validation_decision
                if not validation_decision.write_allowed:
                    run_report.write_status = "blocked"
                    run_report.write_output = (
                        "formatted candidates did not pass final validation"
                    )
            else:
                run_report.write_status = "failed"
                run_report.write_output = "formatter failed before the write transaction"
        if (
            run_report.formatter_status != "failed"
            and validation_decision.write_allowed
        ):
            try:
                run_report.wrote_changes = plan.commit()
                run_report.write_status = (
                    "committed" if run_report.wrote_changes else "no-op"
                )
                run_report.wrote_changes, _mismatches = plan.refresh_final_state()
            except WriteTransactionError as exc:
                changed, mismatches = plan.refresh_final_state()
                run_report.wrote_changes = changed
                run_report.write_status = (
                    "rollback-incomplete" if exc.rollback_incomplete else "failed"
                )
                run_report.write_output = str(exc)
                if mismatches:
                    run_report.write_output += "; on-disk bytes differ from candidates: " + ", ".join(
                        str(path) for path in mismatches
                    )

    diff_chunks = plan.diff_chunks()
    if config.diff and diff_chunks:
        _write_diff(diff_chunks, sys.stdout)

    if (
        config.test_command
        and config.write
        and validation_decision.write_allowed
        and run_report.write_status in {"committed", "no-op"}
    ):
        _run_test_command(config.test_command, run_report)
        run_report.wrote_changes, mismatches = plan.refresh_final_state()
        if mismatches:
            drift = "test command changed planned destinations: " + ", ".join(
                str(path) for path in mismatches
            )
            run_report.test_status = "failed"
            run_report.test_output = "\n".join(
                part for part in (run_report.test_output, drift) if part
            )

    if config.report_json:
        write_json_report(config.report_json, run_report)

    if human_reporter:
        for report in reports:
            human_reporter.file(report)
        human_reporter.summary(run_report)

    if run_report.write_status in {"failed", "rollback-incomplete"}:
        if run_report.formatter_status == "failed":
            return 6
        return 9
    if (exit_code := validation_exit_code(validation_decision)) is not None:
        return exit_code
    if run_report.formatter_status == "failed":
        return 6
    if run_report.test_status == "failed":
        return 8
    if config.check and any(file.changed for file in reports):
        return 1
    if any(diag.severity == "error" for file in reports for diag in file.diagnostics):
        return 5
    return 0


def _prepare_rewrites(files: list[Path], config: Config) -> list[RewriteWork]:
    rewrites: list[RewriteWork] = []
    for path in files:
        original = path.read_bytes().decode("utf-8")
        source_version = config.source_version or infer_pragma(original)
        context = MigrationContext.from_specs(source_version, config.target_version)
        if context.source_newer_than_target():
            rewrite = apply_rules(original, config, path)
            file_report = FileReport(
                path=path,
                changed=False,
                fixes=rewrite.fixes,
                diagnostics=rewrite.diagnostics,
                source_version=source_version,
            )
            rewrites.append(
                RewriteWork(
                    path,
                    original,
                    rewrite,
                    file_report,
                    CompileResult("skipped"),
                    source_version,
                    None,
                )
            )
            continue
        inferred_source_compiler = compiler_version_for_source_validation(
            source_version, config.target_version, original
        )
        source_compile_version = source_version if config.source_vyper else inferred_source_compiler
        source_compiler = None if config.source_vyper else inferred_source_compiler
        source_compile = compile_source_file(path, config, source_compile_version)
        source_ast = source_compile.artifacts.get("ast") if source_compile.artifacts else None
        file_config = replace(
            config, source_ast=source_ast if isinstance(source_ast, dict) else None
        )
        rewrite = apply_rules(original, file_config, path)
        if (
            config.split_interfaces
            and path.suffix == ".vy"
            and _rule_enabled("VY120", source_version, config)
        ):
            split = split_interfaces_to_vyi(rewrite.source, path)
            rewrite.source = split.source
            rewrite.fixes.extend(split.fixes)
            rewrite.generated_files.extend(split.generated)
        changed = original != rewrite.source
        file_report = FileReport(
            path=path,
            changed=changed,
            fixes=rewrite.fixes,
            diagnostics=rewrite.diagnostics,
            source_version=source_version,
            source_compiler=source_compiler,
        )
        file_report.source_compile = source_compile.status
        if source_compile.status != "skipped":
            file_report.source_unavailable_artifacts = unavailable_validation_artifacts(
                source_compile
            )
        file_report.source_unavailable_formats = list(source_compile.unavailable_formats)
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
                source_compiler,
            )
        )
    return rewrites


def _verify_rewrites(
    rewrites: list[RewriteWork],
    generated_reports: list[tuple[GeneratedFile, FileReport]],
    config: Config,
    plan: MigrationPlan,
) -> None:
    target_sources = {
        work.path: plan.candidate_source(work.path, work.rewrite.source)
        for work in rewrites
    }
    for work in rewrites:
        target_sources.update(
            {
                generated_file.path: plan.candidate_source(
                    generated_file.path, generated_file.source
                )
                for generated_file in work.rewrite.generated_files
            }
        )
    for generated_file, report in generated_reports:
        target_sources[report.path] = plan.candidate_source(
            report.path, generated_file.source
        )
    with target_overlay(target_sources, config.target_version, config.compiler_search_paths) as overlay:
        for work in rewrites:
            _reset_target_validation(work.report)
            if any(diagnostic.rule == "VYD016" for diagnostic in work.report.diagnostics):
                continue
            target_source = target_sources[work.path]
            target_compile = compile_target_source(work.path, target_source, config, overlay)
            _record_target_compile(work.report, target_compile)
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
            _add_validation_diagnostics(
                work.report, work.source_version, config, work.source_compiler
            )
        for _generated_file, report in generated_reports:
            _reset_target_validation(report)
            target_compile = compile_target_source(
                report.path,
                target_sources[report.path],
                config,
                overlay,
            )
            _record_target_compile(report, target_compile)


def _record_target_compile(report: FileReport, target_compile: CompileResult) -> None:
    report.target_compile = target_compile.status
    report.target_unavailable_artifacts = unavailable_validation_artifacts(target_compile)
    report.target_unavailable_formats = list(target_compile.unavailable_formats)
    report.target_error = (
        target_compile.stderr if target_compile.status == "failed" else None
    )


def _reset_target_validation(report: FileReport) -> None:
    report.target_compile = "skipped"
    report.target_unavailable_artifacts.clear()
    report.target_unavailable_formats.clear()
    report.target_error = None
    report.abi_equal = None
    report.method_ids_equal = None
    report.storage_layout_equal = None
    report.abi_diff.clear()
    report.method_id_diff.clear()
    report.storage_layout_diff.clear()
    report.validation_decision = ValidationDecision()
    report.diagnostics = [
        diagnostic
        for diagnostic in report.diagnostics
        if diagnostic.rule not in {"VYD006", "VYD007", "VYD008", "VYD009"}
    ]


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
    parser.add_argument("--allow-unvalidated-source", action="store_true")
    parser.add_argument("--allow-abi-change", action="store_true")
    parser.add_argument("--allow-method-id-change", action="store_true")
    parser.add_argument("--allow-storage-layout-change", action="store_true")
    parser.add_argument("--config", help="path to a pyproject.toml file")
    return parser


def _build_migration_plan(
    rewrites: list[RewriteWork],
) -> tuple[
    list[FileReport],
    list[tuple[GeneratedFile, FileReport]],
    MigrationPlan,
    str | None,
]:
    reports = [work.report for work in rewrites]
    generated_reports: list[tuple[GeneratedFile, FileReport]] = []
    for work in rewrites:
        for generated_file in work.rewrite.generated_files:
            generated_report = FileReport(
                path=generated_file.path,
                changed=True,
                fixes=[generated_file.fix],
            )
            reports.append(generated_report)
            generated_reports.append((generated_file, generated_report))

    plan = MigrationPlan()
    try:
        for work in rewrites:
            plan.add_source(
                work.path,
                work.original,
                work.rewrite.source,
                work.report,
            )
        for generated_file, report in generated_reports:
            plan.add_generated(generated_file.path, generated_file.source, report)
    except PlanConflictError as exc:
        return reports, generated_reports, plan, str(exc)
    return reports, generated_reports, plan, None


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
    file_report: FileReport,
    source_version: str | None,
    config: Config,
    source_compiler: str | None = None,
) -> None:
    if file_report.source_compile == "failed":
        _add_diagnostic_if_enabled(
            file_report,
            Diagnostic(
                "VYD006", 1, "source compile failed under declared or inferred source compiler"
            ),
            source_version,
            config,
        )
    if file_report.abi_equal is False:
        _add_diagnostic_if_enabled(
            file_report,
            Diagnostic("VYD007", 1, "ABI changed after migration"),
            source_version,
            config,
        )
    if file_report.method_ids_equal is False:
        _add_diagnostic_if_enabled(
            file_report,
            Diagnostic("VYD007", 1, "method identifiers changed after migration"),
            source_version,
            config,
        )
    if file_report.storage_layout_equal is False:
        _add_diagnostic_if_enabled(
            file_report,
            Diagnostic("VYD008", 1, "storage layout changed after migration"),
            source_version,
            config,
        )
    evm_diagnostic = _evm_default_diagnostic(
        source_compiler or source_version, config.target_version
    )
    if evm_diagnostic is not None and _rule_enabled(evm_diagnostic.rule, source_version, config):
        file_report.diagnostics.append(evm_diagnostic)


def _add_diagnostic_if_enabled(
    file_report: FileReport,
    diagnostic: Diagnostic,
    source_version: str | None,
    config: Config,
) -> None:
    if _rule_enabled(diagnostic.rule, source_version, config):
        file_report.diagnostics.append(diagnostic)


def _rule_enabled(rule: str, source_version: str | None, config: Config) -> bool:
    context = MigrationContext.from_specs(source_version, config.target_version)
    return is_enabled(rule, config, context, RULE_CHANGES)


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


def _run_mamushi(plan: MigrationPlan, report: RunReport) -> None:
    with tempfile.TemporaryDirectory(prefix="vyupgrade-format-") as raw_directory:
        directory = Path(raw_directory)
        staged: dict[Path, Path] = {}
        try:
            common_parent = Path(
                os.path.commonpath([str(entry.path.parent) for entry in plan.writes])
            )
            for entry in plan.writes:
                staged_path = directory / entry.path.relative_to(common_parent)
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_bytes(entry.candidate)
                staged[entry.path] = staged_path
        except OSError as exc:
            report.formatter_status = "failed"
            report.formatter_output = f"could not stage formatter inputs: {exc}"
            return

        command = ["mamushi", *map(str, staged.values())]
        report.formatter_command = shlex.join(command)
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            report.formatter_status = "failed"
            report.formatter_output = "mamushi executable not found"
            return
        except subprocess.TimeoutExpired as exc:
            report.formatter_status = "failed"
            output = _command_output(exc.stdout, exc.stderr)
            report.formatter_output = "\n".join(
                part
                for part in (
                    f"mamushi timed out after {exc.timeout:g} seconds",
                    output,
                )
                if part
            )
            return
        except OSError as exc:
            report.formatter_status = "failed"
            report.formatter_output = f"mamushi failed to start: {exc}"
            return

        report.formatter_status = "passed" if proc.returncode == 0 else "failed"
        output = _command_output(proc.stdout, proc.stderr)
        if proc.returncode != 0:
            report.formatter_output = "\n".join(
                part
                for part in (
                    f"mamushi exited with status {proc.returncode}",
                    output,
                )
                if part
            )
            return

        try:
            formatted = {
                destination: staged_path.read_bytes()
                for destination, staged_path in staged.items()
            }
            for candidate in formatted.values():
                candidate.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.formatter_status = "failed"
            report.formatter_output = f"could not read formatted candidates: {exc}"
            return
        plan.update_candidates(formatted)
        report.formatter_output = output or None


def _run_test_command(command: str, report: RunReport) -> None:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        report.test_status = "failed"
        output = _command_output(exc.stdout, exc.stderr)
        report.test_output = "\n".join(
            part
            for part in (f"test command timed out after {exc.timeout:g} seconds", output)
            if part
        )
        return
    except OSError as exc:
        report.test_status = "failed"
        report.test_output = f"test command failed to start: {exc}"
        return

    report.test_status = "passed" if proc.returncode == 0 else "failed"
    output = _command_output(proc.stdout, proc.stderr)
    if proc.returncode == 0:
        report.test_output = output or None
    else:
        report.test_output = "\n".join(
            part
            for part in (f"test command exited with status {proc.returncode}", output)
            if part
        )


def _command_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    return "\n".join(
        text for text in (_decode_output(stdout), _decode_output(stderr)) if text
    ).strip()


def _decode_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


if __name__ == "__main__":
    raise SystemExit(main())
