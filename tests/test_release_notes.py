from __future__ import annotations

import importlib.util
from pathlib import Path


def load_release_notes_module():
    path = Path("scripts/release_notes.py")
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_changelog_release_notes_extracts_matching_tag_section() -> None:
    module = load_release_notes_module()
    changelog = """# Changelog

## 0.3.1

- Fixed the release.

## 0.3.0

- Added the feature.
"""

    assert module.changelog_release_notes(changelog, "v0.3.1") == "- Fixed the release.\n"


def test_changelog_release_notes_accepts_prefixed_headings() -> None:
    module = load_release_notes_module()
    changelog = """# Changelog

## [v0.3.1] - 2026-05-31

- Fixed the release.
"""

    assert module.changelog_release_notes(changelog, "v0.3.1") == "- Fixed the release.\n"


def test_changelog_release_notes_rejects_missing_tag() -> None:
    module = load_release_notes_module()
    changelog = """# Changelog

## 0.3.0

- Added the feature.
"""

    try:
        module.changelog_release_notes(changelog, "v0.3.1")
    except ValueError as exc:
        assert "v0.3.1" in str(exc)
    else:
        raise AssertionError("expected missing changelog section to fail")


def test_changelog_release_notes_rejects_empty_section() -> None:
    module = load_release_notes_module()
    changelog = """# Changelog

## 0.3.1

## 0.3.0

- Added the feature.
"""

    try:
        module.changelog_release_notes(changelog, "v0.3.1")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected empty changelog section to fail")
