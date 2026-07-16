from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from uv import find_uv_bin

from .models import Config
from .source import code_mask
from .storage_layout import compare_storage_layouts, parse_storage_layout
from .versions import (
    VyperVersion,
    compiler_version_for_spec,
    infer_pragma,
    legacy_prerelease_version,
    parse_version,
)


FORMATS = ("abi", "method_identifiers", "layout")
SOURCE_FORMATS = ("abi", "method_identifiers", "layout", "ast")
TARGET_FORMATS = (*FORMATS, "ast")
COMPILE_TIMEOUT_SECONDS = 120
COMMON_IMPORT_DEPENDENCIES = {
    "snekmate": "snekmate",
}
OVERLAY_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "venv",
}
VALIDATION_SOURCE_SUFFIXES = {".vy", ".vyi", ".json"}
VALIDATION_MODULE_ALIASES = {
    "create2": "create2_address",
}

class OverlayLayoutConflictError(ValueError):
    """Two closure members with different content map to one overlay path."""


@dataclass
class CompileResult:
    status: str
    artifacts: dict[str, object] | None = None
    stderr: str | None = None
    command: list[str] | None = None
    unavailable_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetOverlay:
    root: Path
    paths: Mapping[Path, Path]
    source_roots: tuple[Path, ...]
    search_paths: tuple[Path, ...]

@dataclass(frozen=True)
class ImportClosure:
    roots: tuple[Path, ...]
    dependencies: tuple[Path, ...]
    source_roots: tuple[Path, ...]
    common_root: Path

    @property
    def files(self) -> tuple[Path, ...]:
        return self.roots + self.dependencies


@dataclass(frozen=True)
class _ResolvedValidationImport:
    path: Path
    root: Path


def unavailable_validation_artifacts(result: CompileResult) -> list[str]:
    artifacts = result.artifacts or {}
    validators: dict[str, Callable[[object], bool]] = {
        "abi": _valid_abi_artifact,
        "method_identifiers": _valid_method_identifiers_artifact,
        "layout": _valid_layout_artifact,
    }
    return [
        name
        for name, validator in validators.items()
        if not validator(artifacts.get(name))
    ]


def _valid_abi_artifact(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(_valid_abi_entry(entry) for entry in value)


def _valid_abi_entry(value: object) -> bool:
    if not isinstance(value, dict) or not _nonempty_string(value.get("type")):
        return False
    if "name" in value and not isinstance(value["name"], str):
        return False
    for field in ("inputs", "outputs"):
        if field in value and not _valid_abi_parameters(value[field]):
            return False
    return True


def _valid_abi_parameters(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for parameter in value:
        if not isinstance(parameter, dict) or not _nonempty_string(parameter.get("type")):
            return False
        if "name" in parameter and not isinstance(parameter["name"], str):
            return False
        if "components" in parameter and not _valid_abi_parameters(parameter["components"]):
            return False
    return True


def _valid_method_identifiers_artifact(value: object) -> bool:
    return isinstance(value, dict) and all(
        _nonempty_string(signature) and _nonempty_string(selector)
        for signature, selector in value.items()
    )


def _valid_layout_artifact(value: object) -> bool:
    return parse_storage_layout(value) is not None


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def compile_source_file(path: Path, config: Config, source_version: str | None) -> CompileResult:
    if path.suffix != ".vy":
        return CompileResult("skipped")
    command, suppress_warnings = _prepare_command(
        config.source_vyper,
        source_version or infer_pragma(path.read_text()),
        config.source_python,
    )
    result = _run_compile_with_formats(
        command,
        path,
        config,
        SOURCE_FORMATS,
        (),
        suppress_warnings,
        allow_unsupported_formats=True,
    )
    if _should_retry_source_with_final_newline(path, result):
        return _compile_source_file_with_final_newline(
            command, path, config, suppress_warnings, result
        )
    return result


def _compile_source_file_with_final_newline(
    command: list[str],
    path: Path,
    config: Config,
    suppress_warnings: bool,
    original_result: CompileResult,
) -> CompileResult:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return CompileResult("failed", stderr="could not read source for final-newline retry")
    try:
        tmp = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f".{path.stem}.vyupgrade.source.",
            suffix=path.suffix,
            dir=path.parent,
            delete=False,
        )
    except OSError:
        return original_result
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            tmp.write(source)
            tmp.write("\n")
        return _run_compile_with_formats(
            command,
            tmp_path,
            config,
            SOURCE_FORMATS,
            (),
            suppress_warnings,
            allow_unsupported_formats=True,
        )
    finally:
        with suppress(OSError):
            tmp_path.unlink()


def _should_retry_source_with_final_newline(path: Path, result: CompileResult) -> bool:
    if result.status != "failed" or result.stderr is None:
        return False
    if not _legacy_span_error(result.stderr):
        return False
    try:
        return not path.read_bytes().endswith(b"\n")
    except OSError:
        return False


def compile_target_source(
    path: Path, source: str, config: Config, overlay: TargetOverlay | None = None
) -> CompileResult:
    if path.suffix not in {".vy", ".vyi"}:
        return CompileResult("skipped")
    if overlay is not None:
        tmp_path = overlay.paths.get(path.resolve())
        if tmp_path is not None:
            command, suppress_warnings = _prepare_command(
                config.target_vyper, config.target_version, config.target_python
            )
            compile_config = _target_compile_config(source, config)
            compile_config = replace(
                compile_config,
                compiler_search_paths=_overlay_search_paths(
                    overlay, compile_config.compiler_search_paths
                ),
            )
            if path.suffix == ".vyi":
                return _compile_target_interface(
                    command,
                    tmp_path,
                    source,
                    compile_config,
                    (),
                    suppress_warnings,
                )
            return _run_compile(
                command,
                tmp_path,
                compile_config,
                extra_paths=(),
                suppress_warnings=suppress_warnings,
            )
    command, suppress_warnings = _prepare_command(
        config.target_vyper, config.target_version, config.target_python
    )
    compile_config = _target_compile_config(source, config)
    if path.suffix == ".vyi":
        return _compile_target_interface(
            command,
            path,
            source,
            compile_config,
            (path.parent,),
            suppress_warnings,
        )
    try:
        tmp = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f".{path.stem}.vyupgrade.",
            suffix=".vy",
            dir=path.parent,
            delete=False,
        )
    except OSError:
        tmp = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f".{path.stem}.vyupgrade.",
            suffix=".vy",
            delete=False,
        )
    with tmp:
        tmp.write(source)
        tmp_path = Path(tmp.name)
    try:
        return _run_compile(
            command,
            tmp_path,
            compile_config,
            extra_paths=(path.parent,),
            suppress_warnings=suppress_warnings,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def _compile_target_interface(
    command: list[str],
    path: Path,
    source: str,
    config: Config,
    extra_paths: tuple[Path, ...],
    suppress_warnings: bool,
) -> CompileResult:
    try:
        interface_file = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="vyupgrade_interface_",
            suffix=".vyi",
            dir=path.parent,
            delete=False,
        )
    except OSError as exc:
        return CompileResult(
            "failed", stderr=f"could not stage interface for target validation: {exc}"
        )
    interface_path = Path(interface_file.name)
    harness_path: Path | None = None
    try:
        with interface_file:
            interface_file.write(source)
        harness_file = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="vyupgrade_interface_harness_",
            suffix=".vy",
            dir=path.parent,
            delete=False,
        )
        harness_path = Path(harness_file.name)
        with harness_file:
            harness_file.write(
                f"#pragma version {config.target_version}\n"
                f"import {interface_path.stem} as InterfaceUnderTest\n"
            )
        interface_command = _with_project_import_dependencies(command, interface_path)
        return _run_compile(
            interface_command,
            harness_path,
            config,
            extra_paths=tuple(dict.fromkeys((path.parent, *extra_paths))),
            suppress_warnings=suppress_warnings,
        )
    except OSError as exc:
        return CompileResult(
            "failed", stderr=f"could not create interface validation harness: {exc}"
        )
    finally:
        interface_path.unlink(missing_ok=True)
        if harness_path is not None:
            harness_path.unlink(missing_ok=True)


def resolve_import_closure(
    sources: Mapping[Path, str],
    search_paths: tuple[Path, ...] = (),
) -> ImportClosure:
    """Resolve the full transitive closure, including external search-path files.

    Default overlay materialization copies only closure members under
    ``common_root``; closure mode materializes external dependencies too.
    """
    resolved_sources = {path.resolve(): source for path, source in sources.items()}
    if not resolved_sources:
        raise ValueError("resolve_import_closure requires at least one source")
    source_roots, import_roots, common_root = _overlay_roots(
        resolved_sources, search_paths
    )
    dependencies = [
        path
        for path, _source, _import_root, is_override in _walk_validation_closure(
            source_roots, import_roots, common_root, resolved_sources
        )
        if not is_override
    ]
    return ImportClosure(
        roots=tuple(sorted(resolved_sources)),
        dependencies=tuple(sorted(dict.fromkeys(dependencies))),
        source_roots=source_roots,
        common_root=common_root,
    )


@contextmanager
def target_overlay(
    sources: Mapping[Path, str],
    target_version: str,
    search_paths: tuple[Path, ...] = (),
    *,
    include_dependencies: bool = False,
) -> Iterator[TargetOverlay | None]:
    resolved_sources = {path.resolve(): source for path, source in sources.items()}
    if not resolved_sources:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="vyupgrade-target-") as tmp:
        overlay = materialize_target_overlay(
            resolved_sources,
            target_version,
            Path(tmp),
            search_paths,
            include_dependencies=include_dependencies,
        )
        assert overlay is not None
        yield overlay


def materialize_target_overlay(
    sources: Mapping[Path, str],
    target_version: str,
    root: Path,
    search_paths: tuple[Path, ...] = (),
    *,
    include_dependencies: bool = False,
) -> TargetOverlay | None:
    resolved_sources = {path.resolve(): source for path, source in sources.items()}
    if not resolved_sources:
        return None
    roots, import_roots, common = _overlay_roots(resolved_sources, search_paths)
    if include_dependencies:
        paths, overlay_search_paths = _materialize_closure_sources(
            roots,
            import_roots,
            common,
            root,
            target_version,
            resolved_sources,
        )
    else:
        destination = _overlay_destination_resolver(common, root)
        paths: dict[Path, Path] = {}
        overlay_search_paths: set[Path] = set()
        for source_root in roots:
            copied_search_paths, _copied = _copy_validation_sources(
                source_root,
                import_roots,
                common,
                root,
                target_version,
                resolved_sources,
                destination,
                None,
            )
            overlay_search_paths.update(copied_search_paths)
        overlay_search_paths.update(
            _overlay_configured_search_paths(search_paths, common, root)
        )
        for path, source in resolved_sources.items():
            target = destination(path)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            paths[path] = target
            overlay_search_paths.add(target.parent)
    if include_dependencies:
        configured_search_paths = tuple(
            search_path.resolve() for search_path in search_paths
        )
        for source_root in roots:
            if source_root not in configured_search_paths:
                _copy_project_configs(
                    source_root,
                    root,
                    excluded_roots=configured_search_paths,
                )
    else:
        _copy_project_configs(common, root)
    return TargetOverlay(
        root=root,
        paths=paths,
        source_roots=import_roots if include_dependencies else roots,
        search_paths=tuple(
            sorted(
                (path for path in overlay_search_paths if path != root),
                key=lambda path: str(path),
            )
        ),
    )


def _materialize_closure_sources(
    source_roots: tuple[Path, ...],
    import_roots: tuple[Path, ...],
    common_root: Path,
    target_root: Path,
    target_version: str,
    overrides: Mapping[Path, str],
) -> tuple[dict[Path, Path], set[Path]]:
    placed: dict[Path, Path] = {}
    paths: dict[Path, Path] = {}
    search_paths: set[Path] = set()
    for path, source, import_root, is_override in _walk_validation_closure(
        source_roots, import_roots, common_root, overrides
    ):
        target = target_root / path.relative_to(import_root)
        if is_override:
            if _claim_overlay_destination(placed, target, path, source):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
            search_paths.add(target.parent)
            if path.name == "create2_address.vy":
                alias = target.with_name("create2.vy")
                alias_source = _target_validation_create2_alias_source(source)
                if _claim_overlay_destination(placed, alias, path, alias_source):
                    alias.write_text(alias_source, encoding="utf-8")
        else:
            _copy_validation_source(
                path,
                source,
                target,
                common_root,
                target_version,
                search_paths,
                placed,
            )
        paths[path] = target
    return paths, search_paths


def _overlay_roots(
    resolved_sources: Mapping[Path, str],
    search_paths: tuple[Path, ...],
) -> tuple[tuple[Path, ...], tuple[Path, ...], Path]:
    roots = tuple(
        dict.fromkeys(
            root
            for path in resolved_sources
            for root in _validation_roots(path, search_paths)
        )
    )
    import_roots = tuple(
        dict.fromkeys((*roots, *(search_path.resolve() for search_path in search_paths)))
    )
    common = Path(os.path.commonpath([str(root) for root in roots]))
    return roots, import_roots, common


def _overlay_destination_resolver(
    common_root: Path,
    target_root: Path,
) -> Callable[[Path], Path | None]:
    def destination(path: Path) -> Path | None:
        try:
            return target_root / path.relative_to(common_root)
        except ValueError:
            return None

    return destination


def _claim_overlay_destination(
    placed: dict[Path, Path],
    target: Path,
    source_path: Path,
    content: str,
) -> bool:
    existing_source = placed.get(target)
    if existing_source is None:
        placed[target] = source_path
        return True
    if existing_source == source_path:
        return True
    if target.read_bytes() == content.encode("utf-8"):
        return False
    raise OverlayLayoutConflictError(
        f"overlay destination {target} maps both {existing_source} and {source_path}"
    )

def _overlay_configured_search_paths(
    search_paths: tuple[Path, ...], common_root: Path, target_root: Path
) -> set[Path]:
    paths: set[Path] = set()
    for search_path in search_paths:
        try:
            relative = search_path.resolve().relative_to(common_root)
        except ValueError:
            continue
        target = target_root / relative
        if target.exists():
            paths.add(target)
    return paths


def _overlay_search_paths(
    overlay: TargetOverlay, search_paths: tuple[Path, ...]
) -> tuple[Path, ...]:
    covered = tuple(path.resolve() for path in overlay.source_roots)
    return (
        overlay.root,
        *overlay.search_paths,
        *(
            search_path
            for search_path in search_paths
            if not any(_paths_overlap(search_path.resolve(), root) for root in covered)
        ),
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _validation_roots(path: Path, search_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    resolved = path.resolve()
    candidates: list[Path] = []
    for search_path in search_paths:
        root = search_path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        candidates.append(root)
    if candidates:
        return tuple(sorted(candidates, key=lambda candidate: len(candidate.parts)))
    return (_nearest_project_root(path.parent) or path.parent,)


def _walk_validation_closure(
    source_roots: tuple[Path, ...],
    import_roots: tuple[Path, ...],
    common_root: Path,
    overrides: Mapping[Path, str],
) -> Iterator[tuple[Path, str, Path, bool]]:
    override_paths = set(overrides)
    incoming_overrides: set[Path] = set()
    for path, source in overrides.items():
        import_source = _standard_json_package_dependency_source(
            path, source, common_root
        )
        for source_root in _containing_import_roots(path, source_roots):
            incoming_overrides.update(
                imported.path
                for imported in _validation_import_sources(
                    path, import_source, source_root, import_roots
                )
                if imported.path in override_paths
            )

    entry_paths = override_paths - incoming_overrides
    queue = [
        (
            path,
            overrides[path],
            _containing_import_roots(path, source_roots)[0],
            True,
        )
        for path in sorted(entry_paths)
    ]
    layouts: dict[Path, Path] = {}
    processed: set[Path] = set()
    while queue or override_paths - processed:
        if not queue:
            path = min(override_paths - processed)
            queue.append(
                (
                    path,
                    overrides[path],
                    _containing_import_roots(path, source_roots)[0],
                    True,
                )
            )
        current, current_source, current_root, is_override = queue.pop(0)
        existing_root = layouts.get(current)
        if existing_root is not None and existing_root != current_root:
            raise OverlayLayoutConflictError(
                f"closure member {current} resolves relative to both "
                f"{existing_root} and {current_root}"
            )
        layouts[current] = current_root
        if current in processed:
            continue
        processed.add(current)
        yield current, current_source, current_root, is_override
        import_source = _standard_json_package_dependency_source(
            current, current_source, common_root
        )
        for imported in _validation_import_sources(
            current, import_source, current_root, import_roots
        ):
            if imported.path in processed:
                existing_root = layouts[imported.path]
                if existing_root != imported.root:
                    raise OverlayLayoutConflictError(
                        f"closure member {imported.path} resolves relative to both "
                        f"{existing_root} and {imported.root}"
                    )
                continue
            source = overrides.get(imported.path)
            if source is None:
                try:
                    source = imported.path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
            queue.append(
                (imported.path, source, imported.root, imported.path in override_paths)
            )


def _containing_import_roots(
    path: Path, import_roots: tuple[Path, ...]
) -> tuple[Path, ...]:
    roots = tuple(root for root in import_roots if _is_relative_to(path, root))
    assert roots, f"closure member is outside import roots: {path}"
    return tuple(sorted(roots, key=lambda root: len(root.parts)))


def _walk_validation_sources(
    source_root: Path,
    import_roots: tuple[Path, ...],
    common_root: Path,
    overrides: Mapping[Path, str],
) -> Iterator[tuple[Path, str]]:
    override_paths = set(overrides)
    queue: list[tuple[Path, str]] = []
    for path, source in overrides.items():
        try:
            path.relative_to(source_root)
        except ValueError:
            continue
        queue.append((path, source))

    processed: set[Path] = set()
    while queue:
        current, current_source = queue.pop()
        if current in processed:
            continue
        processed.add(current)
        current_source = _standard_json_package_dependency_source(
            current, current_source, common_root
        )
        for imported in _validation_import_sources(
            current, current_source, source_root, import_roots
        ):
            resolved = imported.path
            if resolved in processed:
                continue
            source = overrides.get(resolved)
            if source is None:
                try:
                    source = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
            queue.append((resolved, source))
            if resolved in override_paths:
                continue
            yield resolved, source


def _copy_validation_sources(
    source_root: Path,
    import_roots: tuple[Path, ...],
    common_root: Path,
    target_root: Path,
    target_version: str,
    overrides: Mapping[Path, str],
    destination: Callable[[Path], Path | None],
    placed: dict[Path, Path] | None,
) -> tuple[set[Path], dict[Path, Path]]:
    search_paths: set[Path] = set()
    copied: dict[Path, Path] = {}
    for resolved, source in _walk_validation_sources(
        source_root, import_roots, common_root, overrides
    ):
        target = destination(resolved)
        if target is None:
            continue
        _copy_validation_source(
            resolved,
            source,
            target,
            common_root,
            target_version,
            search_paths,
            placed,
        )
        if resolved.suffix in VALIDATION_SOURCE_SUFFIXES:
            copied[resolved] = target
    return search_paths, copied


def _copy_validation_source(
    source_path: Path,
    source: str,
    target: Path,
    common_root: Path,
    target_version: str,
    search_paths: set[Path],
    placed: dict[Path, Path] | None,
) -> None:
    if source_path.suffix not in VALIDATION_SOURCE_SUFFIXES:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_path.suffix == ".json":
        if placed is None or _claim_overlay_destination(
            placed, target, source_path, source
        ):
            target.write_text(source, encoding="utf-8")
        search_paths.add(target.parent)
        return
    source = _standard_json_package_dependency_source(
        source_path, source, common_root
    )
    content = _target_validation_source(
        source,
        target_version,
        is_interface=source_path.suffix == ".vyi",
    )
    if placed is None or _claim_overlay_destination(
        placed, target, source_path, content
    ):
        target.write_text(content, encoding="utf-8")
    search_paths.add(target.parent)
    if source_path.name == "create2_address.vy":
        alias = target.with_name("create2.vy")
        alias_content = _target_validation_source(
            _target_validation_create2_alias_source(source),
            target_version,
            is_interface=False,
        )
        if placed is None or _claim_overlay_destination(
            placed, alias, source_path, alias_content
        ):
            alias.write_text(alias_content, encoding="utf-8")


def _standard_json_package_dependency_source(source_path: Path, source: str, common_root: Path) -> str:
    source = _local_sibling_import_source(source_path, source)
    if source_path.parent.name != "src":
        return source
    replacements = {
        name
        for name in ("auth", "utils")
        if (common_root / name).exists() and not (source_path.parent / name).exists()
    }
    if not replacements:
        return source
    names = "|".join(sorted(re.escape(name) for name in replacements))
    return re.sub(
        rf"(^[ \t]*from[ \t]+)\.({names})(?=\b)",
        r"\1..\2",
        source,
        flags=re.MULTILINE,
    )


def _local_sibling_import_source(source_path: Path, source: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        module = match.group("module")
        if not (source_path.parent / f"{module}.vy").exists():
            return match.group(0)
        alias = match.group("alias") or ""
        return f"{match.group('indent')}from . import {module}{alias}{match.group('trailing')}"

    return re.sub(
        r"^(?P<indent>[ \t]*)import[ \t]+(?P<module>[A-Za-z_][A-Za-z0-9_]*)(?P<alias>[ \t]+as[ \t]+[A-Za-z_][A-Za-z0-9_]*)?(?P<trailing>[ \t]*(?:#.*)?)$",
        replacement,
        source,
        flags=re.MULTILINE,
    )


def _validation_import_sources(
    path: Path, source: str, source_root: Path, import_roots: tuple[Path, ...]
) -> tuple[_ResolvedValidationImport, ...]:
    imports: list[_ResolvedValidationImport] = []
    mask = code_mask(source)
    code_source = "".join(
        char if is_code or char in "\r\n" else " "
        for char, is_code in zip(source, mask, strict=True)
    )
    for line in code_source.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        import_match = re.match(
            r"import\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b",
            stripped,
        )
        if import_match:
            imports.extend(
                _resolve_validation_import(
                    path, source_root, import_match.group(1), (), import_roots
                )
            )
            continue
        from_match = re.match(
            r"from\s+([.A-Za-z_][.A-Za-z0-9_]*)\s+import\s+(.+)$",
            stripped,
        )
        if from_match:
            names = tuple(_imported_module_names(from_match.group(2)))
            imports.extend(
                _resolve_validation_import(
                    path, source_root, from_match.group(1), names, import_roots
                )
            )
    return tuple(dict.fromkeys(imports))


def _imported_module_names(imports: str) -> Iterator[str]:
    for part in imports.strip().removeprefix("(").removesuffix(")").split(","):
        name = part.split("#", 1)[0].strip()
        if not name or name == "*":
            continue
        name = name.split()[0]
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*$", name):
            yield name


def _resolve_validation_import(
    path: Path,
    source_root: Path,
    module: str,
    names: tuple[str, ...],
    import_roots: tuple[Path, ...],
) -> tuple[_ResolvedValidationImport, ...]:
    bases, module_parts = _validation_import_bases(path, source_root, module, import_roots)
    candidates: list[tuple[Path, Path]] = []
    for base, import_root in bases:
        module_path = base.joinpath(*module_parts) if module_parts else base
        if names:
            for name in names:
                candidates.extend(
                    (candidate, import_root)
                    for candidate in _validation_module_candidates(module_path / name)
                )
            candidates.extend(
                (candidate, import_root)
                for candidate in _validation_module_candidates(module_path)
            )
        else:
            candidates.extend(
                (candidate, import_root)
                for candidate in _validation_module_candidates(module_path)
            )
    resolved: dict[Path, Path] = {}
    roots = tuple(root.resolve() for root in (source_root, *import_roots))
    for candidate, import_root in candidates:
        try:
            resolved_candidate = candidate.resolve()
            if not any(_is_relative_to(resolved_candidate, root) for root in roots):
                continue
        except (OSError, ValueError):
            continue
        if resolved_candidate.exists() and resolved_candidate.suffix in VALIDATION_SOURCE_SUFFIXES:
            resolved.setdefault(resolved_candidate, import_root)
    return tuple(
        _ResolvedValidationImport(path, import_root)
        for path, import_root in resolved.items()
    )


def _validation_import_bases(
    path: Path, source_root: Path, module: str, import_roots: tuple[Path, ...]
) -> tuple[tuple[tuple[Path, Path], ...], tuple[str, ...]]:
    if not module.startswith("."):
        bases = (
            (path.parent, source_root),
            (source_root, source_root),
            *((root, root) for root in import_roots),
        )
        unique_bases: dict[Path, Path] = {}
        for base, root in bases:
            unique_bases.setdefault(base, root)
        return tuple(unique_bases.items()), tuple(
            part for part in module.split(".") if part
        )
    level = len(module) - len(module.lstrip("."))
    base = path.parent
    for _ in range(max(level - 1, 0)):
        base = base.parent
    return ((base, source_root),), tuple(
        part for part in module[level:].split(".") if part
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validation_module_candidates(path: Path) -> tuple[Path, ...]:
    paths = [path]
    alias = VALIDATION_MODULE_ALIASES.get(path.name)
    if alias is not None:
        paths.append(path.with_name(alias))
    return tuple(candidate.with_suffix(suffix) for candidate in paths for suffix in sorted(VALIDATION_SOURCE_SUFFIXES))


def _project_config_paths(
    source_root: Path,
    excluded_roots: tuple[Path, ...],
) -> Iterator[Path]:
    if not excluded_roots:
        yield from source_root.rglob("pyproject.toml")
        return
    for directory, directories, files in os.walk(source_root):
        current = Path(directory)
        directories[:] = [
            name
            for name in directories
            if not any(
                _is_relative_to((current / name).resolve(), excluded_root)
                for excluded_root in excluded_roots
            )
        ]
        if "pyproject.toml" in files:
            yield current / "pyproject.toml"


def _copy_project_configs(
    source_root: Path,
    target_root: Path,
    *,
    excluded_roots: tuple[Path, ...] = (),
) -> None:
    resolved_target = target_root.resolve()
    for pyproject in _project_config_paths(source_root, excluded_roots):
        resolved_pyproject = pyproject.resolve()
        if (
            resolved_pyproject == resolved_target
            or resolved_target in resolved_pyproject.parents
            or any(
                _is_relative_to(resolved_pyproject, excluded_root)
                for excluded_root in excluded_roots
            )
        ):
            continue
        if any(
            part in {".git", ".venv", "venv", "node_modules"}
            for part in pyproject.parts
        ):
            continue
        try:
            relative = pyproject.relative_to(source_root)
        except ValueError:
            continue
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pyproject, target)


def compile_source_ast(path: Path, config: Config, source_version: str | None) -> CompileResult:
    if path.suffix != ".vy":
        return CompileResult("skipped")
    command, suppress_warnings = _prepare_command(
        config.source_vyper,
        source_version or infer_pragma(path.read_text()),
        config.source_python,
    )
    return _run_compile_with_formats(
        command,
        path,
        config,
        ("ast",),
        (),
        suppress_warnings,
        allow_unsupported_formats=True,
    )


def compare_artifacts(
    source: CompileResult, target: CompileResult
) -> tuple[bool | None, bool | None, bool | None]:
    if source.artifacts is None or target.artifacts is None:
        return None, None, None
    source_layout = parse_storage_layout(source.artifacts.get("layout"))
    target_layout = parse_storage_layout(target.artifacts.get("layout"))
    source_abi = source.artifacts.get("abi")
    target_abi = target.artifacts.get("abi")
    source_methods = source.artifacts.get("method_identifiers")
    target_methods = target.artifacts.get("method_identifiers")
    storage_comparison = (
        None
        if source_layout is None or target_layout is None
        else compare_storage_layouts(
            source_layout,
            target_layout,
            target_ast=target.artifacts.get("ast"),
        )
    )
    return (
        None
        if source_abi is None or target_abi is None
        else _canonical_abi(source_abi) == _canonical_abi(target_abi),
        None
        if source_methods is None or target_methods is None
        else _canonical_method_identifiers(source_methods)
        == _canonical_method_identifiers(target_methods),
        None if storage_comparison is None else storage_comparison.equal,
    )


def compare_artifact_details(
    source: CompileResult,
    target: CompileResult,
) -> tuple[list[str], list[str], list[str]]:
    if source.artifacts is None or target.artifacts is None:
        return [], [], []
    source_abi = source.artifacts.get("abi")
    target_abi = target.artifacts.get("abi")
    source_methods = source.artifacts.get("method_identifiers")
    target_methods = target.artifacts.get("method_identifiers")
    source_layout = parse_storage_layout(source.artifacts.get("layout"))
    target_layout = parse_storage_layout(target.artifacts.get("layout"))
    storage_comparison = (
        None
        if source_layout is None or target_layout is None
        else compare_storage_layouts(
            source_layout,
            target_layout,
            target_ast=target.artifacts.get("ast"),
        )
    )
    return (
        _abi_diff(source_abi, target_abi),
        _method_identifier_diff(source_methods, target_methods),
        [] if storage_comparison is None else list(storage_comparison.differences),
    )


def _target_validation_source(
    source: str, target_version: str, *, is_interface: bool = False
) -> str:
    pattern = re.compile(r"^(\s*)#\s*(?:@version|pragma\s+version)\s+(.+?)\s*$", re.MULTILINE)
    replaced = False

    def replacement(match: re.Match[str]) -> str:
        nonlocal replaced
        if replaced:
            return ""
        replaced = True
        return f"{match.group(1)}#pragma version {target_version}"

    rewritten = pattern.sub(replacement, source)
    rewritten = re.sub(
        r"^[ \t]*#[ \t]*pragma[ \t]+solidity\b.*(?:\n|$)",
        "",
        rewritten,
        flags=re.MULTILINE,
    )
    rewritten = _target_validation_dependency_source(rewritten)
    rewritten = _strip_target_validation_docstrings(rewritten)
    if is_interface:
        return _target_validation_interface_source(rewritten)
    return rewritten


def _strip_target_validation_docstrings(source: str) -> str:
    lines = source.splitlines(keepends=True)
    result: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        quote = _standalone_docstring_quote(stripped)
        if quote is None:
            result.append(line)
            index += 1
            continue
        if not _target_validation_docstring_context(lines, index):
            result.append(line)
            index += 1
            continue
        index += 1
        if stripped.count(quote) >= 2:
            continue
        while index < len(lines):
            current = lines[index]
            index += 1
            if quote in current:
                break
    return "".join(result)


def _standalone_docstring_quote(stripped_line: str) -> str | None:
    for quote in ('"""', "'''"):
        if stripped_line.startswith(quote):
            return quote
    return None


def _target_validation_docstring_context(lines: list[str], index: int) -> bool:
    indent = len(lines[index]) - len(lines[index].lstrip(" \t"))
    previous = _previous_significant_line(lines, index)
    if previous is None:
        return indent == 0
    previous_index, previous_line = previous
    previous_stripped = previous_line.strip()
    previous_indent = len(previous_line) - len(previous_line.lstrip(" \t"))
    if indent == 0:
        return previous_stripped.startswith("#pragma version")
    if previous_indent >= indent or not previous_stripped.endswith(":"):
        return False
    if previous_stripped.startswith("def "):
        return True
    return _previous_function_header(lines, previous_index, previous_indent) is not None


def _previous_significant_line(lines: list[str], index: int) -> tuple[int, str] | None:
    cursor = index - 1
    while cursor >= 0:
        stripped = lines[cursor].strip()
        if stripped and not stripped.startswith("#"):
            return cursor, lines[cursor]
        cursor -= 1
    return None


def _previous_function_header(lines: list[str], index: int, indent: int) -> int | None:
    cursor = index
    while cursor >= 0:
        line = lines[cursor]
        stripped = line.strip()
        current_indent = len(line) - len(line.lstrip(" \t"))
        if current_indent == indent and stripped.startswith("def "):
            return cursor
        if current_indent < indent and stripped.startswith("def "):
            return cursor
        if current_indent < indent:
            return None
        cursor -= 1
    return None


def _target_validation_dependency_source(source: str) -> str:
    source = re.sub(
        r"(^[ \t]*from[ \t]+snekmate\.utils[ \t]+import[ \t]+.*?)\bcreate2_address\b",
        r"\1create2",
        source,
        flags=re.MULTILINE,
    )
    source = re.sub(
        r"(?<![\w.])create2(?:_address)?\._compute_address\b",
        "create2._compute_create2_address",
        source,
    )
    return source


def _target_validation_create2_alias_source(source: str) -> str:
    if "_compute_create2_address" in source or "_compute_address" not in source:
        return source
    return re.sub(r"\b_compute_address\b", "_compute_create2_address", source)


def _target_validation_interface_source(source: str) -> str:
    lines = source.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not re.match(r"def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped):
            output.append(line)
            index += 1
            continue

        def_indent = len(line) - len(line.lstrip(" \t"))
        header_lines = [line]
        index += 1
        while index < len(lines) and not _interface_header_complete("".join(header_lines)):
            header_lines.append(lines[index])
            index += 1
        output.extend(_interface_header_stub_lines(header_lines))
        while index < len(lines):
            next_line = lines[index]
            if not next_line.strip():
                index += 1
                continue
            next_indent = len(next_line) - len(next_line.lstrip(" \t"))
            if next_indent <= def_indent:
                break
            index += 1
    return "".join(output)


def _interface_header_complete(header: str) -> bool:
    depth = 0
    for char in header:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
    return depth == 0 and bool(re.search(r":[ \t]*(?:\w+\s*)?(?:#.*)?$", header.rstrip()))


def _interface_header_stub_lines(header_lines: list[str]) -> list[str]:
    if not header_lines:
        return []
    output = list(header_lines)
    last = output[-1].rstrip("\n")
    output[-1] = re.sub(
        r":[ \t]*(?:view|pure|payable|nonpayable)?([ \t]*(?:#.*)?)$",
        r": ...\1",
        last,
    ) + ("\n" if output[-1].endswith("\n") else "")
    return output


def _canonical_abi(abi: object) -> object:
    if not isinstance(abi, list):
        return abi
    entries = [_strip_abi_metadata(entry) for entry in abi if isinstance(entry, dict)]
    return sorted(entries, key=lambda entry: json.dumps(_abi_sort_key(entry), sort_keys=True))


def _strip_abi_metadata(value: object) -> object:
    if isinstance(value, dict):
        stripped = {
            key: _canonical_abi_value(key, item, value)
            for key, item in sorted(value.items())
            if key not in {"gas", "internalType", "unit"}
            and not (key == "name" and not _is_abi_entry(value))
            and not (value.get("type") == "constructor" and key == "stateMutability" and item == "nonpayable")
            and key != "constant"
            and key != "payable"
            and not (value.get("type") == "constructor" and key == "outputs")
            and not (key == "components" and _is_tuple_abi_type(value.get("type")))
        }
        if _is_abi_entry(value) and "stateMutability" not in stripped:
            mutability = _legacy_abi_mutability(value)
            if mutability is not None:
                stripped["stateMutability"] = mutability
        return stripped
    if isinstance(value, list):
        return [_strip_abi_metadata(item) for item in value]
    return value


def _canonical_abi_value(key: str, value: object, parent: Mapping[str, object]) -> object:
    if key == "stateMutability" and value == "pure":
        return "view"
    if key == "outputs":
        return _canonical_abi_outputs(value)
    if key == "type" and isinstance(value, str):
        return _canonical_abi_type(value, parent.get("components"))
    return _strip_abi_metadata(value)


def _is_abi_entry(value: Mapping[str, object]) -> bool:
    return value.get("type") in {"function", "event", "error"}


def _legacy_abi_mutability(value: Mapping[str, object]) -> str | None:
    if value.get("payable") is True:
        return "payable"
    if value.get("constant") is True:
        return "view"
    if value.get("type") in {"function", "constructor"} and (
        value.get("payable") is False or value.get("constant") is False
    ):
        return "nonpayable"
    return None


def _canonical_abi_outputs(value: object) -> object:
    if (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], dict)
        and _is_tuple_abi_type(value[0].get("type"))
        and isinstance(value[0].get("components"), list)
    ):
        return _strip_abi_metadata(value[0]["components"])
    return _strip_abi_metadata(value)


def _canonical_abi_type(type_name: str, components: object = None) -> str:
    fixed_match = re.fullmatch(r"u?fixed(?P<bits>\d+)x(?P<scale>\d+)", type_name)
    if fixed_match is not None:
        prefix = "uint" if type_name.startswith("ufixed") else "int"
        return f"{prefix}{fixed_match.group('bits')}"
    if _is_tuple_abi_type(type_name):
        suffix = type_name.removeprefix("tuple")
        if isinstance(components, list):
            inner = ",".join(
                _canonical_abi_type(str(component.get("type", "?")), component.get("components"))
                if isinstance(component, dict)
                else "?"
                for component in components
            )
            return f"({inner}){suffix}"
    return type_name


def _is_tuple_abi_type(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"tuple(?:\[[0-9]*\])*", value) is not None


def _abi_sort_key(entry: dict[str, object]) -> tuple[object, ...]:
    inputs = entry.get("inputs")
    input_types: tuple[object, ...] = ()
    if isinstance(inputs, list):
        input_types = tuple(item.get("type") if isinstance(item, dict) else None for item in inputs)
    return (entry.get("type"), entry.get("name", ""), input_types, entry.get("stateMutability", ""))


def _canonical_method_identifiers(methods: object) -> object:
    if not isinstance(methods, dict):
        return methods
    return {key: value for key, value in sorted(methods.items()) if not key.startswith("__init__(")}


def _abi_diff(source: object, target: object) -> list[str]:
    if source is None or target is None:
        return []
    source_entries = _abi_entry_map(_canonical_abi(source))
    target_entries = _abi_entry_map(_canonical_abi(target))
    return _mapping_diff_lines(
        source_entries,
        target_entries,
        removed=lambda key, value: f"removed ABI entry: {key}",
        added=lambda key, value: f"added ABI entry: {key}",
        changed=_changed_abi_lines,
    )


def _abi_entry_diff(source: object, target: object) -> list[str]:
    if not isinstance(source, dict) or not isinstance(target, dict):
        return []
    details: list[str] = []
    for field, label in (
        ("inputs", "inputs"),
        ("outputs", "outputs"),
    ):
        if source.get(field) != target.get(field):
            details.append(
                f"{label} {_format_abi_params(source.get(field))} -> {_format_abi_params(target.get(field))}"
            )
    for field in ("stateMutability", "anonymous"):
        if source.get(field) != target.get(field):
            details.append(f"{field} {source.get(field)!r} -> {target.get(field)!r}")
    if details:
        return details
    return [f"{json.dumps(source, sort_keys=True)} -> {json.dumps(target, sort_keys=True)}"]


def _format_abi_params(value: object) -> str:
    if not isinstance(value, list):
        return str(value)
    if not value:
        return "()"
    return "(" + ", ".join(_format_abi_param(item) for item in value) + ")"


def _format_abi_param(value: object) -> str:
    if not isinstance(value, dict):
        return str(value)
    type_name = str(value.get("type", "?"))
    if type_name == "tuple":
        type_name = f"tuple{_format_abi_params(value.get('components'))}"
    name = value.get("name")
    return f"{name}: {type_name}" if isinstance(name, str) and name else type_name


def _method_identifier_diff(source: object, target: object) -> list[str]:
    if source is None or target is None:
        return []
    source_methods = _canonical_method_identifiers(source)
    target_methods = _canonical_method_identifiers(target)
    if not isinstance(source_methods, dict) or not isinstance(target_methods, dict):
        return []
    return _mapping_diff_lines(
        source_methods,
        target_methods,
        removed=lambda key, value: f"removed selector: {key} = {value}",
        added=lambda key, value: f"added selector: {key} = {value}",
        changed=lambda key, before, after: [f"changed selector: {key} {before} -> {after}"],
    )


def _mapping_diff_lines(
    source: Mapping[str, object],
    target: Mapping[str, object],
    *,
    removed: Callable[[str, object], str],
    added: Callable[[str, object], str],
    changed: Callable[[str, object, object], list[str]],
    skip_removed: set[str] | None = None,
    skip_added: set[str] | None = None,
    equivalent: Callable[[str, object, object], bool] | None = None,
) -> list[str]:
    skip_removed = skip_removed or set()
    skip_added = skip_added or set()
    lines = [
        removed(key, source[key]) for key in sorted((source.keys() - target.keys()) - skip_removed)
    ]
    lines.extend(
        added(key, target[key]) for key in sorted((target.keys() - source.keys()) - skip_added)
    )
    for key in sorted(source.keys() & target.keys()):
        if not (
            equivalent(key, source[key], target[key])
            if equivalent
            else source[key] == target[key]
        ):
            lines.extend(changed(key, source[key], target[key]))
    return lines


def _changed_abi_lines(key: str, source: object, target: object) -> list[str]:
    details = _abi_entry_diff(source, target)
    if not details:
        return [f"changed ABI entry: {key}"]
    return [f"changed ABI entry: {key}: {detail}" for detail in details]


def _abi_entry_map(abi: object) -> dict[str, object]:
    if not isinstance(abi, list):
        return {}
    return {_abi_entry_key(entry): entry for entry in abi if isinstance(entry, dict)}


def _abi_entry_key(entry: dict[str, object]) -> str:
    entry_type = str(entry.get("type", "unknown"))
    if entry_type in {"function", "event", "error"}:
        return f"{entry_type} {entry.get('name', '')}({_abi_input_types(entry)})"
    return entry_type


def _abi_input_types(entry: dict[str, object]) -> str:
    inputs = entry.get("inputs")
    if not isinstance(inputs, list):
        return ""
    return ", ".join(
        str(item.get("type", "?")) if isinstance(item, dict) else "?" for item in inputs
    )


def _prepare_command(
    explicit: str | None, version: str | None, python: str | None
) -> tuple[list[str], bool]:
    normalized = _normalize_version(version)
    return _compiler_command(explicit, normalized, python), _supports_warning_policy(normalized)


def _compiler_command(explicit: str | None, version: str | None, python: str | None) -> list[str]:
    if explicit:
        return [explicit]
    normalized = _normalize_version(version) or "0.4.3"
    python = python or _default_python(normalized)
    command = [
        _uv_bin(),
        "run",
        "--no-project",
        "--python",
        python,
        "--with",
        f"vyper=={normalized}",
    ]
    if legacy_prerelease_version(normalized) is not None:
        return [
            *command,
            "--with",
            "typed-ast",
            "python",
            str(Path(__file__).with_name("legacy_vyper.py")),
        ]
    return [*command, "vyper"]


@cache
def _uv_bin() -> str:
    try:
        return find_uv_bin()
    except TypeError as exc:
        if "NoneType" not in str(exc):
            raise
        uv = shutil.which("uv")
        if uv is None:
            raise exc
        return uv


def _normalize_version(version: str | None) -> str | None:
    return compiler_version_for_spec(version)


def _default_python(version: str) -> str:
    if legacy_prerelease_version(version) is not None:
        return "3.8"
    parsed = parse_version(version)
    if parsed is not None and parsed < VyperVersion("0.3.1"):
        return "3.8"
    # Modern Vyper releases run cleanly on Python 3.11. Pinning the subprocess
    # interpreter avoids accidentally using a bleeding-edge project venv, where
    # old compiler dependencies can break.
    return "3.11"


def _supports_warning_policy(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= VyperVersion("0.4.1")


def _target_compile_config(source: str, config: Config) -> Config:
    if config.enable_decimals or not _uses_decimal(source):
        return config
    return replace(config, enable_decimals=True)


def _uses_decimal(source: str) -> bool:
    if re.search(r"\bdecimal\b", source):
        return True
    return bool(re.search(r"^\s*(?:from\s+math\s+import\b|import\s+math(?:\s|,|$))", source, re.MULTILINE))


def _run_compile(
    command: list[str],
    path: Path,
    config: Config,
    extra_paths: tuple[Path, ...] = (),
    suppress_warnings: bool = False,
) -> CompileResult:
    return _run_compile_with_formats(
        command,
        path,
        config,
        TARGET_FORMATS,
        extra_paths,
        suppress_warnings,
        optional_formats=frozenset({"ast"}),
    )


def _run_compile_with_formats(
    command: list[str],
    path: Path,
    config: Config,
    formats: tuple[str, ...],
    extra_paths: tuple[Path, ...],
    suppress_warnings: bool,
    *,
    allow_unsupported_formats: bool = False,
    unavailable_formats: tuple[str, ...] = (),
    optional_formats: frozenset[str] = frozenset(),
) -> CompileResult:
    full = [*_with_project_import_dependencies(command, path), "-f", ",".join(formats)]
    if _supports_search_paths(command):
        for search_path in config.compiler_search_paths:
            full.extend(["-p", str(search_path)])
        project_root = _nearest_project_root(path.parent)
        if project_root is not None:
            full.extend(["-p", str(project_root)])
        for search_path in extra_paths:
            full.extend(["-p", str(search_path)])
        full.extend(["-p", str(path.parent)])
    if config.enable_decimals:
        full.append("--enable-decimals")
    if suppress_warnings:
        full.extend(["-W", "none"])
    full.append(str(path))
    try:
        proc = subprocess.run(full, capture_output=True, text=True, timeout=COMPILE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return CompileResult(
            "failed",
            stderr=f"compiler timed out after {COMPILE_TIMEOUT_SECONDS} seconds",
            command=full,
        )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        unsupported = _unsupported_output_format(stderr, formats)
        if unsupported is not None:
            unavailable = tuple(dict.fromkeys((*unavailable_formats, unsupported)))
            if not allow_unsupported_formats and unsupported not in optional_formats:
                return CompileResult(
                    "failed",
                    stderr=f"target compiler did not support required output format '{unsupported}': {stderr}",
                    command=full,
                    unavailable_formats=unavailable,
                )
            fallback_formats = tuple(name for name in formats if name != unsupported)
            if not fallback_formats:
                return CompileResult(
                    "failed",
                    stderr="source compiler did not support any requested output formats",
                    command=full,
                    unavailable_formats=unavailable,
                )
            return _run_compile_with_formats(
                command,
                path,
                config,
                fallback_formats,
                extra_paths,
                suppress_warnings,
                allow_unsupported_formats=allow_unsupported_formats,
                unavailable_formats=unavailable,
                optional_formats=optional_formats,
            )
        span_error_format = _legacy_span_error_format(stderr, formats)
        if (
            span_error_format is not None
            and not allow_unsupported_formats
            and span_error_format not in optional_formats
        ):
            span_error_format = None
        if span_error_format is not None:
            unavailable = tuple(
                dict.fromkeys((*unavailable_formats, span_error_format))
            )
            fallback_formats = tuple(name for name in formats if name != span_error_format)
            if not fallback_formats:
                return CompileResult(
                    "failed",
                    stderr="source compiler could not produce any requested output formats",
                    command=full,
                    unavailable_formats=unavailable,
                )
            return _run_compile_with_formats(
                command,
                path,
                config,
                fallback_formats,
                extra_paths,
                suppress_warnings,
                allow_unsupported_formats=allow_unsupported_formats,
                unavailable_formats=unavailable,
                optional_formats=optional_formats,
            )
        retry_command = _command_with_missing_module_dependency(command, path, stderr)
        if retry_command is not None:
            return _run_compile_with_formats(
                retry_command,
                path,
                config,
                formats,
                extra_paths,
                suppress_warnings,
                allow_unsupported_formats=allow_unsupported_formats,
                unavailable_formats=unavailable_formats,
                optional_formats=optional_formats,
            )
        return CompileResult(
            "failed",
            stderr=proc.stderr.strip() or proc.stdout.strip(),
            command=full,
            unavailable_formats=unavailable_formats,
        )
    try:
        artifacts = _parse_outputs(proc.stdout, formats)
    except ValueError as exc:
        return CompileResult(
            "failed",
            stderr=f"could not parse compiler output: {exc}",
            command=full,
            unavailable_formats=unavailable_formats,
        )
    return CompileResult(
        "degraded"
        if any(name not in optional_formats for name in unavailable_formats)
        else "passed",
        artifacts=artifacts,
        stderr=proc.stderr.strip() or None,
        command=full,
        unavailable_formats=unavailable_formats,
    )


def _unsupported_output_format(stderr: str, formats: tuple[str, ...]) -> str | None:
    for name in formats:
        if f"Unsupported format type '{name}'" in stderr or f"KeyError: '{name}'" in stderr:
            return name
    return None


def _legacy_span_error_format(stderr: str, formats: tuple[str, ...]) -> str | None:
    if not _legacy_span_error(stderr):
        return None
    for name in ("ast", "layout", "method_identifiers"):
        if name in formats:
            return name
    return None


def _legacy_span_error(stderr: str) -> bool:
    return "ValueError: start (" in stderr and "precedes previous end" in stderr


def _supports_search_paths(command: list[str]) -> bool:
    return legacy_prerelease_version(_command_vyper_version(command)) is None


def _command_vyper_version(command: list[str]) -> str | None:
    for index, arg in enumerate(command):
        if arg == "--with" and index + 1 < len(command):
            package = command[index + 1]
            if package.startswith("vyper=="):
                return package.removeprefix("vyper==")
    return None


def _with_project_import_dependencies(command: list[str], path: Path) -> list[str]:
    if not _is_uv_run_command(command):
        return command
    packages = _project_import_packages(path)
    if not packages:
        return command
    return _uv_command_with_packages(command, packages)


def _command_with_missing_module_dependency(
    command: list[str], path: Path, stderr: str
) -> list[str] | None:
    if not _is_uv_run_command(command):
        return None
    missing = _missing_module_name(stderr)
    if missing is None:
        return None
    pyproject = _nearest_pyproject(path.parent)
    dependencies = _pyproject_dependencies(pyproject) if pyproject is not None else {}
    root = missing.split(".", 1)[0]
    package = dependencies.get(root) or COMMON_IMPORT_DEPENDENCIES.get(root)
    if package is None:
        return None
    retry_command = _uv_command_with_packages(command, (package,))
    return retry_command if retry_command != command else None


def _missing_module_name(stderr: str) -> str | None:
    match = re.search(
        r"\bModuleNotFound:\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)", stderr
    )
    return match.group(1) if match else None


def _uv_command_with_packages(command: list[str], packages: tuple[str, ...]) -> list[str]:
    insert_at = _uv_run_command_index(command)
    full = command[:insert_at]
    for package in packages:
        if _uv_command_has_package(command, package):
            continue
        full.extend(["--with", package])
    full.extend(command[insert_at:])
    return full


def _uv_run_command_index(command: list[str]) -> int:
    index = 2
    options_with_value = {
        "--python",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--env-file",
    }
    while index < len(command):
        arg = command[index]
        if arg == "--":
            return index + 1
        if not arg.startswith("-"):
            return index
        index += 1
        if arg in options_with_value and index < len(command):
            index += 1
    return len(command)


def _uv_command_has_package(command: list[str], package: str) -> bool:
    return any(
        arg == "--with" and index + 1 < len(command) and command[index + 1] == package
        for index, arg in enumerate(command)
    )


def _is_uv_run_command(command: list[str]) -> bool:
    return len(command) >= 3 and Path(command[0]).name == "uv" and command[1] == "run"


def _project_import_packages(path: Path) -> tuple[str, ...]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    imports = _vyper_import_roots(source)
    if not imports:
        return ()
    pyproject = _nearest_pyproject(path.parent)
    dependencies = _pyproject_dependencies(pyproject) if pyproject is not None else {}
    return tuple(
        package
        for name in imports
        if (package := dependencies.get(name) or COMMON_IMPORT_DEPENDENCIES.get(name)) is not None
    )


def _vyper_import_roots(source: str) -> tuple[str, ...]:
    roots: set[str] = set()
    for line in source.splitlines():
        match = re.match(
            r"\s*from\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\b", line
        )
        if match is None:
            match = re.match(
                r"\s*import\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b", line
            )
        if match is None:
            continue
        root = match.group(1).split(".", 1)[0]
        if root != "vyper":
            roots.add(root)
    return tuple(sorted(roots))


def _nearest_project_root(start: Path) -> Path | None:
    pyproject = _nearest_pyproject(start)
    return pyproject.parent if pyproject is not None else None


def _nearest_pyproject(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        pyproject = directory / "pyproject.toml"
        if pyproject.exists():
            return pyproject
    return None


def _pyproject_dependencies(path: Path) -> dict[str, str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    dependencies: dict[str, str] = {}
    project = data.get("project")
    if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
        for dependency in project["dependencies"]:
            if isinstance(dependency, str):
                name = _dependency_name(dependency)
                if name is not None:
                    dependencies[name] = dependency
    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    poetry_dependencies = poetry.get("dependencies") if isinstance(poetry, dict) else None
    if isinstance(poetry_dependencies, dict):
        for name, value in poetry_dependencies.items():
            if name == "python":
                continue
            package = _poetry_dependency_package(name, value)
            if package is not None:
                dependencies[name.replace("-", "_")] = package
    return dependencies


def _dependency_name(dependency: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", dependency)
    return match.group(1).replace("-", "_") if match else None


def _poetry_dependency_package(name: str, value: object) -> str | None:
    normalized_name = name.replace("-", "_")
    if isinstance(value, str):
        return _package_with_version(name, value)
    if not isinstance(value, dict):
        return None
    package_name = str(value.get("package", name))
    if "git" in value:
        git = str(value["git"])
        rev = value.get("rev") or value.get("tag") or value.get("branch")
        suffix = f"@{rev}" if isinstance(rev, str) and rev else ""
        return f"{package_name} @ git+{git}{suffix}"
    version = value.get("version")
    if isinstance(version, str):
        return _package_with_version(package_name, version)
    return package_name or normalized_name


def _package_with_version(name: str, version: str) -> str | None:
    version = version.strip()
    if version in {"", "*"}:
        return name
    if version.startswith("^"):
        return None
    if version[0].isdigit():
        return f"{name}=={version}"
    if version.startswith(("==", "!=", "<=", ">=", "<", ">", "~=")):
        return f"{name}{version}"
    return None


def _parse_outputs(stdout: str, formats: tuple[str, ...] = FORMATS) -> dict[str, object]:
    chunks = [line for line in stdout.splitlines() if line.strip()]
    if len(chunks) != len(formats):
        raise ValueError(
            f"expected {len(formats)} compiler outputs, received {len(chunks)}"
        )
    artifacts: dict[str, object] = {}
    for name, raw in zip(formats, chunks, strict=True):
        artifacts[name] = json.loads(raw)
    return artifacts
