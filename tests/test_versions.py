from __future__ import annotations

from vyupgrade.versions import (
    MigrationContext,
    VyperVersion,
    compiler_version_for_spec,
    is_supported_source_version,
    known_versions_satisfying,
    minimum_satisfying_version,
)


def test_known_versions_cover_full_supported_range() -> None:
    assert is_supported_source_version("0.2.1")
    assert is_supported_source_version("0.4.3")
    assert not is_supported_source_version("0.2.0")
    assert not is_supported_source_version("0.4.4")
    assert VyperVersion(0, 2, 1) in known_versions_satisfying(">=0.2.1,<0.2.3")
    assert VyperVersion(0, 4, 3) in known_versions_satisfying(">=0.4.0")


def test_version_specs_pick_lowest_satisfying_source_floor() -> None:
    assert minimum_satisfying_version("^0.3.10") == VyperVersion(0, 3, 10)
    assert minimum_satisfying_version(">=0.3.4,<0.4.0") == VyperVersion(0, 3, 4)
    assert minimum_satisfying_version(">0.3.10") == VyperVersion(0, 4, 0)
    assert compiler_version_for_spec("<=0.3.10") == "0.3.10"


def test_migration_context_tracks_patch_level_crossings() -> None:
    older = MigrationContext.from_specs("0.4.1", "0.4.3")
    current = MigrationContext.from_specs("0.4.2", "0.4.3")
    below_target = MigrationContext.from_specs("0.3.10", "0.4.1")

    assert older.crosses("0.4.2")
    assert not current.crosses("0.4.2")
    assert not below_target.crosses("0.4.2")
