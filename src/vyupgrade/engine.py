from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

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
    ValidationDecision,
)
from .rule_registry import is_enabled
from .rules import RULE_CHANGES, apply_rules
from .validation import decide_run_validation
from .versions import (
    MigrationContext,
    compiler_version_for_source_validation,
    compiler_version_for_spec,
    default_evm_version_for_spec,
    infer_pragma,
)


CandidateSource = Callable[[Path, str], str]


class CandidatePathConflictError(ValueError):
    """Raised when multiple migration candidates resolve to one destination."""


@dataclass(frozen=True)
class SourceCompileAttempt:
    """One compiler attempt and the source version rules should use if it wins."""

    compile_version: str | None
    rule_version: str | None
    compiler_label: str | None = None


@dataclass(frozen=True)
class MigrationRequest:
    path: Path
    original: str
    source_version: str | None
    source_attempts: tuple[SourceCompileAttempt, ...]
    skip_target_on_blocked_source: bool = True
    role: str = "project"


@dataclass
class MigrationFile:
    request: MigrationRequest
    rewrite: RewriteResult
    report: FileReport
    source_compile: CompileResult
    source_version: str | None
    source_compiler: str | None
    target_compile: CompileResult | None = None
    validation_diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.request.path

    @property
    def original(self) -> str:
        return self.request.original


@dataclass
class GeneratedMigration:
    file: GeneratedFile
    report: FileReport
    target_compile: CompileResult | None = None
    validation_diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass
class MigrationBatch:
    files: list[MigrationFile]
    generated: list[GeneratedMigration]

    @property
    def reports(self) -> list[FileReport]:
        return [
            *(migration.report for migration in self.files),
            *(migration.report for migration in self.generated),
        ]


def bounded_migration_request(
    path: Path, original: str, config: Config, *, role: str = "project"
) -> MigrationRequest:
    """Build the target-bounded source compiler request used by the CLI."""
    source_version = config.source_version or infer_pragma(original)
    context = MigrationContext.from_specs(source_version, config.target_version)
    if not context.source_can_migrate_to_target():
        attempts: tuple[SourceCompileAttempt, ...] = ()
    elif config.source_vyper:
        attempts = (SourceCompileAttempt(source_version, source_version),)
    else:
        compiler = compiler_version_for_source_validation(
            source_version, config.target_version, original
        )
        attempts = (SourceCompileAttempt(compiler, source_version, compiler),)
    return MigrationRequest(path, original, source_version, attempts, role=role)


def prepare_migrations(
    requests: Iterable[MigrationRequest], config: Config
) -> MigrationBatch:
    """Compile and rewrite sources without mutating their destinations."""
    files: list[MigrationFile] = []
    for request in requests:
        attempt, source_compile = _compile_source(request, config)
        source_version = (
            attempt.rule_version if attempt is not None else request.source_version
        )
        source_compiler = attempt.compiler_label if attempt is not None else None
        source_ast = (
            source_compile.artifacts.get("ast") if source_compile.artifacts else None
        )
        # Config.source_ast remains the compatibility bridge for rules, but this
        # derived config belongs to this file and is never reused by another file.
        file_config = replace(
            config,
            source_version=source_version,
            source_ast=source_ast if isinstance(source_ast, dict) else None,
        )
        rewrite = apply_rules(request.original, file_config, request.path)
        if (
            config.split_interfaces
            and request.path.suffix == ".vy"
            and request.role == "project"
            and MigrationContext.from_specs(
                source_version, config.target_version
            ).source_can_migrate_to_target()
            and _rule_enabled("VY120", source_version, config)
        ):
            split = split_interfaces_to_vyi(rewrite.source, request.path)
            rewrite.source = split.source
            rewrite.fixes.extend(split.fixes)
            rewrite.generated_files.extend(split.generated)

        report = FileReport(
            path=request.path,
            role=request.role,
            changed=request.original != rewrite.source,
            fixes=rewrite.fixes,
            # Validation diagnostics belong to the report, not the rule result.
            # Keeping these lists independent preserves rule-only consumers such
            # as the corpus result's `diagnostics` field.
            diagnostics=list(rewrite.diagnostics),
            source_version=source_version,
            source_compiler=source_compiler,
            source_compile=source_compile.status,
            source_unavailable_formats=list(
                getattr(source_compile, "unavailable_formats", ())
            ),
            source_error=(
                source_compile.stderr if source_compile.status == "failed" else None
            ),
        )
        if source_compile.status != "skipped":
            report.source_unavailable_artifacts = unavailable_validation_artifacts(
                source_compile
            )
        files.append(
            MigrationFile(
                request,
                rewrite,
                report,
                source_compile,
                source_version,
                source_compiler,
            )
        )

    generated = [
        GeneratedMigration(
            generated_file,
            FileReport(
                path=generated_file.path,
                changed=True,
                fixes=[generated_file.fix],
            ),
        )
        for migration in files
        for generated_file in getattr(migration.rewrite, "generated_files", ())
    ]
    return MigrationBatch(files, generated)


def validate_migrations(
    batch: MigrationBatch,
    config: Config,
    candidate_source: CandidateSource | None = None,
) -> ValidationDecision:
    """Validate one coherent candidate overlay and return its typed decision."""
    resolve_candidate = candidate_source or _unchanged_candidate
    target_sources = candidate_sources(batch, resolve_candidate)

    with target_overlay(
        target_sources,
        config.target_version,
        config.compiler_search_paths,
        include_dependencies=config.include_dependencies,
    ) as overlay:
        for migration in batch.files:
            _reset_target_validation(
                migration.report, migration.validation_diagnostics
            )
            migration.target_compile = None
            source_context = MigrationContext.from_specs(
                migration.request.source_version, config.target_version
            )
            if (
                migration.request.skip_target_on_blocked_source
                and not source_context.source_can_migrate_to_target()
            ):
                continue
            target_compile = compile_target_source(
                migration.path,
                target_sources[migration.path],
                config,
                overlay,
            )
            migration.target_compile = target_compile
            _record_target_compile(migration.report, target_compile)
            (
                migration.report.abi_equal,
                migration.report.method_ids_equal,
                migration.report.storage_layout_equal,
            ) = compare_artifacts(migration.source_compile, target_compile)
            (
                migration.report.abi_diff,
                migration.report.method_id_diff,
                migration.report.storage_layout_diff,
            ) = compare_artifact_details(migration.source_compile, target_compile)
            migration.validation_diagnostics.extend(
                _add_validation_diagnostics(
                    migration.report,
                    migration.source_version,
                    config,
                    migration.source_compiler,
                )
            )

        for migration in batch.generated:
            _reset_target_validation(
                migration.report, migration.validation_diagnostics
            )
            migration.target_compile = None
            target_compile = compile_target_source(
                migration.file.path,
                target_sources[migration.file.path],
                config,
                overlay,
            )
            migration.target_compile = target_compile
            _record_target_compile(migration.report, target_compile)

    return decide_run_validation(batch.reports, config)


def _compile_source(
    request: MigrationRequest, config: Config
) -> tuple[SourceCompileAttempt | None, CompileResult]:
    if not request.source_attempts:
        return None, CompileResult("skipped")

    first_attempt: SourceCompileAttempt | None = None
    first_result: CompileResult | None = None
    for attempt in request.source_attempts:
        attempt_config = replace(
            config, source_version=attempt.rule_version, source_ast=None
        )
        result = compile_source_file(
            request.path, attempt_config, attempt.compile_version
        )
        if first_result is None:
            first_attempt = attempt
            first_result = result
        if result.status == "passed":
            return attempt, result
    assert first_attempt is not None and first_result is not None
    return first_attempt, first_result


def _unchanged_candidate(_path: Path, source: str) -> str:
    return source


def candidate_sources(
    batch: MigrationBatch, resolve_candidate: CandidateSource
) -> dict[Path, str]:
    candidates = [
        *(
            (
                migration.path,
                migration.rewrite.source,
                False,
                migration.original,
            )
            for migration in batch.files
        ),
        *(
            (migration.file.path, migration.file.source, True, None)
            for migration in batch.generated
        ),
    ]
    destinations: dict[Path, tuple[Path, str, bool, bool]] = {}
    target_sources: dict[Path, str] = {}
    for path, fallback, generated, original in candidates:
        candidate = resolve_candidate(path, fallback)
        destination = path.resolve()
        previous = destinations.get(destination)
        if previous is not None:
            (
                previous_path,
                previous_candidate,
                generated_seen,
                source_aliasable,
            ) = previous
            # MigrationPlan permits an existing, unchanged discovered source to
            # satisfy one identical generated output as a no-op. Preserve that
            # alias while still rejecting duplicate sources, differing bytes,
            # and more than one generator for a destination.
            if (
                generated
                and source_aliasable
                and not generated_seen
                and candidate == previous_candidate
            ):
                destinations[destination] = (
                    previous_path,
                    previous_candidate,
                    True,
                    True,
                )
                target_sources[path] = candidate
                continue
            raise CandidatePathConflictError(
                f"migration candidates {previous_path} and {path} resolve to the same "
                f"destination {destination}"
            )
        destinations[destination] = (
            path,
            candidate,
            generated,
            not generated and candidate == original,
        )
        target_sources[path] = candidate
    return target_sources


def _record_target_compile(report: FileReport, target_compile: CompileResult) -> None:
    report.target_compile = target_compile.status
    report.target_unavailable_artifacts = unavailable_validation_artifacts(target_compile)
    report.target_unavailable_formats = list(
        getattr(target_compile, "unavailable_formats", ())
    )
    report.target_error = (
        target_compile.stderr if target_compile.status == "failed" else None
    )


def _reset_target_validation(
    report: FileReport, validation_diagnostics: list[Diagnostic]
) -> None:
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
    validation_ids = {id(diagnostic) for diagnostic in validation_diagnostics}
    if validation_ids:
        report.diagnostics = [
            diagnostic
            for diagnostic in report.diagnostics
            if id(diagnostic) not in validation_ids
        ]
    validation_diagnostics.clear()


def _add_validation_diagnostics(
    file_report: FileReport,
    source_version: str | None,
    config: Config,
    source_compiler: str | None = None,
) -> list[Diagnostic]:
    added: list[Diagnostic] = []

    def add(diagnostic: Diagnostic) -> None:
        if _rule_enabled(diagnostic.rule, source_version, config):
            file_report.diagnostics.append(diagnostic)
            added.append(diagnostic)

    if file_report.source_compile == "failed":
        add(
            Diagnostic(
                "VYD006",
                1,
                "source compile failed under declared or inferred source compiler",
            )
        )
    if file_report.abi_equal is False:
        add(Diagnostic("VYD007", 1, "ABI changed after migration"))
    if file_report.method_ids_equal is False:
        add(Diagnostic("VYD007", 1, "method identifiers changed after migration"))
    if file_report.storage_layout_equal is False:
        add(Diagnostic("VYD008", 1, "storage layout changed after migration"))
    evm_diagnostic = _evm_default_diagnostic(
        source_compiler or source_version, config.target_version
    )
    if evm_diagnostic is not None:
        add(evm_diagnostic)
    return added


def _rule_enabled(rule: str, source_version: str | None, config: Config) -> bool:
    context = MigrationContext.from_specs(source_version, config.target_version)
    return is_enabled(rule, config, context, RULE_CHANGES)


def _evm_default_diagnostic(
    source_version: str | None, target_version: str
) -> Diagnostic | None:
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
        message = (
            f"target compiler defaults to EVM {target_evm}; "
            "source-era default is unknown; review or pin explicitly"
        )
    else:
        message = (
            "target compiler default EVM version differs from source-era default; "
            "review or pin explicitly"
        )
    return Diagnostic("VYD009", 1, message)
