import stat
from pathlib import Path

import pytest

from vyupgrade import cli, write_plan
from vyupgrade.models import FileReport, RunReport
from vyupgrade.write_plan import MigrationPlan, PlanConflictError, WriteTransactionError


def test_partial_write_failure_rolls_back_every_destination(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    first.write_text("first original\n", encoding="utf-8")
    second.write_text("second original\n", encoding="utf-8")
    plan = MigrationPlan()
    plan.add_source(
        first,
        "first original\n",
        "first candidate\n",
        FileReport(first),
    )
    plan.add_source(
        second,
        "second original\n",
        "second candidate\n",
        FileReport(second),
    )
    real_replace = write_plan.os.replace
    failed = False

    def fail_second_once(source, destination):
        nonlocal failed
        if Path(destination) == second and not failed:
            failed = True
            raise OSError("injected second-write failure")
        real_replace(source, destination)

    monkeypatch.setattr(write_plan.os, "replace", fail_second_once)

    with pytest.raises(WriteTransactionError, match="destinations were restored"):
        plan.commit()

    assert first.read_text(encoding="utf-8") == "first original\n"
    assert second.read_text(encoding="utf-8") == "second original\n"
    assert not list(tmp_path.glob(".*.vyupgrade-*"))


def test_commit_refuses_to_overwrite_a_destination_changed_after_planning(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text("original\n", encoding="utf-8")
    plan = MigrationPlan()
    plan.add_source(contract, "original\n", "candidate\n", FileReport(contract))
    contract.write_text("concurrent edit\n", encoding="utf-8")

    with pytest.raises(WriteTransactionError, match="changed before commit"):
        plan.commit()

    assert contract.read_text(encoding="utf-8") == "concurrent edit\n"


def test_source_snapshot_stat_failure_becomes_a_reportable_plan_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    contract = tmp_path / "Contract.vy"
    contract.write_text("original\n", encoding="utf-8")
    destination = contract.resolve()
    real_lstat = Path.lstat

    def fail_destination_stat(path: Path, *args, **kwargs):
        if path == destination:
            raise OSError("injected stat failure")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail_destination_stat)

    with pytest.raises(PlanConflictError, match="could not snapshot migration source"):
        MigrationPlan().add_source(
            contract,
            "original\n",
            "candidate\n",
            FileReport(contract),
        )


def test_new_generated_file_uses_the_normal_creation_mode(tmp_path: Path) -> None:
    control = tmp_path / "control.vyi"
    control.write_text("control\n", encoding="utf-8")
    generated = tmp_path / "Generated.vyi"
    plan = MigrationPlan()
    plan.add_generated(
        generated,
        "generated\n",
        FileReport(generated),
    )

    assert plan.commit() is True

    assert stat.S_IMODE(generated.stat().st_mode) == stat.S_IMODE(control.stat().st_mode)


def test_generated_broken_symlink_is_rejected_without_creating_its_target(
    tmp_path: Path,
) -> None:
    escaped = tmp_path / "escaped.vyi"
    generated = tmp_path / "Generated.vyi"
    generated.symlink_to(escaped)

    with pytest.raises(PlanConflictError, match="may not be a symbolic link"):
        MigrationPlan().add_generated(
            generated,
            "generated\n",
            FileReport(generated),
        )

    assert generated.is_symlink()
    assert not escaped.exists()


def test_commit_rechecks_unchanged_planned_dependencies(tmp_path: Path) -> None:
    contract = tmp_path / "Contract.vy"
    dependency = tmp_path / "Dependency.vyi"
    contract.write_text("original\n", encoding="utf-8")
    dependency.write_text("dependency\n", encoding="utf-8")
    plan = MigrationPlan()
    plan.add_source(contract, "original\n", "candidate\n", FileReport(contract))
    plan.add_source(
        dependency,
        "dependency\n",
        "dependency\n",
        FileReport(dependency),
    )
    dependency.write_text("concurrent dependency edit\n", encoding="utf-8")

    with pytest.raises(WriteTransactionError, match="changed before commit"):
        plan.commit()

    assert contract.read_text(encoding="utf-8") == "original\n"
    assert dependency.read_text(encoding="utf-8") == "concurrent dependency edit\n"


def test_changed_hardlinked_source_is_rejected(tmp_path: Path) -> None:
    contract = tmp_path / "Contract.vy"
    alias = tmp_path / "Alias.vy"
    contract.write_text("original\n", encoding="utf-8")
    alias.hardlink_to(contract)

    with pytest.raises(PlanConflictError, match="hard links"):
        MigrationPlan().add_source(
            contract,
            "original\n",
            "candidate\n",
            FileReport(contract),
        )


def test_incomplete_rollback_is_explicit_and_refreshes_final_state(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "First.vy"
    second = tmp_path / "Second.vy"
    first.write_text("first original\n", encoding="utf-8")
    second.write_text("second original\n", encoding="utf-8")
    first_report = FileReport(first)
    plan = MigrationPlan()
    plan.add_source(first, "first original\n", "first candidate\n", first_report)
    plan.add_source(
        second,
        "second original\n",
        "second candidate\n",
        FileReport(second),
    )
    real_replace = write_plan.os.replace
    first_replaced = False

    def fail_commit_and_rollback(source, destination):
        nonlocal first_replaced
        destination = Path(destination)
        if destination == first and not first_replaced:
            first_replaced = True
            return real_replace(source, destination)
        if destination in {first, second}:
            raise OSError("injected replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(write_plan.os, "replace", fail_commit_and_rollback)

    with pytest.raises(WriteTransactionError, match="rollback was incomplete") as caught:
        plan.commit()

    assert caught.value.rollback_incomplete is True
    assert caught.value.affected_paths == (first,)
    assert first.read_text(encoding="utf-8") == "first candidate\n"
    assert first_report.final_matches_candidate is True


def test_formatter_staging_preserves_hierarchy_for_duplicate_basenames(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "one" / "Contract.vy"
    second = tmp_path / "two" / "Contract.vy"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    plan = MigrationPlan()
    plan.add_source(first, "first\n", "first candidate\n", FileReport(first))
    plan.add_source(second, "second\n", "second candidate\n", FileReport(second))
    report = RunReport(None, "0.4.3", [])

    def fake_run(command, **kwargs):
        staged = [Path(path) for path in command[1:]]
        assert [path.name for path in staged] == ["Contract.vy", "Contract.vy"]
        assert staged[0].parent != staged[1].parent
        assert all(path.parent.name in {"one", "two"} for path in staged)
        return cli.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli._run_mamushi(plan, report)

    assert report.formatter_status == "passed"
    assert first.read_text(encoding="utf-8") == "first\n"
    assert second.read_text(encoding="utf-8") == "second\n"
