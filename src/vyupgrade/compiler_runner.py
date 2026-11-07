# This adapter runs inside provisioned compiler environments, which use Python 3.8
# for Vyper < 0.3.1. Keep it compatible with Python 3.8 syntax and standard library.
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


_VERSION_PATTERN = re.compile(r"\b\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?\b")
_MANAGED_WORKER_ARGUMENT = "--managed-compiler-worker"


def main() -> None:
    if sys.argv[1:2] == [_MANAGED_WORKER_ARGUMENT]:
        _managed_compiler_worker(sys.argv[2:])
        return
    result_path, timeout, managed_value, coherence, *command = sys.argv[1:]
    destination = Path(result_path)
    managed = managed_value == "managed"
    _write_result(destination, {"state": "started"})
    try:
        resolved_compiler, compiler_identity = _resolved_compiler(command, managed)
        packages = _resolved_packages()
    except OSError as exc:
        _write_result(
            destination,
            _failure_payload(
                origin="environment" if managed else "launch",
                completion_status="not-started",
                error=f"could not identify compiler: {exc}",
            ),
        )
        return
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        _write_result(
            destination,
            _failure_payload(
                origin="environment" if managed else "adapter",
                completion_status="not-started",
                error=f"could not identify compiler: {exc}",
            ),
        )
        return

    evidence = {
        "resolved_compiler": resolved_compiler,
        "compiler_identity": compiler_identity,
        "resolved_packages": packages,
    }
    coherence_error = _compiler_coherence_error(resolved_compiler, coherence)
    if coherence_error is not None:
        _write_result(
            destination,
            _failure_payload(
                origin="environment",
                completion_status="not-started",
                error=coherence_error,
                **evidence,
            ),
        )
        return

    try:
        process = (
            _run_managed_compiler(command, float(timeout))
            if managed and command and command[0] == "vyper"
            else _run_explicit_compiler(command, float(timeout))
        )
    except subprocess.TimeoutExpired as exc:
        _write_result(
            destination,
            _failure_payload(
                origin="timeout",
                completion_status="timed-out",
                compiler_started=True,
                stdout=_timeout_text(exc.stdout),
                stderr=_timeout_text(exc.stderr),
                error=f"compiler timed out after {timeout} seconds",
                **evidence,
            ),
        )
        return
    except OSError as exc:
        _write_result(
            destination,
            _failure_payload(
                origin="launch",
                completion_status="not-started",
                error=f"compiler failed to start: {exc}",
                **evidence,
            ),
        )
        return

    _write_result(
        destination,
        {
            "state": "complete",
            "compiler_started": True,
            "failure_origin": process["failure_origin"],
            "error_type": process.get("error_type"),
            "completion_status": process["completion_status"],
            "returncode": process["returncode"],
            "stdout": process["stdout"],
            "stderr": process["stderr"],
            **evidence,
        },
    )


def _run_managed_compiler(command: list[str], timeout: float) -> dict[str, object]:
    process = subprocess.run(
        [
            sys.executable,
            *(("-S",) if sys.flags.no_site else ()),
            str(Path(__file__).resolve()),
            _MANAGED_WORKER_ARGUMENT,
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if process.returncode != 0:
        signaled = process.returncode < 0
        return {
            "returncode": process.returncode,
            "failure_origin": "compiler-internal",
            "completion_status": "signaled" if signaled else "adapter-failed",
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        return {
            "returncode": 1,
            "failure_origin": "adapter",
            "completion_status": "adapter-failed",
            "stdout": process.stdout,
            "stderr": process.stderr or "managed compiler worker returned invalid output",
        }
    if not isinstance(result, dict):
        return {
            "returncode": 1,
            "failure_origin": "adapter",
            "completion_status": "adapter-failed",
            "stdout": process.stdout,
            "stderr": process.stderr or "managed compiler worker returned invalid output",
        }
    return result


def _managed_compiler_worker(command: list[str]) -> None:
    sys.stdout.write(json.dumps(_managed_compiler_result(command)))


def _managed_compiler_result(command: list[str]) -> dict[str, object]:
    with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout, tempfile.TemporaryFile(
        "w+", encoding="utf-8"
    ) as stderr:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            error_type = None
            try:
                vyper_compile = importlib.import_module("vyper.cli.vyper_compile")
                vyper_exception_type = importlib.import_module("vyper.exceptions").VyperException
                if (
                    command[1:3] == ["-f", "external_calls_v1"]
                    and importlib.metadata.version("vyper") == "0.3.10"
                ):
                    # Reuse this CLI's settings, import resolution and source text.
                    # The formatter lives only in this optional query's subprocess.
                    compiler = importlib.import_module("vyper.compiler")
                    compiler.OUTPUT_FORMATS["external_calls_v1"] = _external_calls_0310
                vyper_compile._parse_args(command[1:])
                returncode = 0
                origin = None
            except SystemExit as exc:
                returncode = exc.code if type(exc.code) is int else 1
                origin = None if returncode == 0 else "adapter"
            except BaseException as exc:
                if "vyper_exception_type" in locals() and isinstance(exc, vyper_exception_type):
                    origin = "compiler"
                    error_type = type(exc).__name__
                else:
                    origin = "compiler-internal"
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=stderr)
                returncode = 1
        stdout.seek(0)
        stderr.seek(0)
        return {
            "returncode": returncode,
            "failure_origin": origin,
            "error_type": error_type,
            "completion_status": "completed",
            "stdout": stdout.read(),
            "stderr": stderr.read(),
        }


def _external_calls_0310(compiler_data):
    from vyper import ast as vy_ast
    from vyper.semantics.analysis.base import FunctionVisibility
    from vyper.semantics.types.function import ContractFunctionT
    from vyper.semantics.types.user import InterfaceT

    # Vyper 0.3.10's compile_files reads with universal newlines and appends
    # exactly one newline. The caller verifies the resulting hash against the
    # validated file. This declines offsets when CRLF was normalized.
    source = compiler_data.source_code
    if not source.endswith("\n"):
        return {}
    source_sha256sum = hashlib.sha256(source[:-1].encode("utf-8")).hexdigest()
    # The regular semantic phase folds constants, including array bounds.
    # Only export calls whose ID, full span and method survive that transformation.
    original = {
        (node.node_id, node.src, node.func.attr)
        for node in compiler_data.vyper_module.get_descendants(vy_ast.Call)
        if isinstance(node.func, vy_ast.Attribute)
    }
    calls = []
    for node in compiler_data.vyper_module_folded.get_descendants(vy_ast.Call):
        callee = node.func
        if (
            not isinstance(callee, vy_ast.Attribute)
            or (node.node_id, node.src, callee.attr) not in original
        ):
            continue
        function = callee._metadata.get("type")
        interface = callee.value._metadata.get("type")
        if (
            isinstance(function, ContractFunctionT)
            and function.visibility is FunctionVisibility.EXTERNAL
            and isinstance(interface, InterfaceT)
            and function.name == callee.attr
        ):
            calls.append(
                {
                    "node_id": node.node_id,
                    "src": node.src,
                    "method": callee.attr,
                    "interface": str(interface),
                    "mutability": function.mutability.value,
                }
            )
    return {"source_sha256sum": source_sha256sum, "calls": calls}


def _run_explicit_compiler(command: list[str], timeout: float) -> dict[str, object]:
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    signaled = process.returncode < 0
    return {
        "returncode": process.returncode,
        "failure_origin": (
            "compiler-internal" if signaled else "adapter" if process.returncode != 0 else None
        ),
        "completion_status": "signaled" if signaled else "completed",
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


def _resolved_compiler(command: list[str], managed: bool) -> tuple[str, dict[str, object]]:
    if managed:
        version = importlib.metadata.version("vyper")
    else:
        process = subprocess.run(
            [command[0], "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if process.returncode != 0:
            raise RuntimeError(
                process.stderr.strip() or process.stdout.strip() or "version query failed"
            )
        match = _VERSION_PATTERN.search(process.stdout)
        if match is None:
            raise RuntimeError("version query returned no version")
        version = match.group(0)
    executable = (
        _managed_executable_identity("vyper")
        if managed
        else _file_identity(_resolved_executable(command[0]))
    )
    artifact = _compiler_artifact_identity() if managed else executable
    return version, {
        "version": version,
        "executable": executable,
        "artifact": artifact,
    }


def _resolved_executable(command: str) -> Path:
    resolved = shutil.which(command)
    if resolved is None:
        return Path(command).resolve(strict=True)
    return Path(resolved).resolve(strict=True)


def _managed_executable_identity(command: str) -> dict[str, str]:
    path = _resolved_executable(command)
    lines = path.read_bytes().splitlines(keepends=True)
    if lines and lines[0].startswith(b"#!"):
        lines[0] = b"#!python\n"
    return {
        "path": command,
        "sha256": hashlib.sha256(b"".join(lines)).hexdigest(),
    }


def _compiler_artifact_identity() -> dict[str, str]:
    distribution = importlib.metadata.distribution("vyper")
    artifact = _distribution_artifact(distribution)
    if artifact is None:
        raise RuntimeError("vyper distribution has no RECORD or METADATA artifact")
    return artifact


def _resolved_packages() -> list[dict[str, str | None]]:
    packages: dict[tuple[str, str, str | None, str], dict[str, str | None]] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        artifact = _distribution_artifact(distribution)
        if artifact is None:
            continue
        source = _canonical_json(distribution.read_text("direct_url.json"))
        identity = (name.lower(), distribution.version, source, artifact["sha256"])
        packages[identity] = {
            "name": name,
            "version": distribution.version,
            "source": source,
            "artifact_sha256": artifact["sha256"],
        }
    return [packages[identity] for identity in sorted(packages, key=repr)]


def _distribution_artifact(
    distribution: importlib.metadata.Distribution,
) -> dict[str, str] | None:
    files = distribution.files or ()
    for suffix in (".dist-info/RECORD", ".dist-info/METADATA"):
        entry = next((file for file in files if str(file).endswith(suffix)), None)
        if entry is None:
            continue
        path = Path(distribution.locate_file(entry)).resolve()
        if path.is_file():
            return _file_identity(path)
    return None


def _canonical_json(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"))
    except json.JSONDecodeError:
        return value


def _file_identity(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _compiler_coherence_error(version: str, raw_evidence: str) -> str | None:
    if not raw_evidence:
        return None
    try:
        evidence = json.loads(raw_evidence)
    except json.JSONDecodeError:
        return "compiler coherence evidence is malformed"
    if not isinstance(evidence, dict):
        return "compiler coherence evidence is malformed"
    declaration = evidence.get("declaration")
    versions = evidence.get("versions")
    if (
        not isinstance(declaration, str)
        or not isinstance(versions, list)
        or not all(isinstance(candidate, str) for candidate in versions)
    ):
        return "compiler coherence evidence is malformed"
    # Vyper release executables report versions such as
    # ``0.4.1+commit.8a93dd27`` while pragmas name the public ``0.4.1`` release.
    public_version = version.partition("+")[0]
    if public_version in {candidate.partition("+")[0] for candidate in versions}:
        return None
    return f"resolved project compiler {version} conflicts with source declaration {declaration}"


def _failure_payload(
    *,
    origin: str,
    completion_status: str,
    compiler_started: bool = False,
    error: str,
    **evidence: object,
) -> dict[str, object]:
    return {
        "state": "complete",
        "compiler_started": compiler_started,
        "failure_origin": origin,
        "completion_status": completion_status,
        "returncode": None,
        "error": error,
        **evidence,
    }


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _write_result(path: Path, result: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(result), encoding="utf-8")
    os.replace(temporary, path)


if __name__ == "__main__":
    main()
