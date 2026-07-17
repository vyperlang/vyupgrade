from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path


_VERSION_PATTERN = re.compile(r"\b\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?\b")


def main() -> None:
    result_path, timeout, managed, *command = sys.argv[1:]
    destination = Path(result_path)
    _write_result(destination, {"state": "started"})
    try:
        resolved_compiler = _resolved_compiler(command, managed == "managed")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        _write_result(
            destination,
            {
                "state": "complete",
                "compiler_started": False,
                "failure_origin": "launch",
                "resolved_compiler": None,
                "error": f"could not identify compiler: {exc}",
            },
        )
        return

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=float(timeout),
        )
    except subprocess.TimeoutExpired as exc:
        _write_result(
            destination,
            {
                "state": "complete",
                "compiler_started": True,
                "failure_origin": "timeout",
                "resolved_compiler": resolved_compiler,
                "stdout": _timeout_text(exc.stdout),
                "stderr": _timeout_text(exc.stderr),
                "error": f"compiler timed out after {timeout} seconds",
            },
        )
        return
    except OSError as exc:
        _write_result(
            destination,
            {
                "state": "complete",
                "compiler_started": False,
                "failure_origin": "launch",
                "resolved_compiler": resolved_compiler,
                "error": f"compiler failed to start: {exc}",
            },
        )
        return

    _write_result(
        destination,
        {
            "state": "complete",
            "compiler_started": True,
            "failure_origin": "compiler" if process.returncode != 0 else None,
            "resolved_compiler": resolved_compiler,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        },
    )


def _resolved_compiler(command: list[str], managed: bool) -> str:
    if managed:
        return importlib.metadata.version("vyper")
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
    return match.group(0)


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
