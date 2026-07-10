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
from itertools import pairwise
from pathlib import Path

from uv import find_uv_bin

from .models import Config
from .versions import (
    VyperVersion,
    compiler_version_for_spec,
    infer_pragma,
    legacy_prerelease_version,
    parse_version,
)


FORMATS = ("abi", "method_identifiers", "layout")
SOURCE_FORMATS = ("abi", "method_identifiers", "layout", "ast")
COMPILE_TIMEOUT_SECONDS = 120
UINT256_MAX_DECIMAL = str(2**256 - 1)
UINT256_LIMIT = 2**256
LAYOUT_WRAPPER_KEYS = frozenset(
    {"storage_layout", "transient_storage_layout", "code_layout"}
)
STORAGE_LEAF_KEYS = frozenset({"slot", "type", "location", "n_slots"})
GENERATED_REENTRANCY_GAP_RE = re.compile(
    r"^_vyupgrade_reentrancy_lock_slot(?:_[1-9][0-9]*)?$"
)
STORAGE_PATH_ATOM_RE = re.compile(
    r"(?P<path>(?:[^,\[\]\n]+[/\\])+)(?P<base>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<extension>\.vyi?)?(?=[ \t]*(?:$|[,\]\)]))"
)
PLAIN_STORAGE_TYPE_FILE_RE = re.compile(
    r"(?P<base>[A-Za-z_][A-Za-z0-9_]*)(?P<extension>\.vyi?)"
    r"(?=[ \t]*(?:$|[,\]\)]))"
)
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
class _StorageLayoutEntry:
    name: str
    slot: int
    type_name: str
    location: str
    n_slots: int | None


_CanonicalStorageValue = tuple[int, str, int | None]


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
    return _canonical_storage_layouts(value) is not None


def _normalize_storage_slot(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < UINT256_LIMIT else None
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = int(value, 0)
    except ValueError:
        return None
    return normalized if 0 <= normalized < UINT256_LIMIT else None


def _normalize_storage_size(value: object) -> int | None:
    normalized = _normalize_storage_slot(value)
    return normalized if normalized is not None and normalized > 0 else None


def _classify_layout_artifact(
    value: object,
) -> tuple[tuple[_StorageLayoutEntry, ...], tuple[_StorageLayoutEntry, ...]] | None:
    """Return complete persistent/transient layouts, or fail closed.

    Vyper <=0.3 emitted a flat mapping while Vyper 0.4 module layouts are
    recursively namespaced below ``storage_layout``.  This classifier is shared
    by artifact validation and canonicalization so a node cannot be accepted by
    one path and silently discarded by the other.
    """
    if not isinstance(value, dict):
        return None
    legacy_wrapper_leaves = _legacy_wrapper_leaf_names(value)
    if legacy_wrapper_leaves is None:
        return None
    has_storage_wrapper = (
        "storage_layout" in value and "storage_layout" not in legacy_wrapper_leaves
    )
    has_transient_wrapper = (
        "transient_storage_layout" in value
        and "transient_storage_layout" not in legacy_wrapper_leaves
    )
    has_code_wrapper = "code_layout" in value and "code_layout" not in legacy_wrapper_leaves
    if has_storage_wrapper or has_transient_wrapper or has_code_wrapper:
        if legacy_wrapper_leaves or value.keys() - LAYOUT_WRAPPER_KEYS:
            return None
        storage = value.get("storage_layout", {})
        transient = value.get("transient_storage_layout", {})
        persistent_entries = _classify_storage_namespace(storage, location="storage")
        transient_entries = _classify_storage_namespace(transient, location="transient")
        code_entries = _classify_code_namespace(value.get("code_layout", {}))
        if (
            persistent_entries is None
            or transient_entries is None
            or code_entries is None
            or not _valid_layout_entry_sets(persistent_entries, transient_entries)
            or not _valid_code_entry_spans(code_entries)
        ):
            return None
        return persistent_entries, transient_entries

    entries = _classify_storage_namespace(value)
    if entries is None:
        return None
    persistent_entries = tuple(entry for entry in entries if entry.location == "storage")
    transient_entries = tuple(entry for entry in entries if entry.location == "transient")
    if not _valid_layout_entry_sets(persistent_entries, transient_entries):
        return None
    return persistent_entries, transient_entries


def _storage_leaf_node(value: object) -> bool:
    return isinstance(value, dict) and any(
        field in value and not isinstance(value[field], dict) for field in ("slot", "type")
    )


def _legacy_wrapper_leaf_names(value: dict[str, object]) -> set[str] | None:
    legacy_names: set[str] = set()
    for name in value.keys() & LAYOUT_WRAPPER_KEYS:
        node = value[name]
        if not _storage_leaf_node(node):
            continue
        if (
            not isinstance(node, dict)
            or node.get("location") not in {"storage", "transient"}
        ):
            return None
        legacy_names.add(name)
    return legacy_names


def _classify_storage_namespace(
    value: object,
    prefix: tuple[str, ...] = (),
    *,
    location: str | None = None,
) -> tuple[_StorageLayoutEntry, ...] | None:
    if not isinstance(value, dict):
        return None
    if prefix and not value:
        return None
    entries: list[_StorageLayoutEntry] = []
    for name, node in value.items():
        if not _nonempty_string(name) or not isinstance(node, dict):
            return None

        # A scalar slot or type marks a leaf.  Dict-valued keys named ``slot``
        # or ``type`` remain valid namespace/variable names.
        if _storage_leaf_node(node):
            if node.keys() - STORAGE_LEAF_KEYS:
                return None
            slot = _normalize_storage_slot(node.get("slot"))
            type_name = node.get("type")
            leaf_location = node.get("location", location or "storage")
            has_n_slots = "n_slots" in node
            raw_n_slots = node.get("n_slots")
            n_slots = _normalize_storage_size(raw_n_slots) if has_n_slots else None
            if (
                slot is None
                or not _nonempty_string(type_name)
                or leaf_location not in {"storage", "transient"}
                or (location is not None and leaf_location != location)
                or (has_n_slots and n_slots is None)
                or any(isinstance(field, (dict, list)) for field in node.values())
            ):
                return None
            entries.append(
                _StorageLayoutEntry(
                    ".".join((*prefix, name)),
                    slot,
                    type_name,
                    leaf_location,
                    n_slots,
                )
            )
            continue

        nested = _classify_storage_namespace(node, (*prefix, name), location=location)
        if nested is None:
            return None
        entries.extend(nested)
    if len(entries) != len({entry.name for entry in entries}):
        return None
    return tuple(entries)


def _classify_code_namespace(
    value: object,
    prefix: tuple[str, ...] = (),
) -> tuple[tuple[str, int, int], ...] | None:
    if not isinstance(value, dict):
        return None
    if prefix and not value:
        return None
    entries: list[tuple[str, int, int]] = []
    for name, node in value.items():
        if not _nonempty_string(name) or not isinstance(node, dict):
            return None
        is_leaf = any(
            field in node and not isinstance(node[field], dict)
            for field in ("offset", "type", "length")
        )
        if is_leaf:
            if node.keys() != {"offset", "type", "length"}:
                return None
            offset = _normalize_storage_slot(node.get("offset"))
            length = _normalize_storage_size(node.get("length"))
            if offset is None or length is None or not _nonempty_string(node.get("type")):
                return None
            entries.append((".".join((*prefix, name)), offset, length))
            continue
        nested = _classify_code_namespace(node, (*prefix, name))
        if nested is None:
            return None
        entries.extend(nested)
    if len(entries) != len({name for name, _offset, _length in entries}):
        return None
    return tuple(entries)


def _valid_layout_entry_sets(
    persistent: tuple[_StorageLayoutEntry, ...],
    transient: tuple[_StorageLayoutEntry, ...],
) -> bool:
    entries = (*persistent, *transient)
    if entries and len({entry.n_slots is None for entry in entries}) != 1:
        return False
    return _valid_storage_entry_spans(persistent) and _valid_storage_entry_spans(transient)


def _valid_storage_entry_spans(entries: tuple[_StorageLayoutEntry, ...]) -> bool:
    spans: list[tuple[int, int]] = []
    for entry in entries:
        size = entry.n_slots or 1
        end = entry.slot + size
        if end > UINT256_LIMIT:
            return False
        spans.append((entry.slot, end))
    spans.sort()
    return all(current[0] >= previous[1] for previous, current in pairwise(spans))


def _valid_code_entry_spans(entries: tuple[tuple[str, int, int], ...]) -> bool:
    spans: list[tuple[int, int]] = []
    for _name, offset, length in entries:
        end = offset + length
        if end > UINT256_LIMIT:
            return False
        spans.append((offset, end))
    spans.sort()
    return all(current[0] >= previous[1] for previous, current in pairwise(spans))


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
        return _compile_source_file_with_final_newline(command, path, config, suppress_warnings)
    return result


def _compile_source_file_with_final_newline(
    command: list[str], path: Path, config: Config, suppress_warnings: bool
) -> CompileResult:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return CompileResult("failed", stderr="could not read source for final-newline retry")
    tmp = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix=f".{path.stem}.vyupgrade.source.",
        suffix=path.suffix,
        dir=path.parent,
        delete=False,
    )
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


@contextmanager
def target_overlay(
    sources: Mapping[Path, str],
    target_version: str,
    search_paths: tuple[Path, ...] = (),
) -> Iterator[TargetOverlay | None]:
    resolved_sources = {path.resolve(): source for path, source in sources.items()}
    if not resolved_sources:
        yield None
        return
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
    with tempfile.TemporaryDirectory(prefix="vyupgrade-target-") as tmp:
        root = Path(tmp)
        paths: dict[Path, Path] = {}
        overlay_search_paths: set[Path] = set()
        for source_root in roots:
            overlay_search_paths.update(
                _copy_validation_sources(
                    source_root,
                    import_roots,
                    common,
                    root,
                    target_version,
                    resolved_sources,
                )
            )
        overlay_search_paths.update(_overlay_configured_search_paths(search_paths, common, root))
        for path, source in resolved_sources.items():
            try:
                relative = path.relative_to(common)
            except ValueError:
                continue
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            paths[path] = target
            overlay_search_paths.add(target.parent)
        _copy_project_configs(common, root)
        yield TargetOverlay(
            root=root,
            paths=paths,
            source_roots=tuple(roots),
            search_paths=tuple(
                sorted(
                    (path for path in overlay_search_paths if path != root),
                    key=lambda path: str(path),
                )
            ),
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


def _copy_validation_sources(
    source_root: Path,
    import_roots: tuple[Path, ...],
    common_root: Path,
    target_root: Path,
    target_version: str,
    overrides: Mapping[Path, str],
) -> set[Path]:
    search_paths: set[Path] = set()
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
        for resolved in _validation_import_sources(
            current, current_source, source_root, import_roots
        ):
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
            _copy_validation_source(
                resolved,
                source,
                common_root,
                target_root,
                target_version,
                search_paths,
            )
    return search_paths


def _copy_validation_source(
    source_path: Path,
    source: str,
    common_root: Path,
    target_root: Path,
    target_version: str,
    search_paths: set[Path],
) -> None:
    if source_path.suffix not in VALIDATION_SOURCE_SUFFIXES:
        return
    try:
        relative = source_path.relative_to(common_root)
    except ValueError:
        return
    target = target_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_path.suffix == ".json":
        target.write_text(source, encoding="utf-8")
        search_paths.add(target.parent)
        return
    source = _standard_json_package_dependency_source(source_path, source, common_root)
    target.write_text(
        _target_validation_source(
            source,
            target_version,
            is_interface=source_path.suffix == ".vyi",
        ),
        encoding="utf-8",
    )
    search_paths.add(target.parent)
    if source_path.name == "create2_address.vy":
        alias = target.with_name("create2.vy")
        alias.write_text(
            _target_validation_source(
                _target_validation_create2_alias_source(source),
                target_version,
                is_interface=False,
            ),
            encoding="utf-8",
        )


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
) -> tuple[Path, ...]:
    imports: list[Path] = []
    for line in source.splitlines():
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
    path: Path, source_root: Path, module: str, names: tuple[str, ...], import_roots: tuple[Path, ...]
) -> tuple[Path, ...]:
    bases, module_parts = _validation_import_bases(path, source_root, module, import_roots)
    candidates: list[Path] = []
    for base in bases:
        module_path = base.joinpath(*module_parts) if module_parts else base
        if names:
            for name in names:
                candidates.extend(_validation_module_candidates(module_path / name))
            candidates.extend(_validation_module_candidates(module_path))
        else:
            candidates.extend(_validation_module_candidates(module_path))
    resolved: list[Path] = []
    roots = tuple(root.resolve() for root in (source_root, *import_roots))
    for candidate in candidates:
        try:
            resolved_candidate = candidate.resolve()
            if not any(_is_relative_to(resolved_candidate, root) for root in roots):
                continue
        except (OSError, ValueError):
            continue
        if resolved_candidate.exists() and resolved_candidate.suffix in VALIDATION_SOURCE_SUFFIXES:
            resolved.append(resolved_candidate)
    return tuple(dict.fromkeys(resolved))


def _validation_import_bases(
    path: Path, source_root: Path, module: str, import_roots: tuple[Path, ...]
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    if not module.startswith("."):
        bases = (path.parent, source_root, *import_roots)
        return tuple(dict.fromkeys(bases)), tuple(part for part in module.split(".") if part)
    level = len(module) - len(module.lstrip("."))
    base = path.parent
    for _ in range(max(level - 1, 0)):
        base = base.parent
    return (base,), tuple(part for part in module[level:].split(".") if part)


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


def _copy_project_configs(source_root: Path, target_root: Path) -> None:
    for pyproject in source_root.rglob("pyproject.toml"):
        if any(part in {".git", ".venv", "venv", "node_modules"} for part in pyproject.parts):
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
    source_layouts = _canonical_storage_layouts(source.artifacts.get("layout"))
    target_layouts = _canonical_storage_layouts(target.artifacts.get("layout"))
    source_abi = source.artifacts.get("abi")
    target_abi = target.artifacts.get("abi")
    source_methods = source.artifacts.get("method_identifiers")
    target_methods = target.artifacts.get("method_identifiers")
    return (
        None
        if source_abi is None or target_abi is None
        else _canonical_abi(source_abi) == _canonical_abi(target_abi),
        None
        if source_methods is None or target_methods is None
        else _canonical_method_identifiers(source_methods)
        == _canonical_method_identifiers(target_methods),
        None
        if source_layouts is None or target_layouts is None
        else _storage_layouts_equal(*source_layouts, *target_layouts),
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
    source_layouts = _canonical_storage_layouts(source.artifacts.get("layout"))
    target_layouts = _canonical_storage_layouts(target.artifacts.get("layout"))
    return (
        _abi_diff(source_abi, target_abi),
        _method_identifier_diff(source_methods, target_methods),
        []
        if source_layouts is None or target_layouts is None
        else _storage_layout_diff(*source_layouts, *target_layouts),
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


def _canonical_storage_layouts(
    layout: object,
) -> tuple[
    dict[str, _CanonicalStorageValue],
    dict[str, _CanonicalStorageValue],
] | None:
    classified = _classify_layout_artifact(layout)
    if classified is None:
        return None
    storage_entries, transient_entries = classified
    storage = _normalize_layout_entries(storage_entries)
    transient = _normalize_layout_entries(transient_entries)
    if storage is None or transient is None:
        return None
    return storage, transient


def _normalize_layout_entries(
    entries: tuple[_StorageLayoutEntry, ...],
) -> dict[str, _CanonicalStorageValue] | None:
    normalized: dict[str, _CanonicalStorageValue] = {}
    for entry in entries:
        canonical_type, type_safe = _canonical_storage_type(entry.type_name)
        if not type_safe:
            return None
        inferred_width = _known_storage_width(canonical_type)
        if inferred_width == 0:
            return None
        if (
            inferred_width is not None
            and entry.n_slots is not None
            and entry.n_slots != inferred_width
        ):
            return None
        canonical_width = inferred_width if inferred_width is not None else entry.n_slots
        normalized_name = (
            f"$nonreentrant:{entry.slot}" if canonical_type == "nonreentrant lock" else entry.name
        )
        if normalized_name in normalized:
            return None
        normalized[normalized_name] = (entry.slot, canonical_type, canonical_width)
    if not _valid_canonical_storage_spans(normalized):
        return None
    return normalized


def _valid_canonical_storage_spans(
    layout: dict[str, _CanonicalStorageValue],
) -> bool:
    spans: list[tuple[int, int]] = []
    for slot, _type_name, width in layout.values():
        end = slot + (width or 1)
        if end > UINT256_LIMIT:
            return False
        spans.append((slot, end))
    spans.sort()
    return all(current[0] >= previous[1] for previous, current in pairwise(spans))


def _known_storage_width(
    type_name: str,
) -> int | None:
    """Infer widths encoded by Vyper's stable storage type grammar.

    ``None`` means the canonical spelling does not carry enough structure to
    prove a width (for example, a bare user-defined struct name). ``0`` marks a
    recognized but invalid or uint256-overflowing type expression.
    """
    type_name = type_name.strip()
    if type_name == "nonreentrant lock":
        return 1
    if type_name in {"address", "bool", "decimal"}:
        return 1
    integer = re.fullmatch(r"u?int(?P<bits>[0-9]+)?", type_name)
    if integer is not None:
        bits = int(integer.group("bits") or "256")
        return 1 if 8 <= bits <= 256 and bits % 8 == 0 else 0
    fixed_bytes = re.fullmatch(r"bytes(?P<size>[0-9]+)", type_name)
    if fixed_bytes is not None:
        size = int(fixed_bytes.group("size"))
        return 1 if 1 <= size <= 32 else 0
    if re.fullmatch(r"interface\s+[A-Za-z_][A-Za-z0-9_]*", type_name):
        return 1

    bounded_bytes = re.fullmatch(r"(?:Bytes|String)\[(?P<size>[0-9]+)\]", type_name)
    if bounded_bytes is not None:
        size = int(bounded_bytes.group("size"))
        return _checked_storage_width(1 + (size + 31) // 32) if size > 0 else 0

    hashmap_args = _storage_generic_args(type_name, "HashMap")
    if hashmap_args is not None:
        return 1 if len(hashmap_args) == 2 and all(hashmap_args) else 0

    dynarray_args = _storage_generic_args(type_name, "DynArray")
    if dynarray_args is not None:
        if len(dynarray_args) != 2 or not dynarray_args[0]:
            return 0
        try:
            length = int(dynarray_args[1], 10)
        except ValueError:
            return 0
        element_width = _known_storage_width(
            dynarray_args[0],
        )
        if length <= 0 or element_width == 0:
            return 0
        if element_width is None:
            return None
        return _checked_storage_width(1 + element_width * length)

    fixed_array = re.fullmatch(r"(?P<element>.+)\[(?P<length>[0-9]+)\]", type_name)
    if fixed_array is not None:
        length = int(fixed_array.group("length"))
        element_width = _known_storage_width(
            fixed_array.group("element"),
        )
        if length <= 0 or element_width == 0:
            return 0
        if element_width is None:
            return None
        return _checked_storage_width(element_width * length)
    return None


def _checked_storage_width(value: int) -> int:
    return value if 0 < value < UINT256_LIMIT else 0


def _storage_generic_args(type_name: str, generic: str) -> list[str] | None:
    prefix = f"{generic}["
    if not type_name.startswith(prefix):
        return None
    open_index = len(generic)
    close_index = _matching_square_bracket(type_name, open_index)
    if close_index != len(type_name) - 1:
        return []
    return _split_storage_type_args(type_name[open_index + 1 : close_index])


def _split_storage_type_args(value: str) -> list[str]:
    args: list[str] = []
    start = 0
    stack: list[str] = []
    pairs = {"]": "[", ")": "("}
    for index, char in enumerate(value):
        if char in "[(":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return []
        elif char == "," and not stack:
            args.append(value[start:index].strip())
            start = index + 1
    if stack:
        return []
    args.append(value[start:].strip())
    return args


def _canonical_storage_type(type_name: str) -> tuple[str, bool]:
    original_type = type_name
    type_name, paths_safe = _normalize_storage_type_paths(type_name)
    if not paths_safe:
        # A comma or bracket inside a path segment is indistinguishable from a
        # type delimiter without compiler-owned structure.  Preserve the
        # complete spelling so comparison fails closed instead of partially
        # stripping a suffix from the path.
        return original_type, False
    type_name = type_name.replace(" declaration object", "")
    type_name = re.sub(r"\benum ([A-Za-z_][A-Za-z0-9_]*)\([^][]*\)", r"\1", type_name)
    type_name = _canonical_max_value_arrays(type_name)
    type_name = _strip_legacy_hashmap_storage_suffixes(type_name)
    return type_name, True


def _normalize_storage_type_paths(type_name: str) -> tuple[str, bool]:
    output: list[str] = []
    index = 0
    at_atom_start = True
    while index < len(type_name):
        if at_atom_start:
            while index < len(type_name) and type_name[index] in " \t":
                output.append(type_name[index])
                index += 1
            path_match = STORAGE_PATH_ATOM_RE.match(type_name, index)
            if path_match is not None:
                if _path_match_has_ambiguous_comma_suffix(type_name, path_match.end()):
                    return type_name, False
                output.append(_canonical_file_type_atom(path_match))
                index = path_match.end()
                at_atom_start = False
                continue
            file_match = PLAIN_STORAGE_TYPE_FILE_RE.match(type_name, index)
            if file_match is not None:
                output.append(_canonical_file_type_atom(file_match))
                index = file_match.end()
                at_atom_start = False
                continue
            if index < len(type_name) and (
                type_name[index] in "/\\"
                or (
                    index + 2 < len(type_name)
                    and type_name[index].isalpha()
                    and type_name[index + 1] == ":"
                    and type_name[index + 2] in "/\\"
                )
            ):
                return type_name, False

        char = type_name[index]
        output.append(char)
        index += 1
        at_atom_start = char in "[,"
    normalized = "".join(output)
    if not _valid_storage_type_delimiters(normalized):
        return type_name, False
    return normalized, True


def _canonical_file_type_atom(match: re.Match[str]) -> str:
    base = match.group("base")
    if match.group("extension") == ".vyi":
        return f"interface {base}"
    # Only a .vyi suffix is compiler-owned evidence that this path names an
    # interface handle.  Preserve extensionless and .vy paths so unrelated
    # modules or structs with the same basename cannot compare equal.
    return match.group(0)


def _path_match_has_ambiguous_comma_suffix(type_name: str, end: int) -> bool:
    cursor = end
    while cursor < len(type_name) and type_name[cursor] in " \t":
        cursor += 1
    if cursor >= len(type_name) or type_name[cursor] != ",":
        return False
    next_delimiter = re.search(r"[,\[\]]", type_name[cursor + 1 :])
    segment_end = (
        len(type_name)
        if next_delimiter is None
        else cursor + 1 + next_delimiter.start()
    )
    return any(separator in type_name[cursor + 1 : segment_end] for separator in "/\\")


def _valid_storage_type_delimiters(type_name: str) -> bool:
    stack: list[str] = []
    pairs = {"]": "[", ")": "("}
    for char in type_name:
        if char in "[(":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return False
        elif char == "," and not stack:
            return False
    if stack:
        return False

    for match in re.finditer(r"\b(?:HashMap|DynArray)\s*\[", type_name):
        open_index = type_name.find("[", match.start())
        close_index = _matching_square_bracket(type_name, open_index)
        if close_index is None:
            return False
        depth = 0
        commas = 0
        for char in type_name[open_index + 1 : close_index]:
            if char in "[(":
                depth += 1
            elif char in "])":
                depth -= 1
            elif char == "," and depth == 0:
                commas += 1
        if commas != 1:
            return False
    return True


def _canonical_max_value_arrays(type_name: str) -> str:
    pattern = re.compile(
        rf"\b(?P<element>[A-Za-z_][A-Za-z0-9_]*|u?int(?:\d+)?|bytes(?:\d+)?|address|bool)"
        rf"\[{UINT256_MAX_DECIMAL}\]"
    )
    while True:
        updated = pattern.sub(r"HashMap[uint256, \g<element>]", type_name)
        if updated == type_name:
            return type_name
        type_name = updated


def _strip_legacy_hashmap_storage_suffixes(type_name: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(type_name):
        if not type_name.startswith("HashMap[", index):
            output.append(type_name[index])
            index += 1
            continue
        open_index = index + len("HashMap")
        close_index = _matching_square_bracket(type_name, open_index)
        if close_index is None:
            output.append(type_name[index])
            index += 1
            continue
        inner = _strip_legacy_hashmap_storage_suffixes(type_name[open_index + 1 : close_index])
        output.append(f"HashMap[{inner}]")
        index = close_index + 1
        if index < len(type_name) and type_name[index] == "[":
            suffix_end = _matching_square_bracket(type_name, index)
            if suffix_end is not None:
                index = suffix_end + 1
    return "".join(output)


def _matching_square_bracket(value: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(value)):
        char = value[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
    return None


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


def _storage_layout_diff(
    source: dict[str, _CanonicalStorageValue],
    source_transient: dict[str, _CanonicalStorageValue],
    target: dict[str, _CanonicalStorageValue],
    target_transient: dict[str, _CanonicalStorageValue],
) -> list[str]:
    moved_locks = _moved_nonreentrant_locks(
        source,
        target,
        source_transient,
        target_transient,
    )
    preserved_lock_gaps = _counterpart_nonreentrant_storage_gaps(
        source,
        target,
        excluded_source_names=set(moved_locks),
    )
    moved_lock_names = set(moved_locks) | set(preserved_lock_gaps)
    preserved_gap_names = set(preserved_lock_gaps.values())
    moved_transient_names = set(moved_locks.values())
    return [
        *(
            f"moved storage to transient: {name} slot {_slot_type(source[name])} -> {target_name} slot {_slot_type(target_transient[target_name])}"
            for name, target_name in moved_locks.items()
        ),
        *_mapping_diff_lines(
            source,
            target,
            removed=lambda key, value: f"removed storage: {key} slot {_slot_type(value)}",
            added=lambda key, value: f"added storage: {key} slot {_slot_type(value)}",
            changed=lambda key, before, after: [
                _changed_storage_line("storage", key, before, after)
            ],
            skip_removed=moved_lock_names,
            skip_added=preserved_gap_names,
            equivalent=_storage_values_equal,
        ),
        *_mapping_diff_lines(
            source_transient,
            target_transient,
            removed=lambda key, value: (
                f"removed transient storage: {key} slot {_slot_type(value)}"
            ),
            added=lambda key, value: f"added transient storage: {key} slot {_slot_type(value)}",
            changed=lambda key, before, after: [
                _changed_storage_line("transient storage", key, before, after)
            ],
            skip_added=moved_transient_names,
            equivalent=_storage_values_equal,
        ),
    ]


def _storage_layouts_equal(
    source: dict[str, _CanonicalStorageValue],
    source_transient: dict[str, _CanonicalStorageValue],
    target: dict[str, _CanonicalStorageValue],
    target_transient: dict[str, _CanonicalStorageValue],
) -> bool:
    return all(
        line.startswith("moved storage to transient: ")
        for line in _storage_layout_diff(source, source_transient, target, target_transient)
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
    equivalent: Callable[[object, object], bool] | None = None,
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
        if not (equivalent(source[key], target[key]) if equivalent else source[key] == target[key]):
            lines.extend(changed(key, source[key], target[key]))
    return lines


def _changed_abi_lines(key: str, source: object, target: object) -> list[str]:
    details = _abi_entry_diff(source, target)
    if not details:
        return [f"changed ABI entry: {key}"]
    return [f"changed ABI entry: {key}: {detail}" for detail in details]


def _moved_nonreentrant_locks(
    source: dict[str, _CanonicalStorageValue],
    target: dict[str, _CanonicalStorageValue],
    source_transient: dict[str, _CanonicalStorageValue],
    target_transient: dict[str, _CanonicalStorageValue],
) -> dict[str, str]:
    transient_locks = {
        name: value
        for name, value in sorted(target_transient.items())
        if name not in source_transient and value[1] == "nonreentrant lock"
    }
    moved: dict[str, str] = {}
    for name, value in sorted(source.items()):
        if name in target:
            continue
        if not name.startswith("$nonreentrant:") or value[1] != "nonreentrant lock":
            continue
        candidates = [
            target_name
            for target_name, target_value in transient_locks.items()
            if _storage_move_compatible(value, target_value)
        ]
        if not candidates:
            continue
        target_name = name if name in candidates else candidates[0]
        moved[name] = target_name
        transient_locks.pop(target_name)
    return moved


def _counterpart_nonreentrant_storage_gaps(
    source: dict[str, _CanonicalStorageValue],
    target: dict[str, _CanonicalStorageValue],
    *,
    excluded_source_names: set[str],
) -> dict[str, str]:
    candidates = {
        name: value
        for name, value in target.items()
        if name not in source
        and GENERATED_REENTRANCY_GAP_RE.fullmatch(name)
        and value[1] == "uint256"
    }
    preserved: dict[str, str] = {}
    for source_name, source_value in sorted(source.items()):
        if (
            source_name in target
            or source_name in excluded_source_names
            or not source_name.startswith("$nonreentrant:")
            or source_value[1] != "nonreentrant lock"
            or not _known_single_slot(source_value)
        ):
            continue
        matches = [
            target_name
            for target_name, target_value in candidates.items()
            if source_value[0] == target_value[0]
            and _known_single_slot(target_value)
            and source_value[2] == target_value[2]
        ]
        if len(matches) != 1:
            continue
        target_name = matches[0]
        preserved[source_name] = target_name
        candidates.pop(target_name)
    return preserved


def _storage_move_compatible(
    source: _CanonicalStorageValue,
    target: _CanonicalStorageValue,
) -> bool:
    return (
        source[1] == target[1] == "nonreentrant lock"
        and _known_single_slot(source)
        and _known_single_slot(target)
        and source[2] == target[2]
    )


def _known_single_slot(value: _CanonicalStorageValue) -> bool:
    return value[2] == 1


def _storage_values_equal(source: object, target: object) -> bool:
    if not isinstance(source, tuple) or not isinstance(target, tuple):
        return source == target
    source_slot, source_type, source_size = source
    target_slot, target_type, target_size = target
    return (
        source_slot == target_slot
        and source_type == target_type
        and source_size == target_size
    )


def _changed_storage_line(
    label: str,
    key: str,
    source: object,
    target: object,
) -> str:
    line = f"changed {label}: {key} slot {_slot_type(source)} -> {_slot_type(target)}"
    if (
        isinstance(source, tuple)
        and isinstance(target, tuple)
        and source[2] != target[2]
    ):
        source_width = source[2] if source[2] is not None else "unknown"
        target_width = target[2] if target[2] is not None else "unknown"
        line += f" (n_slots {source_width} -> {target_width})"
    return line


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


def _slot_type(value: object) -> str:
    if not isinstance(value, tuple):
        return str(value)
    slot, type_name, _n_slots = value
    return f"{slot} {type_name}"


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
    return _run_compile_with_formats(command, path, config, FORMATS, extra_paths, suppress_warnings)


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
            if not allow_unsupported_formats:
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
                allow_unsupported_formats=True,
                unavailable_formats=unavailable,
            )
        span_error_format = (
            _legacy_span_error_format(stderr, formats) if allow_unsupported_formats else None
        )
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
                allow_unsupported_formats=True,
                unavailable_formats=unavailable,
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
        "degraded" if unavailable_formats else "passed",
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
