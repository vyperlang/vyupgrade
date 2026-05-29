from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from uv import find_uv_bin

from .models import Config
from .versions import VyperVersion, compiler_version_for_spec, infer_pragma, parse_version


FORMATS = ("abi", "method_identifiers", "layout")
SOURCE_FORMATS = ("abi", "method_identifiers", "layout", "ast")
COMPILE_TIMEOUT_SECONDS = 120
MAX_ARTIFACT_DIFF_LINES = 12


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
    return (
        _abi_diff(source_abi, target_abi),
        _method_identifier_diff(source_methods, target_methods),
        _storage_layout_diff(source_layout, target_layout),
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
    return {key: value for key, value in sorted(methods.items()) if key != "__init__()"}


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
    return _limited_diff_lines(
        [
            *(f"removed ABI entry: {key}" for key in sorted(source_entries.keys() - target_entries.keys())),
            *(f"added ABI entry: {key}" for key in sorted(target_entries.keys() - source_entries.keys())),
            *(
                f"changed ABI entry: {key}"
                for key in sorted(source_entries.keys() & target_entries.keys())
                if source_entries[key] != target_entries[key]
            ),
        ]
    )


def _method_identifier_diff(source: object, target: object) -> list[str]:
    if source is None or target is None:
        return []
    source_methods = _canonical_method_identifiers(source)
    target_methods = _canonical_method_identifiers(target)
    if not isinstance(source_methods, dict) or not isinstance(target_methods, dict):
        return []
    return _limited_diff_lines(
        [
            *(f"removed selector: {key} = {source_methods[key]}" for key in sorted(source_methods.keys() - target_methods.keys())),
            *(f"added selector: {key} = {target_methods[key]}" for key in sorted(target_methods.keys() - source_methods.keys())),
            *(
                f"changed selector: {key} {source_methods[key]} -> {target_methods[key]}"
                for key in sorted(source_methods.keys() & target_methods.keys())
                if source_methods[key] != target_methods[key]
            ),
        ]
    )


def _storage_layout_diff(
    source: dict[str, tuple[int, str]] | None,
    target: dict[str, tuple[int, str]] | None,
) -> list[str]:
    if source is None or target is None:
        return []
    return _limited_diff_lines(
        [
            *(f"removed storage: {name} slot {_slot_type(source[name])}" for name in sorted(source.keys() - target.keys())),
            *(f"added storage: {name} slot {_slot_type(target[name])}" for name in sorted(target.keys() - source.keys())),
            *(
                f"changed storage: {name} slot {_slot_type(source[name])} -> {_slot_type(target[name])}"
                for name in sorted(source.keys() & target.keys())
                if source[name] != target[name]
            ),
        ]
    )


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


def _limited_diff_lines(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_ARTIFACT_DIFF_LINES:
        return lines
    shown = lines[:MAX_ARTIFACT_DIFF_LINES]
    shown.append(f"... {len(lines) - MAX_ARTIFACT_DIFF_LINES} more")
    return shown


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
    full = [*command, "-f", ",".join(formats)]
    for search_path in config.compiler_search_paths:
        full.extend(["-p", str(search_path)])
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
        return CompileResult("failed", stderr=proc.stderr.strip() or proc.stdout.strip(), command=full)
    try:
        artifacts = _parse_outputs(proc.stdout, formats)
    except json.JSONDecodeError as exc:
        return CompileResult("failed", stderr=f"could not parse compiler output: {exc}", command=full)
    return CompileResult("passed", artifacts=artifacts, stderr=proc.stderr.strip() or None, command=full)


def _parse_outputs(stdout: str, formats: tuple[str, ...] = FORMATS) -> dict[str, object]:
    chunks = [line for line in stdout.splitlines() if line.strip()]
    artifacts: dict[str, object] = {}
    for name, raw in zip(formats, chunks, strict=False):
        artifacts[name] = json.loads(raw)
    return artifacts
