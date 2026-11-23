from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

from .closure import RetainedClosureInputs, stage_retained_closure_inputs
from .compiler import (
    CompileResult,
    ImportClosure,
    OverlayLayoutConflictError,
    TargetOverlay,
    _entry_closure,
    resolve_import_closure,
    compare_validation_artifacts,
    compile_source_ast,
    compile_source_file,
    compile_target_source,
    target_overlay,
    unavailable_validation_artifacts,
)
from .interfaces import split_interfaces_to_vyi
from .models import (
    CompileAttempt,
    CompilerDeclaration,
    CompilerFacts,
    ContentIdentity,
    DeclaredSpec,
    DependencyContext,
    Config,
    Diagnostic,
    FileReport,
    GeneratedFile,
    RewriteResult,
    ValidationDecision,
    TargetFailureOrigin,
    ValidationAttestation,
)
from .rule_groups.interface_wildcards import plan_interface_wildcards
from .rule_registry import is_enabled
from .rules import RULE_CHANGES, apply_rules
from .validation import decide_run_validation
from .versions import (
    MigrationContext,
    compiler_versions_for_source_validation,
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
    consumer_roots: tuple[Path, ...] = ()


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
    origin: Path
    target_compile: CompileResult | None = None
    validation_diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass
class MigrationBatch:
    files: list[MigrationFile]
    generated: list[GeneratedMigration]
    snapshot: ImportClosure | None = None
    retained_closure_inputs: RetainedClosureInputs | None = None

    @property
    def reports(self) -> list[FileReport]:
        return [
            *(migration.report for migration in self.files),
            *(migration.report for migration in self.generated),
        ]


def bounded_migration_request(
    path: Path,
    original: str,
    config: Config,
    *,
    role: str = "project",
    consumer_roots: tuple[Path, ...] = (),
) -> MigrationRequest:
    """Build the target-bounded source compiler request used by the CLI."""
    source_version = config.source_version or infer_pragma(original)
    context = MigrationContext.from_specs(source_version, config.target_version)
    if not context.source_can_migrate_to_target():
        attempts: tuple[SourceCompileAttempt, ...] = ()
    elif config.source_vyper:
        attempts = (SourceCompileAttempt(source_version, source_version),)
    else:
        attempts = tuple(
            SourceCompileAttempt(compiler, source_version, compiler)
            for compiler in compiler_versions_for_source_validation(
                source_version, config.target_version
            )
        )
    return MigrationRequest(
        path,
        original,
        source_version,
        attempts,
        role=role,
        consumer_roots=consumer_roots,
    )


def prepare_migrations(
    requests: Iterable[MigrationRequest], config: Config, *, snapshot: ImportClosure | None = None
) -> MigrationBatch:
    """Compile and rewrite sources without mutating their destinations."""
    request_list = tuple(requests)
    if request_list:
        snapshot = snapshot or resolve_import_closure(
            {request.path: request.original for request in request_list}, config.compiler_search_paths
        )
        config = replace(config, source_snapshot=snapshot)
    snapshot_sources = tuple(
        ContentIdentity(
            str(request.path.resolve()),
            hashlib.sha256(request.original.encode()).hexdigest(),
        )
        for request in request_list
    )
    if snapshot is not None:
        snapshot_sources = tuple(
            ContentIdentity(str(path), hashlib.sha256(content).hexdigest())
            for path, content in snapshot.contents.items()
        )
    compilations = []
    for request in request_list:
        attempt, source_compile = _compile_source(request, config)
        source_version = attempt.rule_version if attempt is not None else request.source_version
        compilations.append((request, attempt, source_version, source_compile))
    if snapshot is not None:
        compilations = _validate_library_modules_through_consumers(compilations, snapshot, config)
    wildcard_plans = {}
    if snapshot is not None and any(
        _rule_enabled(code, None, config) for code in ("VY125", "VYD022", "VYD023")
    ):
        wildcard_plans = plan_interface_wildcards(
            snapshot,
            [
                (request.path, request.original, source_version, source_compile)
                for request, _, source_version, source_compile in compilations
            ],
            config,
        )
    files: list[MigrationFile] = []
    for request, attempt, source_version, source_compile in compilations:
        contextual_validation = request.role == "dependency" or bool(request.consumer_roots)
        source_compiler = source_compile.resolved_compiler or (
            attempt.compiler_label if attempt is not None else None
        )
        file_config = replace(config, source_version=source_version, source_ast=None)
        facts = CompilerFacts.from_artifacts(
            source_compile.artifacts if source_compile.status in {"passed", "degraded"} else None
        )
        interface_wildcards = wildcard_plans.get(request.path.resolve())
        if interface_wildcards is None:
            rewrite = apply_rules(request.original, file_config, request.path, compiler_facts=facts)
        else:
            rewrite = apply_rules(
                request.original, file_config, request.path, compiler_facts=facts,
                interface_wildcards=interface_wildcards,
            )
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
            validation_mode="consumer-roots" if contextual_validation else "direct",
            consumer_roots=tuple(path.resolve() for path in request.consumer_roots),
            changed=request.original != rewrite.source,
            fixes=rewrite.fixes,
            # Validation diagnostics belong to the report, not the rule result.
            # Keeping these lists independent preserves rule-only consumers such
            # as the corpus result's `diagnostics` field.
            diagnostics=list(rewrite.diagnostics),
            source_version=source_version,
            source_compiler=None if contextual_validation else source_compiler,
            source_compile="skipped" if contextual_validation else source_compile.status,
            source_unavailable_formats=(
                []
                if contextual_validation
                else list(getattr(source_compile, "unavailable_formats", ()))
            ),
            source_error=(
                None
                if contextual_validation
                else source_compile.stderr
                if source_compile.status == "failed"
                else None
            ),
            source_error_type=(
                None
                if contextual_validation or source_compile.status != "failed"
                else source_compile.error_type
            ),
            source_attestation=_validation_attestation(
                source_compile,
                _declared_spec(snapshot_sources, source_compile.compiler_declarations),
                attempt_source=ContentIdentity(
                    str(request.path.resolve()),
                    hashlib.sha256(request.original.encode()).hexdigest(),
                ),
            )
            if not contextual_validation and source_compile.status != "skipped"
            else None,
        )
        if not contextual_validation and source_compile.status != "skipped":
            report.source_unavailable_artifacts = unavailable_validation_artifacts(source_compile)
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
            origin=migration.path,
        )
        for migration in files
        for generated_file in getattr(migration.rewrite, "generated_files", ())
    ]
    return MigrationBatch(files, generated, snapshot)


_Compilation = tuple[MigrationRequest, SourceCompileAttempt | None, str | None, CompileResult]


def _validate_library_modules_through_consumers(
    compilations: list[_Compilation], snapshot: ImportClosure, config: Config
) -> list[_Compilation]:
    """Validate a project module that only a consumer root can initialize through those roots.

    Vyper rejects a module with ``uses:`` as a compilation target, so its standalone compile fails
    with ``InitializerException``; it is valid source wherever an importer initializes it. Such a
    module takes its rewrite facts from the AST, which needs no compilation target, and its
    validation from the project roots in the batch that import it and compile directly. With no
    such root, it keeps the standalone failure.
    """

    def is_library_module(compilation: _Compilation) -> bool:
        request, _attempt, _version, source_compile = compilation
        return (
            request.role == "project"
            and request.path.suffix == ".vy"
            and source_compile.status == "failed"
            and source_compile.error_type == "InitializerException"
        )

    direct_roots = {
        request.path.resolve()
        for compilation in compilations
        if (request := compilation[0]).role == "project" and not is_library_module(compilation)
    }
    validated: list[_Compilation] = []
    for compilation in compilations:
        request, attempt, source_version, source_compile = compilation
        consumers = tuple(
            root for root in snapshot.consumers.get(request.path.resolve(), ()) if root in direct_roots
        )
        if attempt is None or not consumers or not is_library_module(compilation):
            validated.append(compilation)
            continue
        ast_config = replace(config, source_version=attempt.rule_version, source_ast=None)
        ast_compile = compile_source_ast(request.path, ast_config, attempt.compile_version)
        if ast_compile.status == "passed":
            request = replace(request, consumer_roots=consumers)
            source_compile = ast_compile
        validated.append((request, attempt, source_version, source_compile))
    return validated


def _target_scopes(
    batch: MigrationBatch,
    target_sources: dict[Path, str],
) -> Iterator[
    tuple[list[MigrationFile], list[GeneratedMigration], dict[Path, str], ImportClosure | None]
]:
    """Group direct validation and generated files by captured project scope."""
    snapshot = batch.snapshot
    if snapshot is None:
        yield batch.files, batch.generated, target_sources, None
        return

    projects: dict[Path, list[MigrationFile]] = {}
    # Generated files go with their origin's project; a module validated through its consumers
    # goes with its first consumer's, whose closure stages it.
    origin_projects: dict[Path, Path] = {}
    for migration in batch.files:
        if migration.report.validation_mode != "consumer-roots":
            root = snapshot.layout_root(migration.path)
            projects.setdefault(root, []).append(migration)
            origin_projects[migration.path] = root
        elif migration.request.role == "project":
            origin_projects[migration.path] = snapshot.layout_root(migration.request.consumer_roots[0])

    for root, files in projects.items():
        entry = snapshot.captured_path(files[0].path)
        assert entry is not None
        scope = _entry_closure(snapshot, entry)
        generated = [
            migration for migration in batch.generated if origin_projects.get(migration.origin) == root
        ]
        generated_paths = {
            migration.file.path for migration in batch.generated
            if scope.captured_path(migration.origin) is not None
        }
        sources = {
            path: source for path, source in target_sources.items()
            if path in generated_paths or scope.captured_path(path) is not None
        }
        yield files, generated, sources, scope


def validate_migrations(
    batch: MigrationBatch,
    config: Config,
    candidate_source: CandidateSource | None = None,
) -> ValidationDecision:
    """Validate each project's coherent candidate overlay and return its typed decision."""
    resolve_candidate = candidate_source or _unchanged_candidate
    batch.retained_closure_inputs = None
    target_sources = candidate_sources(batch, resolve_candidate)
    scopes = tuple(_target_scopes(batch, target_sources))
    if batch.snapshot is not None:
        config = replace(config, compiler_search_paths=batch.snapshot.search_roots)

    for migration in (*batch.files, *batch.generated):
        _reset_target_validation(migration.report, migration.validation_diagnostics)
        migration.target_compile = None

    has_dependencies = batch.snapshot is not None and bool(batch.snapshot.dependencies)
    with ExitStack() as overlays:
        shared_overlay = None
        if config.include_dependencies and target_sources and (
            len(scopes) > 1 or config.closure_output is not None
        ):
            # Closure output uses one import-root-relative layout for all projects.
            try:
                shared_overlay = overlays.enter_context(target_overlay(
                    target_sources,
                    config.target_version,
                    config.compiler_search_paths,
                    include_dependencies=True,
                    snapshot=batch.snapshot,
                ))
            except OverlayLayoutConflictError as exc:
                raise OverlayLayoutConflictError(f"cannot export a shared closure: {exc}") from exc
        if shared_overlay is not None and config.closure_output is not None:
            retained = stage_retained_closure_inputs(config.closure_output, shared_overlay.root)
            batch.retained_closure_inputs = retained
            if not retained.files and len(scopes) == 1:
                shared_overlay = None
        for files, generated, sources, snapshot in scopes:
            _validate_target_scope(
                files, generated, sources, snapshot, config, has_dependencies, shared_overlay
            )
    return decide_run_validation(batch.reports, config)


def _validate_target_scope(
    files: list[MigrationFile],
    generated: list[GeneratedMigration],
    sources: dict[Path, str],
    snapshot: ImportClosure | None,
    config: Config,
    has_dependencies: bool,
    shared_overlay: TargetOverlay | None,
) -> None:
    declared_sources = {**(snapshot.sources if snapshot else {}), **sources}
    target_declared_spec = _declared_spec(
        tuple(
            ContentIdentity(str(path.resolve()), hashlib.sha256(source.encode()).hexdigest())
            for path, source in declared_sources.items()
        ),
        (CompilerDeclaration("target-version", config.target_version),),
    )
    with target_overlay(
        sources,
        config.target_version,
        config.compiler_search_paths,
        include_dependencies=config.include_dependencies,
        snapshot=snapshot,
    ) as overlay:
        shared_view = None
        if shared_overlay is not None:
            assert overlay is not None
            shared_view = replace(
                shared_overlay,
                search_paths=tuple(
                    shared_overlay.root / path.relative_to(overlay.root)
                    for path in overlay.search_paths
                ),
            )
        for migration in files:
            if migration.report.validation_mode == "consumer-roots":
                continue
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
                sources[migration.path],
                config,
                overlay,
            )
            if shared_view is not None and migration.path.suffix == ".vy":
                _check_shared_target(
                    migration.path, sources[migration.path], config, target_compile, shared_view
                )
            migration.target_compile = target_compile
            _record_target_compile(
                migration.report,
                target_compile,
                target_declared_spec,
            )
            if (
                target_compile.status == "failed"
                and target_compile.failure_origin == "compiler"
                and has_dependencies
                and not config.include_dependencies
            ):
                migration.report.target_error = (migration.report.target_error or "") + (
                    "\nDependencies were validated unchanged. Use --include-dependencies with "
                    "--closure-output or --closure-archive if they also need migration."
                )
            comparisons = compare_validation_artifacts(migration.source_compile, target_compile)
            (
                migration.report.abi_equal,
                migration.report.method_ids_equal,
                migration.report.storage_layout_equal,
            ) = comparisons.equalities
            (
                migration.report.abi_diff,
                migration.report.method_id_diff,
                migration.report.storage_layout_diff,
            ) = comparisons.details
            migration.validation_diagnostics.extend(
                _add_validation_diagnostics(
                    migration.report,
                    migration.source_version,
                    config,
                    migration.source_compiler,
                )
            )

        for migration in generated:
            target_compile = compile_target_source(
                migration.file.path,
                sources[migration.file.path],
                config,
                overlay,
            )
            migration.target_compile = target_compile
            _record_target_compile(
                migration.report,
                target_compile,
                target_declared_spec,
            )


def _check_shared_target(
    path: Path,
    source: str,
    config: Config,
    isolated: CompileResult,
    shared_overlay: TargetOverlay,
) -> None:
    """Verify the same compilation in the actual flat tree being exported."""
    if isolated.status not in {"passed", "degraded"}:
        return
    shared = compile_target_source(path, source, config, shared_overlay)
    if shared.status != isolated.status:
        raise OverlayLayoutConflictError(
            f"cannot export a shared closure for {path}: {shared.stderr or shared.status}"
        )
    bytecode = (isolated.artifacts or {}).get("bytecode")
    if not isinstance(bytecode, str) or bytecode != (shared.artifacts or {}).get("bytecode"):
        raise OverlayLayoutConflictError(
            f"cannot export a shared closure for {path}: bytecode differs from the isolated project compile"
        )
    compared = compare_validation_artifacts(isolated, shared)
    if False in compared.equalities:
        raise OverlayLayoutConflictError(
            f"cannot export a shared closure for {path}: validation artifacts differ from the isolated project compile"
        )


def _compile_source(
    request: MigrationRequest, config: Config
) -> tuple[SourceCompileAttempt | None, CompileResult]:
    if not request.source_attempts:
        return None, CompileResult("skipped")

    first_attempt: SourceCompileAttempt | None = None
    first_result: CompileResult | None = None
    compile_source = compile_source_ast if request.role == "dependency" else compile_source_file
    for attempt in request.source_attempts:
        attempt_config = replace(config, source_version=attempt.rule_version, source_ast=None)
        result = compile_source(request.path, attempt_config, attempt.compile_version)
        if first_result is None:
            first_attempt = attempt
            first_result = result
        if result.status == "passed":
            return attempt, result
    assert first_attempt is not None and first_result is not None
    return first_attempt, first_result


def _unchanged_candidate(_path: Path, source: str) -> str:
    return source


def candidate_sources(batch: MigrationBatch, resolve_candidate: CandidateSource) -> dict[Path, str]:
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


def _declared_spec(
    sources: tuple[ContentIdentity, ...],
    compiler_declarations: tuple[CompilerDeclaration, ...],
) -> DeclaredSpec:
    digest = hashlib.sha256()
    ordered_sources = tuple(sorted(sources, key=lambda source: source.path))
    for source in ordered_sources:
        digest.update(source.path.encode())
        digest.update(b"\0")
        digest.update(source.sha256.encode())
        digest.update(b"\0")
    return DeclaredSpec(digest.hexdigest(), ordered_sources, compiler_declarations)


def _validation_attestation(
    result: CompileResult,
    declared_spec: DeclaredSpec,
    *,
    attempt_source: ContentIdentity,
    failure_origin: TargetFailureOrigin | None = None,
) -> ValidationAttestation:
    origin = failure_origin if failure_origin is not None else result.failure_origin
    attempt = CompileAttempt(
        sequence=1,
        source=attempt_source,
        compiler_started=result.compiler_started,
        completion_status=result.completion_status,
        exit_status=result.exit_status,
        failure_origin=origin,
    )
    return ValidationAttestation(
        declared_spec=declared_spec,
        authority_rule=result.compiler_authority,
        resolved_compiler=result.compiler_identity,
        dependency_context=result.dependency_context or DependencyContext(mode="isolated"),
        compiler_started=result.compiler_started,
        completion_status=result.completion_status,
        exit_status=result.exit_status,
        validated_source_set=result.validated_sources,
        attempt_sequence=(attempt,),
        failure_origin=origin,
        compiler_output=result.compiler_output,
    )


def _target_failure_origin(
    origin: object,
    *,
    role: str,
) -> TargetFailureOrigin | None:
    if origin == "compiler":
        return "fixed-target-dependency" if role == "dependency" else "fixed-target-rewrite"
    if origin in {
        "compiler-internal",
        "environment",
        "launch",
        "timeout",
        "adapter",
    }:
        return origin
    return None


def _record_target_compile(
    report: FileReport,
    target_compile: CompileResult,
    declared_spec: DeclaredSpec,
) -> None:
    report.target_compile = target_compile.status
    report.target_unavailable_artifacts = unavailable_validation_artifacts(target_compile)
    report.target_unavailable_formats = list(getattr(target_compile, "unavailable_formats", ()))
    report.target_error = target_compile.stderr if target_compile.status == "failed" else None
    report.target_error_type = (
        target_compile.error_type if target_compile.status == "failed" else None
    )
    report_source = next(
        (source for source in declared_spec.sources if source.path == str(report.path.resolve())),
        declared_spec.sources[0],
    )
    if target_compile.status != "skipped":
        origin = _target_failure_origin(target_compile.failure_origin, role=report.role)
        attempt_source = (
            target_compile.validated_sources[0]
            if target_compile.validated_sources
            else report_source
        )
        report.target_attestation = _validation_attestation(
            target_compile,
            declared_spec,
            attempt_source=attempt_source,
            failure_origin=origin,
        )


def _reset_target_validation(report: FileReport, validation_diagnostics: list[Diagnostic]) -> None:
    report.target_compile = "skipped"
    report.target_unavailable_artifacts.clear()
    report.target_unavailable_formats.clear()
    report.target_error = None
    report.target_error_type = None
    report.target_attestation = None
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
            diagnostic for diagnostic in report.diagnostics if id(diagnostic) not in validation_ids
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
