from __future__ import annotations

from pathlib import Path


DEFAULT_INCLUDE = {".vy", ".vyi"}
DEFAULT_EXCLUDES = {".git", ".venv", "venv", "build", "dist", "__pycache__"}


def discover_files(paths: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix in DEFAULT_INCLUDE:
                files.append(path)
            continue

        if not path.exists():
            raise FileNotFoundError(path)

        for child in path.rglob("*"):
            if any(part in DEFAULT_EXCLUDES for part in child.parts):
                continue
            if child.is_file() and child.suffix in DEFAULT_INCLUDE:
                files.append(child)

    return sorted(dict.fromkeys(file.resolve() for file in files))

