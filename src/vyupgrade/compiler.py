from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .models import Config
from .versions import infer_pragma


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
    command = _compiler_command(config.source_vyper, source_version or infer_pragma(path.read_text()))
    return _run_compile(command, path, config)


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
        command = _compiler_command(config.target_vyper, config.target_version)
        return _run_compile(command, tmp_path, config, extra_paths=(path.parent,))
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


def _compiler_command(explicit: str | None, version: str | None) -> list[str]:
    if explicit:
        return [explicit]
    normalized = _normalize_version(version) or "0.4.3"
    return ["uv", "run", "--with", f"vyper=={normalized}", "vyper"]


def _normalize_version(version: str | None) -> str | None:
    if not version:
        return None
    match = re.search(r"0\.(?:3|4)\.\d+", version)
    if match:
        return match.group(0)
    return None


def _run_compile(
    command: list[str], path: Path, config: Config, extra_paths: tuple[Path, ...] = ()
) -> CompileResult:
    full = [*command, "-f", ",".join(FORMATS)]
    for search_path in config.compiler_search_paths:
        full.extend(["-p", str(search_path)])
    for search_path in extra_paths:
        full.extend(["-p", str(search_path)])
    full.extend(["-p", str(path.parent)])
    if config.enable_decimals:
        full.append("--enable-decimals")
    full.append(str(path))
    proc = subprocess.run(full, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return CompileResult("failed", stderr=proc.stderr.strip() or proc.stdout.strip(), command=full)
    try:
        artifacts = _parse_outputs(proc.stdout)
    except json.JSONDecodeError as exc:
        return CompileResult("failed", stderr=f"could not parse compiler output: {exc}", command=full)
    return CompileResult("passed", artifacts=artifacts, stderr=proc.stderr.strip() or None, command=full)


def _parse_outputs(stdout: str) -> dict[str, object]:
    chunks = [line for line in stdout.splitlines() if line.strip()]
    artifacts: dict[str, object] = {}
    for name, raw in zip(FORMATS, chunks, strict=False):
        artifacts[name] = json.loads(raw)
    return artifacts
