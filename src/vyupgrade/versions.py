from __future__ import annotations

import re


PRAGMA_RE = re.compile(r"^\s*#\s*(?:@version|pragma\s+version)\s+(.+?)\s*$", re.MULTILINE)


def infer_pragma(source: str) -> str | None:
    match = PRAGMA_RE.search(source)
    return match.group(1).strip() if match else None


def is_supported_source_version(version: str | None) -> bool:
    if version is None:
        return False
    return "0.3." in version or "0.4." in version

