from __future__ import annotations

from importlib.metadata import version

from vyupgrade import __version__
from vyupgrade.versions import (
    LEGACY_PRERELEASE_VERSIONS,
    MigrationContext,
    VyperVersion,
    compiler_version_for_source,
    compiler_version_for_spec,
    default_evm_version_for_spec,
    is_supported_source_version,
    known_versions_satisfying,
    minimum_satisfying_version,
)


def test_package_version_is_exposed() -> None:
    assert __version__ == version("vyupgrade")


def test_known_versions_cover_full_supported_range() -> None:
    assert all(is_supported_source_version(version) for version in LEGACY_PRERELEASE_VERSIONS)
    assert is_supported_source_version("0.2.1")
    assert is_supported_source_version("0.4.3")
    assert is_supported_source_version("0.5.0a1")
    assert is_supported_source_version("0.5.0a2")
    assert not is_supported_source_version("0.1.0")
    assert not is_supported_source_version("0.1.0b18")
    assert not is_supported_source_version("0.1.0b99")
    assert not is_supported_source_version("0.2.0")
    assert not is_supported_source_version("0.4.4")
    assert not is_supported_source_version("0.5.0")
    assert VyperVersion("0.1.0b1") in known_versions_satisfying(">=0.1.0b1,<0.2.1")
    assert VyperVersion("0.2.1") in known_versions_satisfying(">=0.2.1,<0.2.3")
    assert VyperVersion("0.4.3") in known_versions_satisfying(">=0.4.0")
    assert VyperVersion("0.5.0a2") in known_versions_satisfying(">=0.5.0a1,<0.5.0")


def test_version_specs_pick_lowest_satisfying_source_floor() -> None:
    for legacy_version in LEGACY_PRERELEASE_VERSIONS:
        assert compiler_version_for_spec(legacy_version) == legacy_version
    assert minimum_satisfying_version("^0.3.10") == VyperVersion("0.3.10")
    assert minimum_satisfying_version(">=0.1.0b4,<0.2.1") == VyperVersion("0.1.0b4")
    assert minimum_satisfying_version(">=0.3.4,<0.4.0") == VyperVersion("0.3.4")
    assert minimum_satisfying_version(">0.3.10") == VyperVersion("0.4.0")
    assert compiler_version_for_spec("<=0.3.10") == "0.3.10"
    assert compiler_version_for_spec(">=0.5.0a1,<0.5.0") == "0.5.0a1"
    assert compiler_version_for_spec("<=0.5.0a2") == "0.5.0a2"


def test_source_syntax_hints_raise_broad_pragma_compiler_floor() -> None:
    assert compiler_version_for_source("^0.3.0", "xs: DynArray[String[32], 100]") == "0.3.4"
    assert compiler_version_for_source("^0.3.0", "xs: DynArray[Reward, MAX_REWARDS]") == "0.3.7"
    assert compiler_version_for_source("^0.3.3", "TOKEN: immutable(address)") == "0.3.7"
    assert compiler_version_for_source(">=0.3.2", "enum Side:\n    BUY\n") == "0.3.4"
    assert compiler_version_for_source("^0.3.0", "decimals: public(uint8)") == "0.3.4"
    assert compiler_version_for_source("^0.3.0", "send(self.owner, fee, gas=msg.gas)") == "0.3.8"
    assert compiler_version_for_source(">=0.3.8,<0.4.0", "TOKEN: immutable(address)") == "0.3.8"
    assert compiler_version_for_source(">=0.3.0,<0.3.4", "enum Side:\n    BUY\n") == "0.3.0"


def test_migration_context_tracks_patch_level_crossings() -> None:
    older = MigrationContext.from_specs("0.4.1", "0.4.3")
    current = MigrationContext.from_specs("0.4.2", "0.4.3")
    below_target = MigrationContext.from_specs("0.3.10", "0.4.1")

    assert older.crosses("0.4.2")
    assert not current.crosses("0.4.2")
    assert not below_target.crosses("0.4.2")
    assert MigrationContext.from_specs("0.1.0b17", "0.2.1").crosses("0.2.1")


def test_default_evm_versions_track_vyper_release_defaults() -> None:
    assert default_evm_version_for_spec("0.1.0b16") == "istanbul"
    assert default_evm_version_for_spec("0.2.4") == "istanbul"
    assert default_evm_version_for_spec("0.2.12") == "berlin"
    assert default_evm_version_for_spec("0.3.7") == "paris"
    assert default_evm_version_for_spec("0.3.10") == "shanghai"
    assert default_evm_version_for_spec("0.4.2") == "cancun"
    assert default_evm_version_for_spec("0.4.3") == "prague"
