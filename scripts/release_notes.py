#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


HEADING = re.compile(r"^##\s+\[?(?P<version>v?[^\]\s]+)\]?(?:\s+-\s+.*)?$")


def changelog_release_notes(changelog: str, tag: str) -> str:
    target = tag.removeprefix("v")
    lines = changelog.splitlines()
    start: int | None = None
    end = len(lines)

    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match is None:
            continue
        version = match.group("version").removeprefix("v")
        if start is not None:
            end = index
            break
        if version == target:
            start = index + 1

    if start is None:
        msg = f"CHANGELOG.md does not contain release notes for {tag}"
        raise ValueError(msg)

    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        msg = f"CHANGELOG.md release notes for {tag} are empty"
        raise ValueError(msg)
    return f"{notes}\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not 1 <= len(args) <= 2:
        print("usage: release_notes.py <tag> [CHANGELOG.md]", file=sys.stderr)
        return 2

    tag = args[0]
    changelog_path = Path(args[1]) if len(args) == 2 else Path("CHANGELOG.md")
    try:
        sys.stdout.write(changelog_release_notes(changelog_path.read_text(encoding="utf-8"), tag))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
