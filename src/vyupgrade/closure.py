from __future__ import annotations

import os
import stat

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from . import compiler
from .models import Config
from .write_plan import read_regular_file_or_missing


@dataclass(frozen=True)
class ClosureWriteResult:
    status: str
    root: Path
    files: tuple[Path, ...]
    error: str | None = None


@dataclass(frozen=True)
class RetainedClosureInputs:
    root: Path
    replaced: frozenset[Path]
    files: tuple[tuple[Path, bytes], ...]

    def verify(self, output: Path) -> None:
        root = _closure_output_root(output)
        if root != self.root or _retained_inputs(root, self.replaced) != self.files:
            raise compiler.OverlayLayoutConflictError(
                f"retained closure output files changed after validation: {output}"
            )


def stage_retained_closure_inputs(output: Path, stage: Path) -> RetainedClosureInputs:
    """Mirror the existing files that will remain visible to the compiler after export."""
    root = _closure_output_root(output)
    replaced = frozenset(path.relative_to(stage) for path in stage.rglob("*") if path.is_file())
    files = _retained_inputs(root, replaced)
    try:
        for relative, content in files:
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    except OSError as exc:
        raise compiler.OverlayLayoutConflictError(
            f"could not stage retained closure output files from {root}: {exc}"
        ) from exc
    return RetainedClosureInputs(root, replaced, files)


def _closure_output_root(output: Path) -> Path:
    try:
        return output.resolve()
    except (OSError, RuntimeError) as exc:
        raise compiler.OverlayLayoutConflictError(
            f"could not resolve closure output directory {output}: {exc}"
        ) from exc


def _retained_inputs(
    root: Path, replaced: frozenset[Path],
) -> tuple[tuple[Path, bytes], ...]:
    def raise_walk_error(exc: OSError) -> None:
        raise exc

    files = []
    try:
        if not root.is_dir():
            return ()
        for directory, subdirectories, names in os.walk(root, onerror=raise_walk_error):
            current = Path(directory)
            for name in subdirectories:
                if (current / name).is_symlink():
                    raise OSError(f"cannot isolate a retained directory symlink: {current / name}")
            for name in names:
                path = current / name
                relative = path.relative_to(root)
                if relative in replaced or (
                    path.suffix not in compiler.VALIDATION_SOURCE_SUFFIXES
                    and name not in {"pyproject.toml", "uv.lock", "uv.toml"}
                ):
                    continue
                content = read_regular_file_or_missing(path)
                if content is not None:
                    files.append((relative, content))
    except OSError as exc:
        raise compiler.OverlayLayoutConflictError(
            f"could not capture retained closure output files in {root}: {exc}"
        ) from exc
    return tuple(sorted(files))


def write_closure_output(
    output_root: Path,
    sources: Mapping[Path, str],
    target_version: str,
    search_paths: tuple[Path, ...] = (),
    *,
    snapshot: compiler.ImportClosure | None = None,
    retained: RetainedClosureInputs | None = None,
) -> ClosureWriteResult:
    resolved = output_root
    try:
        resolved = output_root.resolve()
        if resolved.exists() and not resolved.is_dir():
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                "closure output destination is not a directory",
            )
        if not sources:
            return ClosureWriteResult("written", resolved, ())
        snapshot = snapshot or compiler.resolve_import_closure(sources, search_paths)
        members = snapshot.files
        if any(member.is_relative_to(resolved) for member in members):
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                "refusing to write the closure into a directory that contains migration sources",
            )
        relative_files = closure_relative_files(sources, target_version, search_paths, snapshot)
        linked = _linked_output_path(resolved, relative_files)
        if linked is not None:
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                f"refusing to write the closure through linked output path: {linked}",
            )
        if retained is not None:
            if frozenset(relative_files) != retained.replaced:
                raise compiler.OverlayLayoutConflictError(
                    f"closure output file plan changed after validation: {resolved}"
                )
            retained.verify(output_root)
        resolved.mkdir(parents=True, exist_ok=True)
        overlay = compiler.materialize_target_overlay(
            sources,
            target_version,
            resolved,
            search_paths,
            include_dependencies=True,
            snapshot=snapshot,
        )
        assert overlay is not None
        if retained is not None:
            retained.verify(output_root)
        return ClosureWriteResult("written", resolved, tuple(sorted(set(overlay.paths.values()))))
    except (compiler.OverlayLayoutConflictError, OSError) as exc:
        return ClosureWriteResult("failed", resolved, (), str(exc))


def write_closure_archive(
    output: Path,
    entry: Path,
    sources: Mapping[Path, str],
    config: Config,
    *, snapshot: compiler.ImportClosure | None = None,
) -> ClosureWriteResult:
    resolved = output
    try:
        resolved = output.resolve()
        if entry not in sources:
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                f"archive entry is missing from closure sources: {entry}",
            )
        snapshot = snapshot or compiler.resolve_import_closure(sources, config.compiler_search_paths)
        members = snapshot.files
        if resolved in members:
            return ClosureWriteResult(
                "failed",
                resolved,
                (),
                f"refusing to overwrite closure source with archive: {resolved}",
            )
        with compiler.target_overlay(
            sources,
            config.target_version,
            config.compiler_search_paths,
            include_dependencies=True,
            snapshot=snapshot,
        ) as overlay:
            assert overlay is not None
            result = compiler.compile_target_archive(
                entry, sources[entry], config, overlay, resolved
            )
        if result.status == "passed":
            return ClosureWriteResult("written", resolved, (resolved,))
        return ClosureWriteResult("failed", resolved, (), result.stderr)
    except (compiler.OverlayLayoutConflictError, OSError) as exc:
        return ClosureWriteResult("failed", resolved, (), str(exc))


def closure_relative_files(
    sources: Mapping[Path, str],
    target_version: str,
    search_paths: tuple[Path, ...],
    snapshot: compiler.ImportClosure,
) -> tuple[Path, ...]:
    """List the files staged by closure export, including copied project configuration."""
    with TemporaryDirectory(prefix="vyupgrade-closure-") as tmp:
        root = Path(tmp)
        overlay = compiler.materialize_target_overlay(
            sources,
            target_version,
            root,
            search_paths,
            include_dependencies=True,
            snapshot=snapshot,
        )
        assert overlay is not None
        return tuple(path.relative_to(root) for path in root.rglob("*") if path.is_file())


def _linked_output_path(root: Path, relative_files: tuple[Path, ...]) -> Path | None:
    for relative in relative_files:
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if os.path.islink(current):
                return current
        destination = root / relative
        if os.path.islink(destination):
            return destination
        if destination.exists():
            destination_stat = os.lstat(destination)
            if stat.S_ISREG(destination_stat.st_mode) and destination_stat.st_nlink > 1:
                return destination
    return None
