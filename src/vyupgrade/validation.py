from __future__ import annotations

from collections.abc import Iterable

from .models import (
    Config,
    FileReport,
    ValidationDecision,
    ValidationIssue,
    ValidationIssueCode,
)


def decide_run_validation(
    reports: Iterable[FileReport], config: Config
) -> ValidationDecision:
    decisions: list[ValidationDecision] = []
    for report in reports:
        decision = decide_file_validation(report, config)
        report.validation_decision = decision
        decisions.append(decision)

    blockers = tuple(issue for decision in decisions for issue in decision.blockers)
    waivers = tuple(issue for decision in decisions for issue in decision.waivers)
    if blockers:
        return ValidationDecision("blocked", False, blockers, waivers)
    if waivers:
        return ValidationDecision("waived", True, (), waivers)
    if any(decision.status != "not-required" for decision in decisions):
        return ValidationDecision("passed", True)
    return ValidationDecision()


def decide_file_validation(report: FileReport, config: Config) -> ValidationDecision:
    if report.source_compile == "skipped" and report.target_compile == "skipped":
        return ValidationDecision()

    blockers: list[ValidationIssue] = []
    waivers: list[ValidationIssue] = []

    def require(
        code: ValidationIssueCode,
        message: str,
        *,
        allowed: bool = False,
        waiver: str | None = None,
    ) -> None:
        issue = ValidationIssue(code, message, report.path, waiver if allowed else None)
        (waivers if allowed else blockers).append(issue)

    if report.target_compile != "passed":
        require("target_compile_failed", "target compilation did not pass")
    elif report.target_unavailable_artifacts:
        require(
            "target_artifacts_unavailable",
            "target compiler did not produce required artifacts: "
            + ", ".join(report.target_unavailable_artifacts),
        )

    # Interfaces have no deployable source artifacts to compare. Their safety
    # boundary is successful target compilation through an import harness.
    if report.path.suffix == ".vyi":
        return _decision(blockers, waivers)

    source_validated = report.source_compile in {"passed", "degraded"}
    if not source_validated:
        require(
            "source_compile_failed",
            "source compilation did not pass",
            allowed=config.allow_unvalidated_source,
            waiver="--allow-unvalidated-source",
        )
    elif report.source_unavailable_artifacts:
        require(
            "source_artifacts_unavailable",
            "source compiler did not produce required artifacts: "
            + ", ".join(report.source_unavailable_artifacts),
            allowed=config.allow_unvalidated_source,
            waiver="--allow-unvalidated-source",
        )

    comparisons = (
        (
            "abi_changed",
            "ABI changed after migration",
            report.abi_equal,
            config.allow_abi_change,
            "--allow-abi-change",
        ),
        (
            "method_identifiers_changed",
            "method identifiers changed after migration",
            report.method_ids_equal,
            config.allow_method_id_change,
            "--allow-method-id-change",
        ),
        (
            "storage_layout_changed",
            "storage layout changed after migration",
            report.storage_layout_equal,
            config.allow_storage_layout_change,
            "--allow-storage-layout-change",
        ),
    )
    for code, message, equal, allowed, waiver in comparisons:
        if equal is False:
            require(code, message, allowed=allowed, waiver=waiver)

    if (
        source_validated
        and not report.source_unavailable_artifacts
        and report.target_compile == "passed"
        and not report.target_unavailable_artifacts
        and any(equal is None for _, _, equal, _, _ in comparisons)
    ):
        require(
            "artifact_comparison_unavailable",
            "one or more artifact comparisons were unavailable",
            allowed=config.allow_unvalidated_source,
            waiver="--allow-unvalidated-source",
        )

    return _decision(blockers, waivers)


def validation_exit_code(decision: ValidationDecision) -> int | None:
    if decision.status != "blocked":
        return None
    codes = {issue.code for issue in decision.blockers}
    if codes & {"target_compile_failed", "target_artifacts_unavailable"}:
        return 2
    if codes & {
        "source_compile_failed",
        "source_artifacts_unavailable",
        "artifact_comparison_unavailable",
    }:
        return 3
    return 7


def _decision(
    blockers: list[ValidationIssue], waivers: list[ValidationIssue]
) -> ValidationDecision:
    if blockers:
        return ValidationDecision("blocked", False, tuple(blockers), tuple(waivers))
    if waivers:
        return ValidationDecision("waived", True, (), tuple(waivers))
    return ValidationDecision("passed", True)
