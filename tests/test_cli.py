from __future__ import annotations

import json
import hashlib
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from vyupgrade import cli, engine
from vyupgrade.cli import _write_diff, main
from vyupgrade.compiler import CompileResult
from vyupgrade.engine import _add_validation_diagnostics, _evm_default_diagnostic
from vyupgrade.models import Config, Diagnostic, FileReport


VALIDATION_ARTIFACTS = {"abi": [], "method_identifiers": {}, "layout": {}}


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def passing_compiler(monkeypatch):
    def compile_source_file(
        path: Path, config: Config, source_version: str | None
    ) -> CompileResult:
        return CompileResult("passed", artifacts={**VALIDATION_ARTIFACTS, "ast": {}})

    def compile_target_source(
        path: Path,
        source: str,
        config: Config,
        overlay=None,
    ) -> CompileResult:
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    monkeypatch.setattr(engine, "compile_source_file", compile_source_file)
    monkeypatch.setattr(engine, "compile_target_source", compile_target_source)
    return None


@pytest.fixture
def failing_target_compiler(monkeypatch, passing_compiler):
    def compile_target_source(
        path: Path,
        source: str,
        config: Config,
        overlay=None,
    ) -> CompileResult:
        return CompileResult("failed", stderr="target failed")

    monkeypatch.setattr(engine, "compile_target_source", compile_target_source)
    return None


def test_check_mode_reports_changes(tmp_path: Path, passing_compiler) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )

    report = tmp_path / "report.json"
    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code in {1, 2}
    data = json.loads(report.read_text())
    assert data["write_requested"] is False
    assert data["wrote_changes"] is False
    assert data["files"][0]["changed"] is True
    assert any(fix["rule"] == "VY002" for fix in data["files"][0]["fixes"])


def test_write_mode_is_idempotent(tmp_path: Path, passing_compiler) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )

    report = tmp_path / "report.json"
    code = main([str(contract), "--write", "--report-json", str(report)])

    assert code == 0
    rewritten = contract.read_text()
    assert "#pragma version 0.4.3" in rewritten
    assert "@deploy\ndef __init__" in rewritten
    data = json.loads(report.read_text())
    assert data["write_requested"] is True
    assert data["wrote_changes"] is True
    assert data["files"][0]["validation"]["target_compile"] == "passed"

    second_report = tmp_path / "second.json"
    second = main([str(contract), "--check", "--report-json", str(second_report)])
    assert second == 0
    assert json.loads(second_report.read_text())["files"][0]["changed"] is False


def test_broad_pragma_source_validation_uses_target_bounded_compiler(
    tmp_path: Path, monkeypatch
) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text(
        """#pragma version >0.3.10

@external
def f() -> uint256:
    return 1
""",
        encoding="utf-8",
    )
    seen: dict[str, str | None] = {}

    def compile_source_file(
        path: Path, config: Config, source_version: str | None
    ) -> CompileResult:
        seen["source_version"] = source_version
        return CompileResult("passed", artifacts={**VALIDATION_ARTIFACTS, "ast": {}})

    def compile_target_source(
        path: Path,
        source: str,
        config: Config,
        overlay=None,
    ) -> CompileResult:
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    monkeypatch.setattr(engine, "compile_source_file", compile_source_file)
    monkeypatch.setattr(engine, "compile_target_source", compile_target_source)

    report = tmp_path / "report.json"
    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code == 1
    assert seen["source_version"] == "0.4.3"
    data = json.loads(report.read_text())
    file_report = data["files"][0]
    assert [fix["rule"] for fix in file_report["fixes"]] == ["VY001"]
    assert file_report["validation"]["source_version"] == ">0.3.10"
    assert file_report["validation"]["source_compiler"] == "0.4.3"
    assert not [diag for diag in file_report["diagnostics"] if diag["rule"] == "VYD009"]


def test_explicit_source_vyper_does_not_report_inferred_compiler(
    tmp_path: Path, monkeypatch
) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text(
        """#pragma version >0.3.10

@external
def f() -> uint256:
    return 1
""",
        encoding="utf-8",
    )
    seen: dict[str, str | None] = {}

    def compile_source_file(
        path: Path, config: Config, source_version: str | None
    ) -> CompileResult:
        seen["source_version"] = source_version
        seen["source_vyper"] = config.source_vyper
        return CompileResult("passed", artifacts={**VALIDATION_ARTIFACTS, "ast": {}})

    def compile_target_source(
        path: Path,
        source: str,
        config: Config,
        overlay=None,
    ) -> CompileResult:
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    monkeypatch.setattr(engine, "compile_source_file", compile_source_file)
    monkeypatch.setattr(engine, "compile_target_source", compile_target_source)

    report = tmp_path / "report.json"
    code = main(
        [
            str(contract),
            "--check",
            "--source-vyper",
            "/tmp/vyper",
            "--report-json",
            str(report),
        ]
    )

    assert code == 1
    assert seen["source_vyper"] == "/tmp/vyper"
    assert seen["source_version"] == ">0.3.10"
    validation = json.loads(report.read_text())["files"][0]["validation"]
    assert validation["source_version"] == ">0.3.10"
    assert validation["source_compiler"] is None


def test_write_mode_reports_missing_mamushi_formatter(
    tmp_path: Path, monkeypatch, capsys, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    def fake_run(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    original = contract.read_text(encoding="utf-8")
    code = main([str(contract), "--write", "--format", "mamushi", "--report-json", str(report)])

    assert code == 6
    assert contract.read_text(encoding="utf-8") == original
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is False
    assert data["write_status"] == "failed"
    assert data["formatter_status"] == "failed"
    assert data["formatter_output"] == "mamushi executable not found"
    assert data["test_status"] == "skipped"
    assert "formatter: failed" in capsys.readouterr().out


def test_write_mode_reports_failing_mamushi_formatter(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    original = contract.read_text(encoding="utf-8")

    def fake_run(command, **kwargs):
        assert command[0] == "mamushi"
        return cli.subprocess.CompletedProcess(command, 2, "formatted stdout", "formatted stderr")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    code = main(
        [
            str(contract),
            "--write",
            "--format",
            "mamushi",
            "--test-command",
            "should-not-run",
            "--report-json",
            str(report),
        ]
    )

    assert code == 6
    assert contract.read_text(encoding="utf-8") == original
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is False
    assert data["formatter_command"].startswith("mamushi ")
    assert data["formatter_status"] == "failed"
    assert data["formatter_output"] == (
        "mamushi exited with status 2\nformatted stdout\nformatted stderr"
    )
    assert data["test_status"] == "skipped"


def test_write_mode_reports_timed_out_mamushi_without_mutation(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 120, output="partial output")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    code = main(
        [str(contract), "--write", "--format", "mamushi", "--report-json", str(report)]
    )

    assert code == 6
    assert contract.read_text(encoding="utf-8") == original
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is False
    assert data["formatter_status"] == "failed"
    assert "mamushi timed out after 120 seconds" in data["formatter_output"]


def test_formatter_runs_on_staged_candidates_and_final_bytes_are_revalidated(
    tmp_path: Path, monkeypatch, capsys, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    compiled: list[str] = []
    original_apply_rules = engine.apply_rules

    def apply_rules_with_diagnostic(source, config, path):
        result = original_apply_rules(source, config, path)
        result.diagnostics.append(Diagnostic("TEST001", 1, "preserve this diagnostic"))
        return result

    def compile_target_source(path, source, config, overlay=None):
        compiled.append(source)
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    def fake_run(command, **kwargs):
        assert command[0] == "mamushi"
        assert all(Path(path).resolve() != contract.resolve() for path in command[1:])
        staged = Path(command[1])
        assert staged.name == contract.name
        staged.write_text(staged.read_text(encoding="utf-8") + "# formatted\n", encoding="utf-8")
        return cli.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(engine, "apply_rules", apply_rules_with_diagnostic)
    monkeypatch.setattr(engine, "compile_target_source", compile_target_source)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    code = main(
        [
            str(contract),
            "--write",
            "--diff",
            "--format",
            "mamushi",
            "--report-json",
            str(report),
        ]
    )

    assert code == 0
    assert len(compiled) == 2
    assert "# formatted" not in compiled[0]
    assert compiled[1].endswith("# formatted\n")
    final = contract.read_bytes()
    assert final.endswith(b"# formatted\n")
    assert "+# formatted" in capsys.readouterr().out
    data = json.loads(report.read_text())
    assert data["files"][0]["candidate_sha256"] == hashlib.sha256(final).hexdigest()
    assert data["files"][0]["final_sha256"] == hashlib.sha256(final).hexdigest()
    assert data["files"][0]["final_matches_candidate"] is True
    diagnostics = data["files"][0]["diagnostics"]
    assert [item["rule"] for item in diagnostics].count("TEST001") == 1
    assert [item["rule"] for item in diagnostics].count("VYD009") == 1


def test_formatter_output_that_fails_final_validation_is_not_committed(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"

    def compile_target_source(path, source, config, overlay=None):
        if "# formatter-broke-source" in source:
            return CompileResult("failed", stderr="formatted candidate failed")
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    def fake_run(command, **kwargs):
        staged = Path(command[1])
        staged.write_text(
            staged.read_text(encoding="utf-8") + "# formatter-broke-source\n",
            encoding="utf-8",
        )
        return cli.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(engine, "compile_target_source", compile_target_source)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    code = main(
        [str(contract), "--write", "--format", "mamushi", "--report-json", str(report)]
    )

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is False
    assert data["write_status"] == "blocked"
    assert data["formatter_status"] == "passed"
    assert data["validation_decision"]["blockers"][0]["code"] == "target_compile_failed"


def test_formatted_generated_interface_bytes_are_revalidated(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "Main.vy"
    original = """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
"""
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"
    compiled_interfaces: list[str] = []

    def compile_target(path, source, config, overlay=None):
        if path.suffix == ".vyi":
            compiled_interfaces.append(source)
            if "# broken generated interface" in source:
                return CompileResult("failed", stderr="formatted interface failed")
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    def break_staged_interface(command, **kwargs):
        interface = next(Path(path) for path in command[1:] if str(path).endswith(".vyi"))
        interface.write_text(
            interface.read_text(encoding="utf-8") + "# broken generated interface\n",
            encoding="utf-8",
        )
        return cli.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(engine, "compile_target_source", compile_target)
    monkeypatch.setattr(cli.subprocess, "run", break_staged_interface)

    code = main(
        [
            str(contract),
            "--write",
            "--split-interfaces",
            "--format",
            "mamushi",
            "--report-json",
            str(report),
        ]
    )

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original
    assert not (tmp_path / "Token.vyi").exists()
    assert len(compiled_interfaces) == 2
    assert "# broken generated interface" not in compiled_interfaces[0]
    assert "# broken generated interface" in compiled_interfaces[1]
    data = json.loads(report.read_text())
    assert data["write_status"] == "blocked"


def test_post_write_test_failure_returns_nonzero_and_persists_report(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: cli.subprocess.CompletedProcess(
            command, 2, "test stdout", "test stderr"
        ),
    )

    code = main(
        [str(contract), "--write", "--test-command", "false", "--report-json", str(report)]
    )

    assert code == 8
    assert "#pragma version 0.4.3" in contract.read_text(encoding="utf-8")
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is True
    assert data["test_status"] == "failed"
    assert data["test_output"] == "test command exited with status 2\ntest stdout\ntest stderr"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            subprocess.TimeoutExpired("tests", 600, output="partial"),
            "test command timed out after 600 seconds",
        ),
        (OSError("shell unavailable"), "test command failed to start: shell unavailable"),
    ],
)
def test_post_write_test_runtime_errors_return_nonzero_and_persist_report(
    tmp_path: Path, monkeypatch, passing_compiler, failure: BaseException, message: str
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    def fake_run(command, **kwargs):
        raise failure

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    code = main(
        [str(contract), "--write", "--test-command", "tests", "--report-json", str(report)]
    )

    assert code == 8
    assert "#pragma version 0.4.3" in contract.read_text(encoding="utf-8")
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is True
    assert data["test_status"] == "failed"
    assert message in data["test_output"]


def test_post_write_test_mutation_is_detected_in_final_hashes(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    def mutate_planned_file(command, **kwargs):
        contract.write_text("# changed by tests\n", encoding="utf-8")
        return cli.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", mutate_planned_file)

    code = main(
        [str(contract), "--write", "--test-command", "tests", "--report-json", str(report)]
    )

    assert code == 8
    data = json.loads(report.read_text())
    file_report = data["files"][0]
    assert data["test_status"] == "failed"
    assert "test command changed planned destinations" in data["test_output"]
    assert file_report["final_matches_candidate"] is False
    assert file_report["final_sha256"] == hashlib.sha256(contract.read_bytes()).hexdigest()


def test_incomplete_rollback_is_reported_as_partial_on_disk_change(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    def leave_candidate_behind(plan):
        entry = plan.writes[0]
        entry.path.write_bytes(entry.candidate)
        raise cli.WriteTransactionError(
            "injected incomplete rollback",
            rollback_incomplete=True,
            affected_paths=(entry.path,),
        )

    monkeypatch.setattr(cli.MigrationPlan, "commit", leave_candidate_behind)

    code = main([str(contract), "--write", "--report-json", str(report)])

    assert code == 9
    data = json.loads(report.read_text())
    assert data["write_status"] == "rollback-incomplete"
    assert data["wrote_changes"] is True
    assert "injected incomplete rollback" in data["write_output"]
    assert data["files"][0]["final_matches_candidate"] is True


def test_write_mode_does_not_write_when_target_compile_fails(
    tmp_path: Path, failing_target_compiler
) -> None:
    contract = tmp_path / "bad.vy"
    original = """# @version 0.3.10
@external
def f(target: address):
    target.unknown()
"""
    contract.write_text(original, encoding="utf-8")

    code = main([str(contract), "--write"])

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original


def test_write_mode_does_not_write_when_source_compile_fails(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "bad-source.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("failed", stderr="source failed"),
    )

    code = main([str(contract), "--write", "--report-json", str(report)])

    assert code == 3
    assert contract.read_text(encoding="utf-8") == original
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is False
    assert data["validation_decision"]["status"] == "blocked"
    assert data["validation_decision"]["blockers"][0]["code"] == "source_compile_failed"


def test_allow_unvalidated_source_waives_source_failure(
    tmp_path: Path, monkeypatch, passing_compiler, capsys
) -> None:
    contract = tmp_path / "bad-source.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n", encoding="utf-8"
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("failed", stderr="source failed"),
    )

    code = main(
        [
            str(contract),
            "--write",
            "--allow-unvalidated-source",
            "--report-json",
            str(report),
        ]
    )

    assert code == 0
    assert "#pragma version 0.4.3" in contract.read_text(encoding="utf-8")
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is True
    assert data["validation_decision"]["status"] == "waived"
    assert data["validation_decision"]["waivers"][0]["waiver"] == (
        "--allow-unvalidated-source"
    )
    assert "write validation: waived" in capsys.readouterr().out


def test_patch_level_abi_mismatch_blocks_even_when_diagnostic_is_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    contract = tmp_path / "patch-level.vy"
    original = "#pragma version 0.4.2\n@external\ndef f():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult(
            "passed", artifacts={**VALIDATION_ARTIFACTS, "ast": {}}
        ),
    )
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult(
            "passed",
            artifacts={
                **VALIDATION_ARTIFACTS,
                "abi": [{"type": "function", "name": "changed", "inputs": []}],
            },
        ),
    )

    code = main(
        [
            str(contract),
            "--write",
            "--ignore",
            "VYD007",
            "--report-json",
            str(report),
        ]
    )

    assert code == 7
    assert contract.read_text(encoding="utf-8") == original
    data = json.loads(report.read_text())
    assert not [diag for diag in data["files"][0]["diagnostics"] if diag["rule"] == "VYD007"]
    assert data["validation_decision"]["blockers"][0]["code"] == "abi_changed"


@pytest.mark.parametrize(
    ("target_artifacts", "flag", "code"),
    [
        (
            {
                **VALIDATION_ARTIFACTS,
                "abi": [{"type": "function", "name": "changed", "inputs": []}],
            },
            "--allow-abi-change",
            "abi_changed",
        ),
        (
            {**VALIDATION_ARTIFACTS, "method_identifiers": {"changed()": "0x12345678"}},
            "--allow-method-id-change",
            "method_identifiers_changed",
        ),
        (
            {
                **VALIDATION_ARTIFACTS,
                "layout": {"changed": {"slot": 0, "type": "uint256"}},
            },
            "--allow-storage-layout-change",
            "storage_layout_changed",
        ),
    ],
)
def test_artifact_change_waivers_are_narrow_and_reported(
    tmp_path: Path,
    monkeypatch,
    target_artifacts: dict[str, object],
    flag: str,
    code: str,
) -> None:
    contract = tmp_path / f"{code}.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n", encoding="utf-8"
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult(
            "passed", artifacts={**VALIDATION_ARTIFACTS, "ast": {}}
        ),
    )
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult("passed", artifacts=target_artifacts),
    )

    assert main([str(contract), "--write", flag, "--report-json", str(report)]) == 0

    data = json.loads(report.read_text())
    assert data["validation_decision"]["status"] == "waived"
    assert data["validation_decision"]["waivers"] == [
        {
            "code": code,
            "message": {
                "abi_changed": "ABI changed after migration",
                "method_identifiers_changed": "method identifiers changed after migration",
                "storage_layout_changed": "storage layout changed after migration",
            }[code],
            "path": str(contract),
            "waiver": flag,
        }
    ]


def test_artifact_waiver_does_not_cover_other_diff_classes(
    tmp_path: Path, monkeypatch
) -> None:
    contract = tmp_path / "multiple-diffs.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult(
            "passed", artifacts={**VALIDATION_ARTIFACTS, "ast": {}}
        ),
    )
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult(
            "passed",
            artifacts={
                "abi": [{"type": "function", "name": "changed", "inputs": []}],
                "method_identifiers": {"changed()": "0x12345678"},
                "layout": {"changed": {"slot": 0, "type": "uint256"}},
            },
        ),
    )

    assert (
        main(
            [
                str(contract),
                "--write",
                "--allow-abi-change",
                "--report-json",
                str(report),
            ]
        )
        == 7
    )

    assert contract.read_text(encoding="utf-8") == original
    decision = json.loads(report.read_text())["validation_decision"]
    assert [issue["code"] for issue in decision["waivers"]] == ["abi_changed"]
    assert [issue["code"] for issue in decision["blockers"]] == [
        "method_identifiers_changed",
        "storage_layout_changed",
    ]


def test_missing_source_artifact_blocks_without_source_waiver(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "missing-source-artifact.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult(
            "degraded",
            artifacts={"abi": [], "layout": {}, "ast": {}},
            unavailable_formats=("method_identifiers",),
        ),
    )

    assert main([str(contract), "--write", "--report-json", str(report)]) == 3
    assert contract.read_text(encoding="utf-8") == original
    validation = json.loads(report.read_text())["files"][0]["validation"]
    assert validation["source_compile"] == "degraded"
    assert validation["source_unavailable_artifacts"] == ["method_identifiers"]
    assert validation["source_unavailable_formats"] == ["method_identifiers"]


def test_optional_source_ast_unavailability_is_degraded_but_safe(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "missing-source-ast.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n", encoding="utf-8"
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult(
            "degraded",
            artifacts=VALIDATION_ARTIFACTS,
            unavailable_formats=("ast",),
        ),
    )

    assert main([str(contract), "--write", "--report-json", str(report)]) == 0

    validation = json.loads(report.read_text())["files"][0]["validation"]
    assert validation["source_compile"] == "degraded"
    assert validation["source_unavailable_artifacts"] == []
    assert validation["source_unavailable_formats"] == ["ast"]
    assert validation["decision"]["status"] == "passed"


def test_missing_target_artifact_is_not_waived_by_source_or_diff_flags(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "missing-target-artifact.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        engine,
        "compile_target_source",
        lambda *_args: CompileResult(
            "passed", artifacts={"abi": [], "method_identifiers": {}}
        ),
    )

    code = main(
        [
            str(contract),
            "--write",
            "--allow-unvalidated-source",
            "--allow-storage-layout-change",
            "--report-json",
            str(report),
        ]
    )

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original
    blockers = json.loads(report.read_text())["validation_decision"]["blockers"]
    assert [blocker["code"] for blocker in blockers] == ["target_artifacts_unavailable"]


def test_malformed_target_artifacts_block_write(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "malformed-target-artifacts.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"
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

    code = main([str(contract), "--write", "--report-json", str(report)])

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original
    validation = json.loads(report.read_text())["files"][0]["validation"]
    assert validation["target_unavailable_artifacts"] == ["abi", "layout"]
    assert validation["decision"]["blockers"][0]["code"] == "target_artifacts_unavailable"


def test_split_interfaces_writes_sibling_vyi_files(tmp_path: Path, passing_compiler) -> None:
    contract = tmp_path / "Main.vy"
    contract.write_text(
        """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable

@external
def f(token: Token, owner: address) -> uint256:
    return staticcall token.balanceOf(owner)
""",
        encoding="utf-8",
    )

    report = tmp_path / "report.json"
    code = main(
        [
            str(contract),
            "--write",
            "--split-interfaces",
            "--report-json",
            str(report),
        ]
    )

    assert code == 0
    assert (
        contract.read_text(encoding="utf-8")
        == """#pragma version 0.4.3

import Token
@external
def f(token: Token, owner: address) -> uint256:
    return staticcall token.balanceOf(owner)
"""
    )
    assert (
        (tmp_path / "Token.vyi").read_text(encoding="utf-8")
        == """@view
@external
def balanceOf(owner: address) -> uint256: ...
@external
def transfer(to: address, amount: uint256) -> bool: ...
"""
    )
    generated_report = next(
        item
        for item in json.loads(report.read_text())["files"]
        if item["path"] == str(tmp_path / "Token.vyi")
    )
    assert generated_report["validation"]["source_compile"] == "skipped"
    assert generated_report["validation"]["target_compile"] == "passed"
    assert generated_report["validation"]["abi_equal"] is None
    assert generated_report["validation"]["decision"]["status"] == "passed"


def test_generated_interface_target_failure_blocks_every_write(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "Main.vy"
    original = """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
"""
    contract.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"

    def fail_generated(path, source, config, overlay=None):
        if path.suffix == ".vyi":
            return CompileResult("failed", stderr="generated interface failed")
        return CompileResult("passed", artifacts=VALIDATION_ARTIFACTS)

    monkeypatch.setattr(engine, "compile_target_source", fail_generated)

    code = main(
        [
            str(contract),
            "--write",
            "--split-interfaces",
            "--report-json",
            str(report),
        ]
    )

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original
    assert not (tmp_path / "Token.vyi").exists()
    data = json.loads(report.read_text())
    generated_report = next(
        item for item in data["files"] if item["path"] == str(tmp_path / "Token.vyi")
    )
    assert generated_report["validation"]["target_compile"] == "failed"
    assert generated_report["validation"]["decision"]["status"] == "blocked"
    assert any(
        blocker["path"] == str(tmp_path / "Token.vyi")
        for blocker in data["validation_decision"]["blockers"]
    )
def test_split_interfaces_rejects_existing_generated_file_collision(
    tmp_path: Path, passing_compiler
) -> None:
    contract = tmp_path / "Main.vy"
    original = """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
"""
    contract.write_text(original, encoding="utf-8")
    token = tmp_path / "Token.vyi"
    token.write_text("# user-owned interface\n", encoding="utf-8")
    report = tmp_path / "report.json"

    code = main(
        [
            str(contract),
            "--write",
            "--split-interfaces",
            "--report-json",
            str(report),
        ]
    )

    assert code == 9
    assert contract.read_text(encoding="utf-8") == original
    assert token.read_text(encoding="utf-8") == "# user-owned interface\n"
    data = json.loads(report.read_text())
    assert data["wrote_changes"] is False
    assert data["write_status"] == "failed"
    assert "already exists with different content" in data["write_output"]


def test_split_interfaces_rejects_duplicate_generated_destinations(
    tmp_path: Path, passing_compiler
) -> None:
    originals: dict[Path, str] = {}
    for name in ("First.vy", "Second.vy"):
        source = """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
"""
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        originals[path] = source
    report = tmp_path / "report.json"

    code = main(
        [str(tmp_path), "--write", "--split-interfaces", "--report-json", str(report)]
    )

    assert code == 9
    assert not (tmp_path / "Token.vyi").exists()
    assert all(path.read_text(encoding="utf-8") == source for path, source in originals.items())
    assert "duplicate generated destination" in json.loads(report.read_text())["write_output"]


def test_split_interfaces_accepts_identical_generated_file_as_noop(
    tmp_path: Path, passing_compiler
) -> None:
    contract = tmp_path / "Main.vy"
    contract.write_text(
        """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
""",
        encoding="utf-8",
    )
    token = tmp_path / "Token.vyi"
    token_source = """@view
@external
def balanceOf(owner: address) -> uint256: ...
"""
    token.write_text(token_source, encoding="utf-8")
    report = tmp_path / "report.json"

    code = main(
        [
            str(tmp_path),
            "--write",
            "--split-interfaces",
            "--ignore",
            "VY001,VYD005",
            "--report-json",
            str(report),
        ]
    )

    assert code == 0
    assert "import Token" in contract.read_text(encoding="utf-8")
    assert token.read_text(encoding="utf-8") == token_source
    token_reports = [
        item for item in json.loads(report.read_text())["files"] if item["path"] == str(token)
    ]
    assert len(token_reports) == 2
    assert all(item["changed"] is False for item in token_reports)
    assert all(
        item["original_sha256"] == item["candidate_sha256"]
        for item in token_reports
    )


def test_split_interfaces_respects_rule_ignore(tmp_path: Path, passing_compiler) -> None:
    contract = tmp_path / "Main.vy"
    contract.write_text(
        """#pragma version 0.4.3

interface Token:
    def balanceOf(owner: address) -> uint256: view
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main(
        [
            str(contract),
            "--check",
            "--split-interfaces",
            "--ignore",
            "VY120",
            "--report-json",
            str(report),
        ]
    )

    assert code in {0, 2}
    data = json.loads(report.read_text())
    assert len(data["files"]) == 1
    assert data["files"][0]["changed"] is False
    assert not (tmp_path / "Token.vyi").exists()
    assert not any(fix["rule"] == "VY120" for fix in data["files"][0]["fixes"])


def test_pyproject_config_paths(tmp_path: Path, monkeypatch, passing_compiler) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )
    report = tmp_path / "configured-report.json"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f"""[tool.vyupgrade]
paths = ["{contract}"]
report-json = "{report}"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    code = main(["--check"])

    assert code == 1
    assert report.exists()


def test_pyproject_can_waive_unvalidated_source(
    tmp_path: Path, monkeypatch, passing_compiler
) -> None:
    contract = tmp_path / "migration_03.vy"
    contract.write_text(
        "# @version 0.3.10\n@external\ndef __init__():\n    pass\n", encoding="utf-8"
    )
    report = tmp_path / "configured-report.json"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f'''[tool.vyupgrade]
paths = ["{contract}"]
report-json = "{report}"
allow-unvalidated-source = true
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        engine,
        "compile_source_file",
        lambda *_args: CompileResult("failed", stderr="source failed"),
    )

    code = main(["--config", str(pyproject), "--write"])

    assert code == 0
    assert "#pragma version 0.4.3" in contract.read_text(encoding="utf-8")
    assert json.loads(report.read_text())["validation_decision"]["status"] == "waived"


def test_select_limits_applied_rules(tmp_path: Path, passing_compiler) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text(
        """# @version 0.3.10
@external
def __init__():
    pass
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--select", "VY001", "--report-json", str(report)])

    assert code in {1, 2, 3}
    fixes = json.loads(report.read_text())["files"][0]["fixes"]
    assert {fix["rule"] for fix in fixes} == {"VY001"}


def test_diff_output_is_colored_for_tty(monkeypatch) -> None:
    stream = TtyStringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)

    _write_diff(
        [
            "--- old.vy\n",
            "+++ new.vy\n",
            "@@ -1 +1 @@\n",
            "-# @version 0.3.10\n",
            "+#pragma version 0.4.3\n",
            " unchanged\n",
        ],
        stream,
    )

    text = stream.getvalue()
    assert "\x1b[1m--- old.vy\n\x1b[0m" in text
    assert "\x1b[36m@@ -1 +1 @@\n\x1b[0m" in text
    assert "\x1b[31m-# @version 0.3.10\n\x1b[0m" in text
    assert "\x1b[32m+#pragma version 0.4.3\n\x1b[0m" in text
    assert " unchanged\n" in text


def test_diff_output_stays_plain_for_pipes() -> None:
    stream = StringIO()

    _write_diff(["-old\n", "+new\n"], stream)

    assert stream.getvalue() == "-old\n+new\n"


def test_diff_output_respects_no_color(monkeypatch) -> None:
    stream = TtyStringIO()
    monkeypatch.setenv("NO_COLOR", "1")

    _write_diff(["-old\n", "+new\n"], stream)

    assert stream.getvalue() == "-old\n+new\n"


def test_evm_default_diagnostic_reports_exact_change() -> None:
    diagnostic = _evm_default_diagnostic("0.3.7", "0.4.3")

    assert diagnostic is not None
    assert diagnostic.rule == "VYD009"
    assert diagnostic.message == (
        "default EVM version changed from paris (source compiler 0.3.7) "
        "to prague (target compiler 0.4.3); review or pin explicitly"
    )


def test_evm_default_diagnostic_tracks_patch_level_default_changes() -> None:
    diagnostic = _evm_default_diagnostic("0.4.2", "0.4.3")

    assert diagnostic is not None
    assert "cancun (source compiler 0.4.2) to prague (target compiler 0.4.3)" in diagnostic.message
    assert _evm_default_diagnostic("0.4.0", "0.4.2") is None


def test_validation_diagnostics_respect_rule_selection(tmp_path: Path) -> None:
    report = FileReport(path=tmp_path / "Contract.vy")
    report.source_compile = "failed"
    report.abi_equal = False
    report.storage_layout_equal = False
    config = Config(paths=(report.path,), select=frozenset({"VYD009"}))

    _add_validation_diagnostics(report, "0.3.7", config)

    assert [diagnostic.rule for diagnostic in report.diagnostics] == ["VYD009"]


def test_validation_diagnostics_respect_rule_ignore(tmp_path: Path) -> None:
    report = FileReport(path=tmp_path / "Contract.vy")
    config = Config(paths=(report.path,), ignore=frozenset({"VYD009"}))

    _add_validation_diagnostics(report, "0.3.7", config)

    assert not report.diagnostics


def test_validation_diagnostics_use_resolved_source_compiler_for_evm_default(
    tmp_path: Path,
) -> None:
    report = FileReport(path=tmp_path / "Contract.vy")
    config = Config(paths=(report.path,), target_version="0.4.3")

    _add_validation_diagnostics(report, ">0.3.10", config, source_compiler="0.4.3")

    assert not [diagnostic for diagnostic in report.diagnostics if diagnostic.rule == "VYD009"]


def test_source_newer_than_target_skips_compile_and_reports_error(tmp_path: Path) -> None:
    contract = tmp_path / "newer.vy"
    contract.write_text(
        """# pragma version >=0.5.0a1,<0.6.0

@external
def f() -> uint256:
    return 1
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code == 5
    data = json.loads(report.read_text())
    file_report = data["files"][0]
    assert file_report["changed"] is False
    assert file_report["diagnostics"][0]["rule"] == "VYD016"
    assert file_report["diagnostics"][0]["severity"] == "error"
    assert file_report["validation"]["source_compile"] == "skipped"
    assert file_report["validation"]["target_compile"] == "skipped"
