from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from uv import find_uv_bin

from .models import Config
from .versions import VyperVersion, compiler_version_for_spec, infer_pragma, parse_version


FORMATS = ("abi", "method_identifiers", "layout")


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
    return _run_compile(command, path, config, suppress_warnings=_supports_warning_policy(normalized))


def compile_target_source(path: Path, source: str, config: Config) -> CompileResult:
    if path.suffix != ".vy":
        return CompileResult("skipped")
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix=f".{path.stem}.vyupgrade.",
        suffix=".vy",
        delete=False,
    ) as tmp:
        tmp.write(source)
        tmp_path = Path(tmp.name)
    try:
        normalized = _normalize_version(config.target_version)
        command = _compiler_command(config.target_vyper, normalized, config.target_python)
        return _run_compile(
            command,
            tmp_path,
            config,
            extra_paths=(path.parent,),
            suppress_warnings=_supports_warning_policy(normalized),
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def compare_artifacts(source: CompileResult, target: CompileResult) -> tuple[bool | None, bool | None, bool | None]:
    if source.artifacts is None or target.artifacts is None:
        return None, None, None
    return (
        source.artifacts.get("abi") == target.artifacts.get("abi"),
        source.artifacts.get("method_identifiers") == target.artifacts.get("method_identifiers"),
        source.artifacts.get("layout") == target.artifacts.get("layout"),
    )


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
    return parsed is not None and parsed >= VyperVersion(0, 4, 0)


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
    proc = subprocess.run(full, capture_output=True, text=True, timeout=60)
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
