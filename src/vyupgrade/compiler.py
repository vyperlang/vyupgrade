from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
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


@dataclass
class CompileResult:
    status: str
    artifacts: dict[str, object] | None = None
    stderr: str | None = None
    command: list[str] | None = None


def compile_source_file(path: Path, config: Config, source_version: str | None) -> CompileResult:
    if path.suffix != ".vy":
        return CompileResult("skipped")
    normalized = _normalize_version(source_version or infer_pragma(path.read_text()))
    command = _compiler_command(
        config.source_vyper,
        normalized,
        config.source_python,
    )
    return _run_compile_with_formats(
        command,
        path,
        config,
        SOURCE_FORMATS,
        (),
        _supports_warning_policy(normalized),
    )


def compile_target_source(path: Path, source: str, config: Config) -> CompileResult:
    if path.suffix != ".vy":
        return CompileResult("skipped")
    compile_source = _target_validation_source(source, config.target_version)
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
        normalized = _normalize_version(config.target_version)
        command = _compiler_command(config.target_vyper, normalized, config.target_python)
        compile_config = _target_compile_config(compile_source, config)
        return _run_compile(
            command,
            tmp_path,
            compile_config,
            extra_paths=(path.parent,),
            suppress_warnings=_supports_warning_policy(normalized),
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def compile_source_ast(path: Path, config: Config, source_version: str | None) -> CompileResult:
    if path.suffix != ".vy":
        return CompileResult("skipped")
    normalized = _normalize_version(source_version or infer_pragma(path.read_text()))
    command = _compiler_command(
        config.source_vyper,
        normalized,
        config.source_python,
    )
    return _run_compile_with_formats(
        command,
        path,
        config,
        ("ast",),
        (),
        _supports_warning_policy(normalized),
    )


def compare_artifacts(source: CompileResult, target: CompileResult) -> tuple[bool | None, bool | None, bool | None]:
    if source.artifacts is None or target.artifacts is None:
        return None, None, None
    source_layout = _canonical_storage_layout(source.artifacts.get("layout"))
    target_layout = _canonical_storage_layout(target.artifacts.get("layout"))
    source_abi = source.artifacts.get("abi")
    target_abi = target.artifacts.get("abi")
    source_methods = source.artifacts.get("method_identifiers")
    target_methods = target.artifacts.get("method_identifiers")
    return (
        None if source_abi is None or target_abi is None else _canonical_abi(source_abi) == _canonical_abi(target_abi),
        None
        if source_methods is None or target_methods is None
        else _canonical_method_identifiers(source_methods)
        == _canonical_method_identifiers(target_methods),
        None if source_layout is None or target_layout is None else source_layout == target_layout,
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


def _target_validation_source(source: str, target_version: str) -> str:
    pattern = re.compile(r"^(\s*)#\s*(?:@version|pragma\s+version)\s+(.+?)\s*$", re.MULTILINE)
    return pattern.sub(lambda match: f"{match.group(1)}#pragma version {target_version}", source, count=1)


def _canonical_abi(abi: object) -> object:
    if not isinstance(abi, list):
        return abi
    entries = [_strip_abi_metadata(entry) for entry in abi if isinstance(entry, dict)]
    return sorted(entries, key=lambda entry: json.dumps(_abi_sort_key(entry), sort_keys=True))


def _strip_abi_metadata(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_abi_metadata(item)
            for key, item in sorted(value.items())
            if key not in {"gas"}
        }
    if isinstance(value, list):
        return [_strip_abi_metadata(item) for item in value]
    return value


def _abi_sort_key(entry: dict[str, object]) -> tuple[object, ...]:
    inputs = entry.get("inputs")
    input_types: tuple[object, ...] = ()
    if isinstance(inputs, list):
        input_types = tuple(
            item.get("type") if isinstance(item, dict) else None
            for item in inputs
        )
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
    normalized: dict[str, tuple[int, str]] = {}
    for name, value in layout.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        if value.get("location", "storage") != "storage":
            continue
        slot = value.get("slot")
        type_name = value.get("type")
        if not isinstance(slot, int) or not isinstance(type_name, str):
            continue
        normalized[name] = (slot, _canonical_storage_type(type_name))
    return normalized


def _canonical_transient_storage_layout(layout: object) -> dict[str, tuple[int, str]]:
    if not isinstance(layout, dict):
        return {}
    transient = layout.get("transient_storage_layout")
    if not isinstance(transient, dict):
        return {}
    normalized: dict[str, tuple[int, str]] = {}
    for name, value in transient.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        slot = value.get("slot")
        type_name = value.get("type")
        if not isinstance(slot, int) or not isinstance(type_name, str):
            continue
        normalized[name] = (slot, _canonical_storage_type(type_name))
    return normalized


def _canonical_storage_type(type_name: str) -> str:
    type_name = type_name.rsplit("/", 1)[-1]
    type_name = type_name.removesuffix(".vyi")
    if type_name in {"IERC20", "IERC20Detailed", "IERC4626", "IERC721", "IERC1155", "IERC165"}:
        return type_name[1:]
    return type_name


def _abi_diff(source: object, target: object) -> list[str]:
    if source is None or target is None:
        return []
    source_entries = _abi_entry_map(_canonical_abi(source))
    target_entries = _abi_entry_map(_canonical_abi(target))
    lines = [
        *(
            f"removed ABI entry: {key}"
            for key in sorted(source_entries.keys() - target_entries.keys())
        ),
        *(
            f"added ABI entry: {key}"
            for key in sorted(target_entries.keys() - source_entries.keys())
        ),
    ]
    for key in sorted(source_entries.keys() & target_entries.keys()):
        if source_entries[key] == target_entries[key]:
            continue
        details = _abi_entry_diff(source_entries[key], target_entries[key])
        if not details:
            lines.append(f"changed ABI entry: {key}")
            continue
        lines.extend(f"changed ABI entry: {key}: {detail}" for detail in details)
    return lines


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
    return [
        *(
            f"removed selector: {key} = {source_methods[key]}"
            for key in sorted(source_methods.keys() - target_methods.keys())
        ),
        *(
            f"added selector: {key} = {target_methods[key]}"
            for key in sorted(target_methods.keys() - source_methods.keys())
        ),
        *(
            f"changed selector: {key} {source_methods[key]} -> {target_methods[key]}"
            for key in sorted(source_methods.keys() & target_methods.keys())
            if source_methods[key] != target_methods[key]
        ),
    ]


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
        *(
            f"removed storage: {name} slot {_slot_type(source[name])}"
            for name in sorted((source.keys() - target.keys()) - moved_lock_names)
        ),
        *(
            f"added storage: {name} slot {_slot_type(target[name])}"
            for name in sorted(target.keys() - source.keys())
        ),
        *(
            f"changed storage: {name} slot {_slot_type(source[name])} -> {_slot_type(target[name])}"
            for name in sorted(source.keys() & target.keys())
            if source[name] != target[name]
        ),
    ]


def _moved_nonreentrant_locks(
    source: dict[str, tuple[int, str]],
    target: dict[str, tuple[int, str]],
    target_transient: dict[str, tuple[int, str]],
) -> dict[str, str]:
    transient_locks = [
        name
        for name, value in sorted(target_transient.items())
        if value[1] == "nonreentrant lock"
    ]
    if not transient_locks:
        return {}
    moved: dict[str, str] = {}
    target_name = transient_locks[0]
    for name, value in sorted(source.items()):
        if name in target:
            continue
        if not name.startswith("nonreentrant.") or value[1] != "nonreentrant lock":
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
        str(item.get("type", "?")) if isinstance(item, dict) else "?"
        for item in inputs
    )


def _slot_type(value: tuple[int, str]) -> str:
    slot, type_name = value
    return f"{slot} {type_name}"


def _compiler_command(explicit: str | None, version: str | None, python: str | None) -> list[str]:
    if explicit:
        return [explicit]
    normalized = _normalize_version(version) or "0.4.3"
    python = python or _default_python(normalized)
    return [_uv_bin(), "run", "--no-project", "--python", python, "--with", f"vyper=={normalized}", "vyper"]


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
    if parsed is not None and parsed < VyperVersion(0, 3, 1):
        return "3.8"
    # Modern Vyper releases run cleanly on Python 3.11. Pinning the subprocess
    # interpreter avoids accidentally using a bleeding-edge project venv, where
    # old compiler dependencies can break.
    return "3.11"


def _supports_warning_policy(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= VyperVersion(0, 4, 1)


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
        return CompileResult("failed", stderr=f"compiler timed out after {COMPILE_TIMEOUT_SECONDS} seconds", command=full)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        if "Unsupported format type 'layout'" in stderr and "layout" in formats:
            fallback_formats = tuple(name for name in formats if name != "layout")
            return _run_compile_with_formats(command, path, config, fallback_formats, extra_paths, suppress_warnings)
        retry_command = _command_with_missing_module_dependency(command, path, stderr)
        if retry_command is not None:
            return _run_compile_with_formats(retry_command, path, config, formats, extra_paths, suppress_warnings)
        return CompileResult("failed", stderr=proc.stderr.strip() or proc.stdout.strip(), command=full)
    try:
        artifacts = _parse_outputs(proc.stdout, formats)
    except json.JSONDecodeError as exc:
        return CompileResult("failed", stderr=f"could not parse compiler output: {exc}", command=full)
    return CompileResult("passed", artifacts=artifacts, stderr=proc.stderr.strip() or None, command=full)


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


def _command_with_missing_module_dependency(command: list[str], path: Path, stderr: str) -> list[str] | None:
    if not _is_uv_run_command(command):
        return None
    missing = _missing_module_name(stderr)
    if missing is None:
        return None
    pyproject = _nearest_pyproject(path.parent)
    dependencies = _pyproject_dependencies(pyproject) if pyproject is not None else {}
    package = dependencies.get(missing.split(".", 1)[0])
    if package is None:
        return None
    retry_command = _uv_command_with_packages(command, (package,))
    return retry_command if retry_command != command else None


def _missing_module_name(stderr: str) -> str | None:
    match = re.search(r"\bModuleNotFound:\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)", stderr)
    return match.group(1) if match else None


def _uv_command_with_packages(command: list[str], packages: tuple[str, ...]) -> list[str]:
    full = command[:-1]
    for package in packages:
        if _uv_command_has_package(command, package):
            continue
        full.extend(["--with", package])
    full.append(command[-1])
    return full


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
    if pyproject is None:
        return ()
    dependencies = _pyproject_dependencies(pyproject)
    return tuple(dependencies[name] for name in imports if name in dependencies)


def _vyper_import_roots(source: str) -> tuple[str, ...]:
    roots: set[str] = set()
    for line in source.splitlines():
        match = re.match(r"\s*from\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\b", line)
        if match is None:
            match = re.match(r"\s*import\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b", line)
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
