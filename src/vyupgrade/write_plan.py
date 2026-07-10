from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .models import FileReport


class PlanConflictError(ValueError):
    """Raised when two migration outputs cannot safely share a destination."""


class WriteTransactionError(OSError):
    """Raised when a planned write cannot be committed or fully rolled back."""

    def __init__(
        self,
        message: str,
        *,
        rollback_incomplete: bool = False,
        affected_paths: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(message)
        self.rollback_incomplete = rollback_incomplete
        self.affected_paths = affected_paths


@dataclass
class PlannedWrite:
    path: Path
    original: bytes | None
    candidate: bytes
    mode: int | None
    generated: bool
    reports: list[FileReport] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.candidate


class MigrationPlan:
    """A collision-checked, content-addressed set of migration destinations."""

    def __init__(self) -> None:
        self._entries: dict[Path, PlannedWrite] = {}
        self._generated_destinations: set[Path] = set()

    @property
    def entries(self) -> tuple[PlannedWrite, ...]:
        return tuple(self._entries.values())

    @property
    def writes(self) -> tuple[PlannedWrite, ...]:
        return tuple(entry for entry in self._entries.values() if entry.changed)

    def add_source(
        self,
        path: Path,
        original: str,
        candidate: str,
        report: FileReport,
    ) -> None:
        destination = path.resolve()
        if destination in self._entries:
            raise PlanConflictError(f"duplicate migration destination: {destination}")
        try:
            original_bytes, source_stat = _snapshot_existing(destination)
        except OSError as exc:
            raise PlanConflictError(
                f"could not snapshot migration source {destination}: {exc}"
            ) from exc
        expected = original.encode("utf-8")
        if original_bytes != expected:
            raise PlanConflictError(
                f"migration source changed while planning: {destination}"
            )
        candidate_bytes = candidate.encode("utf-8")
        if original_bytes != candidate_bytes:
            _require_replaceable_regular_file(destination, source_stat)
        entry = PlannedWrite(
            destination,
            original_bytes,
            candidate_bytes,
            stat.S_IMODE(source_stat.st_mode),
            False,
            [report],
        )
        self._entries[destination] = entry
        self._refresh_entry_reports(entry)

    def add_generated(self, path: Path, candidate: str, report: FileReport) -> None:
        try:
            lexical_stat = path.lstat()
        except FileNotFoundError:
            lexical_stat = None
        except OSError as exc:
            raise PlanConflictError(
                f"could not inspect generated destination {path}: {exc}"
            ) from exc
        if lexical_stat is not None and stat.S_ISLNK(lexical_stat.st_mode):
            raise PlanConflictError(
                f"generated destination may not be a symbolic link: {path}"
            )

        destination = path.resolve()
        candidate_bytes = candidate.encode("utf-8")
        if destination in self._generated_destinations:
            raise PlanConflictError(f"duplicate generated destination: {destination}")
        self._generated_destinations.add(destination)

        existing_entry = self._entries.get(destination)
        if existing_entry is not None:
            if existing_entry.changed or existing_entry.original != candidate_bytes:
                raise PlanConflictError(
                    f"generated destination collides with a migration source: {destination}"
                )
            existing_entry.reports.append(report)
            self._refresh_entry_reports(existing_entry)
            return

        try:
            original, destination_stat = _snapshot_existing(destination)
        except FileNotFoundError:
            original = None
            destination_stat = None
        except OSError as exc:
            raise PlanConflictError(
                f"could not inspect generated destination {destination}: {exc}"
            ) from exc
        if original is not None:
            assert destination_stat is not None
            if original != candidate_bytes:
                raise PlanConflictError(
                    "generated destination already exists with different content: "
                    f"{destination}"
                )
            mode = stat.S_IMODE(destination_stat.st_mode)
        else:
            mode = None

        entry = PlannedWrite(
            destination,
            original,
            candidate_bytes,
            mode,
            True,
            [report],
        )
        self._entries[destination] = entry
        self._refresh_entry_reports(entry)

    def candidate_source(self, path: Path, fallback: str) -> str:
        entry = self._entries.get(path.resolve())
        if entry is None:
            return fallback
        return entry.candidate.decode("utf-8")

    def update_candidates(self, candidates: dict[Path, bytes]) -> None:
        for path, candidate in candidates.items():
            entry = self._entries[path.resolve()]
            candidate.decode("utf-8")
            entry.candidate = candidate
            self._refresh_entry_reports(entry)

    def diff_chunks(self) -> list[str]:
        chunks: list[str] = []
        for entry in self.writes:
            previous = b"" if entry.original is None else entry.original
            chunks.extend(
                difflib.unified_diff(
                    previous.decode("utf-8").splitlines(keepends=True),
                    entry.candidate.decode("utf-8").splitlines(keepends=True),
                    fromfile=str(entry.path),
                    tofile=str(entry.path),
                )
            )
        return chunks

    def commit(self) -> bool:
        entries = self.entries
        self._assert_destinations_unchanged(entries)
        writes = self.writes
        if not writes:
            return False

        staged: dict[Path, Path] = {}
        try:
            for entry in writes:
                staged[entry.path] = _stage_replacement(entry)
        except OSError as exc:
            _cleanup_staged(staged.values())
            raise WriteTransactionError(f"could not stage migration writes: {exc}") from exc

        committed: list[PlannedWrite] = []
        try:
            self._assert_destinations_unchanged(entries)
            for entry in writes:
                self._assert_destinations_unchanged((entry,))
                os.replace(staged[entry.path], entry.path)
                committed.append(entry)
        except OSError as exc:
            rollback_errors = _rollback(committed)
            _cleanup_staged(staged.values())
            if rollback_errors:
                self.refresh_final_state()
                affected = self.paths_differing_from_original()
                detail = (
                    f"migration write failed and rollback was incomplete: {exc}; "
                    "rollback errors: " + "; ".join(rollback_errors)
                )
                raise WriteTransactionError(
                    detail,
                    rollback_incomplete=True,
                    affected_paths=tuple(affected),
                ) from exc
            raise WriteTransactionError(
                f"migration write failed; all committed destinations were restored: {exc}"
            ) from exc

        _cleanup_staged(staged.values())
        return True

    def _assert_destinations_unchanged(
        self, writes: tuple[PlannedWrite, ...]
    ) -> None:
        for entry in writes:
            try:
                current = _read_regular_file_or_missing(entry.path)
            except OSError as exc:
                raise WriteTransactionError(
                    f"could not recheck migration destination {entry.path}: {exc}"
                ) from exc
            if current != entry.original:
                raise WriteTransactionError(
                    f"migration destination changed before commit: {entry.path}"
                )

    def refresh_final_state(self) -> tuple[bool, list[Path]]:
        """Refresh on-disk hashes and return whether anything differs from the plan."""
        differs_from_original = False
        differs_from_candidate: list[Path] = []
        for entry in self.entries:
            try:
                current = _read_regular_file_or_missing(entry.path)
                readable = True
            except OSError:
                current = None
                readable = False
            final_sha256 = _sha256(current) if current is not None else None
            final_matches_candidate = readable and current == entry.candidate
            differs_from_original = (
                differs_from_original or not readable or current != entry.original
            )
            if not final_matches_candidate:
                differs_from_candidate.append(entry.path)
            for report in entry.reports:
                report.final_sha256 = final_sha256
                report.final_matches_candidate = final_matches_candidate
        return differs_from_original, differs_from_candidate

    def paths_differing_from_original(self) -> list[Path]:
        affected: list[Path] = []
        for entry in self.entries:
            try:
                current = _read_regular_file_or_missing(entry.path)
            except OSError:
                affected.append(entry.path)
                continue
            if current != entry.original:
                affected.append(entry.path)
        return affected

    @staticmethod
    def _refresh_entry_reports(entry: PlannedWrite) -> None:
        original_sha256 = (
            _sha256(entry.original) if entry.original is not None else None
        )
        candidate_sha256 = _sha256(entry.candidate)
        for report in entry.reports:
            report.changed = entry.changed
            report.original_sha256 = original_sha256
            report.candidate_sha256 = candidate_sha256


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stage_replacement(entry: PlannedWrite) -> Path:
    staged = entry.path.with_name(
        f".{entry.path.name}.vyupgrade-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        if entry.mode is None:
            descriptor = os.open(
                staged,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
        else:
            shutil.copy2(entry.path, staged)
            descriptor = os.open(staged, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(entry.candidate)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _rollback(committed: list[PlannedWrite]) -> list[str]:
    errors: list[str] = []
    for entry in reversed(committed):
        try:
            if entry.original is None:
                entry.path.unlink(missing_ok=True)
                continue
            rollback_entry = PlannedWrite(
                entry.path,
                entry.candidate,
                entry.original,
                entry.mode,
                entry.generated,
            )
            staged = _stage_replacement(rollback_entry)
            try:
                os.replace(staged, entry.path)
            finally:
                staged.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{entry.path}: {exc}")
    return errors


def _cleanup_staged(paths: Iterable[Path]) -> None:
    for path in paths:
        with suppress(OSError):
            Path(path).unlink(missing_ok=True)


def _snapshot_existing(path: Path) -> tuple[bytes, os.stat_result]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise OSError("destination is a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise OSError("destination is not a regular file")
    content = path.read_bytes()
    after = path.lstat()
    if _stat_identity(before) != _stat_identity(after):
        raise OSError("destination changed while it was being read")
    return content, after


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _require_replaceable_regular_file(path: Path, value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise PlanConflictError(f"migration destination is not a regular file: {path}")
    if value.st_nlink != 1:
        raise PlanConflictError(
            f"migration destination has {value.st_nlink} hard links and cannot be replaced safely: {path}"
        )
    if stat.S_IMODE(value.st_mode) & 0o222 == 0:
        raise PlanConflictError(f"migration destination is read-only: {path}")


def _read_regular_file_or_missing(path: Path) -> bytes | None:
    try:
        content, _value = _snapshot_existing(path)
    except FileNotFoundError:
        return None
    return content
