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


@dataclass(frozen=True)
class TargetOverlay:
    root: Path
    paths: Mapping[Path, Path]
    source_roots: tuple[Path, ...]
    search_paths: tuple[Path, ...]


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
    if path.suffix != ".vy":
        return CompileResult("skipped")
    compile_source = _target_validation_source(
        source, config.target_version, is_interface=path.suffix == ".vyi"
    )
    if overlay is not None:
        tmp_path = overlay.paths.get(path.resolve())
        if tmp_path is not None:
            command, suppress_warnings = _prepare_command(
                config.target_vyper, config.target_version, config.target_python
            )
            compile_config = _target_compile_config(compile_source, config)
            compile_config = replace(
                compile_config,
                compiler_search_paths=_overlay_search_paths(
                    overlay, compile_config.compiler_search_paths
                ),
            )
            return _run_compile(
                command,
                tmp_path,
                compile_config,
                extra_paths=(),
                suppress_warnings=suppress_warnings,
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
        tmp.write(compile_source)
        tmp_path = Path(tmp.name)
    try:
        command, suppress_warnings = _prepare_command(
            config.target_vyper, config.target_version, config.target_python
        )
        compile_config = _target_compile_config(compile_source, config)
        return _run_compile(
            command,
            tmp_path,
            compile_config,
            extra_paths=(path.parent,),
            suppress_warnings=suppress_warnings,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


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
            validation_source = _standard_json_package_dependency_source(path, source, common)
            target.write_text(
                _target_validation_source(
                    validation_source,
                    target_version,
                    is_interface=path.suffix == ".vyi",
                ),
                encoding="utf-8",
            )
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
            _target_validation_source(source, target_version, is_interface=False),
            encoding="utf-8",
        )


def _standard_json_package_dependency_source(source_path: Path, source: str, common_root: Path) -> str:
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
    )


def compare_artifacts(
    source: CompileResult, target: CompileResult
) -> tuple[bool | None, bool | None, bool | None]:
    if source.artifacts is None or target.artifacts is None:
        return None, None, None
    source_layout = _canonical_storage_layout(source.artifacts.get("layout"))
    target_layout = _canonical_storage_layout(target.artifacts.get("layout"))
    source_abi = source.artifacts.get("abi")
    target_abi = target.artifacts.get("abi")
    source_methods = source.artifacts.get("method_identifiers")
    target_methods = target.artifacts.get("method_identifiers")
    target_transient_layout = _canonical_transient_storage_layout(target.artifacts.get("layout"))
    return (
        None
        if source_abi is None or target_abi is None
        else _canonical_abi(source_abi) == _canonical_abi(target_abi),
        None
        if source_methods is None or target_methods is None
        else _canonical_method_identifiers(source_methods)
        == _canonical_method_identifiers(target_methods),
        None
        if source_layout is None or target_layout is None
        else _persistent_storage_layout_equal(source_layout, target_layout, target_transient_layout),
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
    source_layout = _canonical_storage_layout(source.artifacts.get("layout"))
    target_layout = _canonical_storage_layout(target.artifacts.get("layout"))
    target_transient_layout = _canonical_transient_storage_layout(target.artifacts.get("layout"))
    return (
        _abi_diff(source_abi, target_abi),
        _method_identifier_diff(source_methods, target_methods),
        _storage_layout_diff(source_layout, target_layout, target_transient_layout),
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
        return {
            key: _canonical_abi_value(key, item, value)
            for key, item in sorted(value.items())
            if key not in {"gas", "internalType"}
            and not (key == "name" and item == "")
            and not (value.get("type") == "constructor" and key == "outputs")
            and not (key == "components" and _is_tuple_abi_type(value.get("type")))
        }
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


def _canonical_storage_layout(layout: object) -> dict[str, tuple[int, str]] | None:
    if not isinstance(layout, dict):
        return None
    storage = layout.get("storage_layout")
    if isinstance(storage, dict):
        layout = storage
    return _normalize_layout_entries(layout, location_filter="storage")


def _canonical_transient_storage_layout(layout: object) -> dict[str, tuple[int, str]]:
    if not isinstance(layout, dict):
        return {}
    transient = layout.get("transient_storage_layout")
    if not isinstance(transient, dict):
        return {}
    return _normalize_layout_entries(transient)


def _normalize_layout_entries(
    layout: dict[object, object], *, location_filter: str | None = None
) -> dict[str, tuple[int, str]]:
    normalized: dict[str, tuple[int, str]] = {}
    for name, value in layout.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        if (
            location_filter is not None
            and value.get("location", location_filter) != location_filter
        ):
            continue
        slot = value.get("slot")
        type_name = value.get("type")
        if not isinstance(slot, int) or not isinstance(type_name, str):
            continue
        canonical_type = _canonical_storage_type(type_name)
        if (
            canonical_type == "uint256"
            and name.startswith("_vyupgrade_reentrancy_lock_slot")
        ):
            canonical_type = "nonreentrant lock"
        normalized_name = f"$nonreentrant:{slot}" if canonical_type == "nonreentrant lock" else name
        normalized[normalized_name] = (slot, canonical_type)
    return normalized


def _canonical_storage_type(type_name: str) -> str:
    type_name = _strip_storage_type_paths(type_name)
    type_name = type_name.replace("interface ", "")
    type_name = type_name.replace(" declaration object", "")
    type_name = re.sub(r"\benum ([A-Za-z_][A-Za-z0-9_]*)\([^][]*\)", r"\1", type_name)
    type_name = _canonical_max_value_arrays(type_name)
    type_name = _strip_legacy_hashmap_storage_suffixes(type_name)
    type_name = re.sub(
        r"\bI(ERC20Detailed|ERC4626|ERC20|ERC721|ERC1155|ERC165)\b",
        r"\1",
        type_name,
    )
    return type_name


def _strip_storage_type_paths(type_name: str) -> str:
    type_name = re.sub(
        r"(?<![A-Za-z0-9_])(?:/[A-Za-z0-9_.-]+)+/([A-Za-z_][A-Za-z0-9_]*)(?:\.vyi?|\.vy)",
        r"\1",
        type_name,
    )
    return re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)(?:\.vyi?|\.vy)\b", r"\1", type_name)


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
    source: dict[str, tuple[int, str]] | None,
    target: dict[str, tuple[int, str]] | None,
    target_transient: dict[str, tuple[int, str]] | None = None,
) -> list[str]:
    if source is None or target is None:
        return []
    target_transient = target_transient or {}
    moved_locks = _moved_nonreentrant_locks(source, target, target_transient)
    moved_lock_names = set(moved_locks)
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
                f"changed storage: {key} slot {_slot_type(before)} -> {_slot_type(after)}"
            ],
            skip_removed=moved_lock_names,
        ),
    ]


def _persistent_storage_layout_equal(
    source: dict[str, tuple[int, str]],
    target: dict[str, tuple[int, str]],
    target_transient: dict[str, tuple[int, str]],
) -> bool:
    return all(
        line.startswith("moved storage to transient: ")
        for line in _storage_layout_diff(source, target, target_transient)
    )


def _mapping_diff_lines(
    source: Mapping[str, object],
    target: Mapping[str, object],
    *,
    removed: Callable[[str, object], str],
    added: Callable[[str, object], str],
    changed: Callable[[str, object, object], list[str]],
    skip_removed: set[str] | None = None,
) -> list[str]:
    skip_removed = skip_removed or set()
    lines = [
        removed(key, source[key]) for key in sorted((source.keys() - target.keys()) - skip_removed)
    ]
    lines.extend(added(key, target[key]) for key in sorted(target.keys() - source.keys()))
    for key in sorted(source.keys() & target.keys()):
        if source[key] != target[key]:
            lines.extend(changed(key, source[key], target[key]))
    return lines


def _changed_abi_lines(key: str, source: object, target: object) -> list[str]:
    details = _abi_entry_diff(source, target)
    if not details:
        return [f"changed ABI entry: {key}"]
    return [f"changed ABI entry: {key}: {detail}" for detail in details]


def _moved_nonreentrant_locks(
    source: dict[str, tuple[int, str]],
    target: dict[str, tuple[int, str]],
    target_transient: dict[str, tuple[int, str]],
) -> dict[str, str]:
    transient_locks = [
        name for name, value in sorted(target_transient.items()) if value[1] == "nonreentrant lock"
    ]
    if not transient_locks:
        return {}
    moved: dict[str, str] = {}
    target_name = transient_locks[0]
    for name, value in sorted(source.items()):
        if name in target:
            continue
        if not name.startswith("$nonreentrant:") or value[1] != "nonreentrant lock":
            continue
        moved[name] = target_name
    return moved


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


def _slot_type(value: tuple[int, str]) -> str:
    slot, type_name = value
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
    return bool(re.search(r"\bdecimal\b", source))


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
            fallback_formats = tuple(name for name in formats if name != unsupported)
            return _run_compile_with_formats(
                command, path, config, fallback_formats, extra_paths, suppress_warnings
            )
        span_error_format = _legacy_span_error_format(stderr, formats)
        if span_error_format is not None:
            fallback_formats = tuple(name for name in formats if name != span_error_format)
            return _run_compile_with_formats(
                command, path, config, fallback_formats, extra_paths, suppress_warnings
            )
        retry_command = _command_with_missing_module_dependency(command, path, stderr)
        if retry_command is not None:
            return _run_compile_with_formats(
                retry_command, path, config, formats, extra_paths, suppress_warnings
            )
        return CompileResult(
            "failed", stderr=proc.stderr.strip() or proc.stdout.strip(), command=full
        )
    try:
        artifacts = _parse_outputs(proc.stdout, formats)
    except json.JSONDecodeError as exc:
        return CompileResult(
            "failed", stderr=f"could not parse compiler output: {exc}", command=full
        )
    return CompileResult(
        "passed", artifacts=artifacts, stderr=proc.stderr.strip() or None, command=full
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
    artifacts: dict[str, object] = {}
    for name, raw in zip(formats, chunks, strict=False):
        artifacts[name] = json.loads(raw)
    return artifacts
