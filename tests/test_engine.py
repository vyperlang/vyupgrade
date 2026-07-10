from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from vyupgrade import engine
from vyupgrade.compiler import CompileResult
from vyupgrade.engine import MigrationRequest, SourceCompileAttempt
from vyupgrade.models import Config, Diagnostic, Fix, GeneratedFile, RewriteResult


VALIDATION_ARTIFACTS = {"abi": [], "method_identifiers": {}, "layout": {}}
SOURCE_ARTIFACTS = {**VALIDATION_ARTIFACTS, "ast": {}}


def _config(tmp_path: Path, **kwargs) -> Config:
    values = {"paths": (tmp_path,), "target_version": "0.4.3"}
    values.update(kwargs)
    return Config(**values)


def _request(
    path: Path,
    source_version: str = "0.3.10",
    attempts: tuple[SourceCompileAttempt, ...] | None = None,
) -> MigrationRequest:
    return MigrationRequest(
        path,
        "#pragma version 0.3.10\nx: uint256\n",
        source_version,
        attempts or (SourceCompileAttempt(source_version, source_version),),
    )


def _unchanged_rewrite(source, config, path) -> RewriteResult:
    return RewriteResult(source, [], [])


def test_bounded_request_preserves_cli_compiler_selection(tmp_path: Path) -> None:
    path = tmp_path / "Contract.vy"
    source = "#pragma version >0.3.10\nx: uint256\n"

    inferred = engine.bounded_migration_request(path, source, _config(tmp_path))
    explicit = engine.bounded_migration_request(
        path, source, _config(tmp_path, source_vyper="/tmp/vyper")
    )

    assert inferred.source_attempts == (
        SourceCompileAttempt("0.4.3", ">0.3.10", "0.4.3"),
    )
    assert explicit.source_attempts == (
        SourceCompileAttempt(">0.3.10", ">0.3.10"),
    )


def test_prepare_uses_first_passing_retry_and_its_rule_version(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "Contract.vy"
    attempts = tuple(
        SourceCompileAttempt(version, version)
        for version in ("0.3.0", "0.3.1", "0.3.2")
    )
    seen: dict[str, object] = {"compile": []}

    def compile_source(path, config, source_version):
        seen["compile"].append(source_version)
        if source_version == "0.3.0":
            return CompileResult("failed", stderr="first failed")
        return CompileResult("passed", artifacts=SOURCE_ARTIFACTS)

    def apply(source, config, path):
        seen["rule_version"] = config.source_version
        return RewriteResult(source, [], [])

    monkeypatch.setattr(engine, "compile_source_file", compile_source)
    monkeypatch.setattr(engine, "apply_rules", apply)

    batch = engine.prepare_migrations(
        (_request(path, "0.3.0", attempts),), _config(tmp_path)
    )

    assert seen == {"compile": ["0.3.0", "0.3.1"], "rule_version": "0.3.1"}
    assert batch.files[0].source_version == "0.3.1"
    assert batch.files[0].source_compile.status == "passed"


def test_prepare_retains_first_failure_when_all_retries_fail(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "Contract.vy"
    attempts = tuple(
        SourceCompileAttempt(version, version)
        for version in ("0.3.0", "0.3.1", "0.3.2")
    )
    seen: dict[str, object] = {"compile": []}

    def compile_source(path, config, source_version):
        seen["compile"].append(source_version)
        return CompileResult("failed", stderr=f"failed {source_version}")

    def apply(source, config, path):
        seen["rule_version"] = config.source_version
        return RewriteResult(source, [], [])

    monkeypatch.setattr(engine, "compile_source_file", compile_source)
    monkeypatch.setattr(engine, "apply_rules", apply)

    batch = engine.prepare_migrations(
        (_request(path, "0.3.0", attempts),), _config(tmp_path)
    )

    assert seen == {
        "compile": ["0.3.0", "0.3.1", "0.3.2"],
        "rule_version": "0.3.0",
    }
    assert batch.files[0].source_compile.stderr == "failed 0.3.0"


def test_source_ast_is_owned_by_each_file(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    asts: dict[str, object] = {}

    def compile_source(path, config, source_version):
        artifacts = dict(VALIDATION_ARTIFACTS)
        if path == first:
            artifacts["ast"] = {"owner": "first"}
        return CompileResult("passed", artifacts=artifacts)

    def apply(source, config, path):
        asts[path.name] = config.source_ast
        return RewriteResult(source, [], [])

    monkeypatch.setattr(engine, "compile_source_file", compile_source)
    monkeypatch.setattr(engine, "apply_rules", apply)
    config = _config(tmp_path, source_ast={"stale": True})

    engine.prepare_migrations((_request(first), _request(second)), config)

    assert asts == {"First.vy": {"owner": "first"}, "Second.vy": None}
    assert config.source_ast == {"stale": True}


def test_newer_source_cannot_split_interfaces_when_diagnostic_is_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Main.vy"
    original = """#pragma version >=0.5.0a1,<0.6.0

interface Token:
    def balanceOf(owner: address) -> uint256: view
"""
    request = MigrationRequest(
        path,
        original,
        ">=0.5.0a1,<0.6.0",
        (),
    )
    config = _config(
        tmp_path,
        split_interfaces=True,
        ignore=frozenset({"VYD016"}),
    )

    batch = engine.prepare_migrations((request,), config)

    assert batch.files[0].rewrite.source == original
    assert batch.files[0].report.diagnostics == []
    assert batch.files[0].report.changed is False
    assert batch.generated == []


def test_candidate_override_and_revalidation_reset_only_engine_diagnostics(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "Contract.vy"
    rule_diagnostic = Diagnostic("VYD007", 9, "rule-owned diagnostic")
    compiled: list[str] = []

    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )
    monkeypatch.setattr(
        engine,
        "apply_rules",
        lambda source, config, path: RewriteResult(
            source + "# rewritten\n", [], [rule_diagnostic]
        ),
    )

    def compile_target(path, source, config, overlay):
        compiled.append(source)
        artifacts = (
            {
                **VALIDATION_ARTIFACTS,
                "abi": [{"type": "function", "name": "changed", "inputs": []}],
            }
            if len(compiled) == 1
            else VALIDATION_ARTIFACTS
        )
        return CompileResult("passed", artifacts=artifacts)

    monkeypatch.setattr(engine, "compile_target_source", compile_target)
    batch = engine.prepare_migrations((_request(path),), _config(tmp_path))

    first = engine.validate_migrations(
        batch, _config(tmp_path), lambda _path, _fallback: "# formatted one\n"
    )
    second = engine.validate_migrations(
        batch, _config(tmp_path), lambda _path, _fallback: "# formatted two\n"
    )

    assert compiled == ["# formatted one\n", "# formatted two\n"]
    assert first.status == "blocked"
    assert second.status == "passed"
    assert batch.files[0].report.abi_diff == []
    assert [
        diagnostic
        for diagnostic in batch.files[0].report.diagnostics
        if diagnostic.rule == "VYD007"
    ] == [rule_diagnostic]


def test_generated_interface_and_cross_file_sources_share_one_overlay(
    monkeypatch, tmp_path: Path
) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    interface = tmp_path / "Shared.vyi"
    overlay_sources: dict[Path, str] = {}
    compiled: list[tuple[Path, object]] = []
    overlay_token = object()

    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )

    def apply(source, config, path):
        generated = []
        if path == first:
            generated.append(
                GeneratedFile(
                    interface,
                    "@external\ndef ping(): ...\n",
                    Fix("VY120", 1, "split interface", "", ""),
                )
            )
        return RewriteResult(source, [], [], generated)

    @contextmanager
    def overlay(sources, target_version, search_paths):
        overlay_sources.update(sources)
        yield overlay_token

    def compile_target(path, source, config, overlay):
        compiled.append((path, overlay))
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    monkeypatch.setattr(engine, "apply_rules", apply)
    monkeypatch.setattr(engine, "target_overlay", overlay)
    monkeypatch.setattr(engine, "compile_target_source", compile_target)
    batch = engine.prepare_migrations((_request(first), _request(second)), _config(tmp_path))

    decision = engine.validate_migrations(batch, _config(tmp_path))

    assert set(overlay_sources) == {first, second, interface}
    assert {path for path, _overlay in compiled} == {first, second, interface}
    assert all(used_overlay is overlay_token for _path, used_overlay in compiled)
    assert batch.generated[0].report.target_compile == "passed"
    assert decision.status == "passed"


def test_validation_rejects_duplicate_resolved_destinations(
    monkeypatch, tmp_path: Path
) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    interface = tmp_path / "Shared.vyi"

    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )

    def apply(source, config, path):
        declaration = "one" if path == first else "two"
        generated = GeneratedFile(
            interface,
            f"@external\ndef {declaration}(): ...\n",
            Fix("VY120", 1, "split interface", "", ""),
        )
        return RewriteResult(source, [], [], [generated])

    monkeypatch.setattr(engine, "apply_rules", apply)
    batch = engine.prepare_migrations(
        (_request(first), _request(second)), _config(tmp_path)
    )

    with pytest.raises(engine.CandidatePathConflictError, match=r"Shared\.vyi"):
        engine.validate_migrations(batch, _config(tmp_path))


def test_malformed_target_artifacts_remain_unwaivable(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "Contract.vy"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("passed", artifacts=SOURCE_ARTIFACTS),
    )
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult(
            "passed",
            artifacts={
                "abi": [None],
                "method_identifiers": {},
                "layout": {"storage_layout": []},
            },
        ),
    )
    config = _config(
        tmp_path,
        allow_unvalidated_source=True,
        allow_abi_change=True,
        allow_method_id_change=True,
        allow_storage_layout_change=True,
    )
    batch = engine.prepare_migrations((_request(path),), config)

    decision = engine.validate_migrations(batch, config)

    assert decision.status == "blocked"
    assert decision.blockers[0].code == "target_artifacts_unavailable"


def test_source_failure_uses_typed_waiver(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "Contract.vy"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("failed", stderr="source failed"),
    )
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult("passed", artifacts=VALIDATION_ARTIFACTS),
    )
    config = _config(tmp_path, allow_unvalidated_source=True)
    batch = engine.prepare_migrations((_request(path),), config)

    decision = engine.validate_migrations(batch, config)

    assert decision.status == "waived"
    assert [issue.code for issue in decision.waivers] == ["source_compile_failed"]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (CompileResult("passed", artifacts=VALIDATION_ARTIFACTS), "passed"),
        (CompileResult("failed", stderr="interface failed"), "blocked"),
    ],
)
def test_interface_validation_is_target_only(
    target: CompileResult, expected: str, monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "Interface.vyi"
    request = MigrationRequest(
        path,
        "@external\ndef ping(): ...\n",
        "0.3.10",
        (SourceCompileAttempt("0.3.10", "0.3.10"),),
    )
    monkeypatch.setattr(
        engine, "compile_source_file", lambda *_args: CompileResult("skipped")
    )
    monkeypatch.setattr(engine, "apply_rules", _unchanged_rewrite)
    monkeypatch.setattr(engine, "compile_target_source", lambda *_args: target)
    config = _config(tmp_path)
    batch = engine.prepare_migrations((request,), config)

    decision = engine.validate_migrations(batch, config)

    assert batch.files[0].report.source_compile == "skipped"
    assert decision.status == expected
