from __future__ import annotations

import re
from dataclasses import dataclass


PRAGMA_RE = re.compile(r"^\s*#\s*(?:@version|pragma\s+version)\s+(.+?)\s*$", re.MULTILINE)
VERSION_RE = re.compile(r"0\.(?:2|3|4)\.\d+")
# PyPI has no final 0.1.0 release; these are the installable Vyper releases before 0.2.1.
LEGACY_PRERELEASE_VERSIONS = tuple(f"0.1.0b{number}" for number in range(1, 18))
LEGACY_PRERELEASE_RE = re.compile(r"0\.1\.0b(?:[1-9]|1[0-7])\b")


@dataclass(frozen=True, order=True)
class VyperVersion:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


KNOWN_VERSIONS = tuple(
    VyperVersion(0, minor, patch)
    for minor, last_patch in ((2, 16), (3, 10), (4, 3))
    for patch in range(1 if minor == 2 else 0, last_patch + 1)
)


@dataclass(frozen=True)
class MigrationContext:
    source_spec: str | None
    target_spec: str
    source_floor: VyperVersion | None
    target_version: VyperVersion

    @classmethod
    def from_specs(cls, source_spec: str | None, target_spec: str) -> "MigrationContext":
        target = parse_version(compiler_version_for_spec(target_spec)) or parse_version(target_spec)
        if target is None:
            target = VyperVersion(0, 4, 3)
        return cls(
            source_spec=source_spec,
            target_spec=target_spec,
            source_floor=minimum_satisfying_version(source_spec),
            target_version=target,
        )

    def target_at_least(self, version: str | VyperVersion) -> bool:
        return self.target_version >= ensure_version(version)

    def crosses(self, version: str | VyperVersion) -> bool:
        introduced = ensure_version(version)
        if self.target_version < introduced:
            return False
        if self.source_floor is None:
            return True
        return self.source_floor < introduced


def infer_pragma(source: str) -> str | None:
    match = PRAGMA_RE.search(source)
    return match.group(1).strip() if match else None


def parse_version(raw: str | None) -> VyperVersion | None:
    if raw is None:
        return None
    match = VERSION_RE.search(raw)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.group(0).split("."))
    return VyperVersion(major, minor, patch)


def ensure_version(raw: str | VyperVersion) -> VyperVersion:
    if isinstance(raw, VyperVersion):
        return raw
    version = parse_version(raw)
    if version is None:
        raise ValueError(f"unsupported Vyper version: {raw!r}")
    return version


def minimum_satisfying_version(spec: str | None) -> VyperVersion | None:
    if not spec:
        return None
    versions = known_versions_satisfying(spec)
    if versions:
        return versions[0]
    return parse_version(spec)


def compiler_version_for_spec(spec: str | None) -> str | None:
    legacy = legacy_prerelease_version(spec)
    if legacy is not None:
        return legacy
    versions = known_versions_satisfying(spec)
    if versions:
        return str(versions[0] if _has_lower_bound(spec or "") else versions[-1])
    version = parse_version(spec)
    return str(version) if version else None


def default_evm_version_for_spec(spec: str | None) -> str | None:
    compiler_version = compiler_version_for_spec(spec)
    if legacy_prerelease_version(compiler_version) is not None:
        return "istanbul"
    version = parse_version(compiler_version)
    return default_evm_version(version)


def default_evm_version(version: VyperVersion | None) -> str | None:
    if version is None:
        return None
    if version < VyperVersion(0, 2, 12):
        return "istanbul"
    if version < VyperVersion(0, 3, 7):
        return "berlin"
    if version < VyperVersion(0, 3, 8):
        return "paris"
    if version < VyperVersion(0, 4, 0):
        return "shanghai"
    if version < VyperVersion(0, 4, 3):
        return "cancun"
    return "prague"


def known_versions_satisfying(spec: str | None) -> tuple[VyperVersion, ...]:
    if not spec:
        return ()
    clauses = _parse_clauses(spec)
    if not clauses:
        version = parse_version(spec)
        return (version,) if version else ()
    return tuple(version for version in KNOWN_VERSIONS if all(_satisfies(version, op, bound) for op, bound in clauses))


def is_supported_source_version(version: str | None) -> bool:
    if legacy_prerelease_version(version) is not None:
        return True
    parsed = parse_version(version)
    return bool(known_versions_satisfying(version) or (parsed and parsed in KNOWN_VERSIONS))


def legacy_prerelease_version(spec: str | None) -> str | None:
    if spec is None:
        return None
    match = LEGACY_PRERELEASE_RE.search(spec)
    return match.group(0) if match else None


def _parse_clauses(spec: str) -> list[tuple[str, VyperVersion]]:
    clauses: list[tuple[str, VyperVersion]] = []
    for match in re.finditer(r"(?P<op>\^|==|!=|<=|>=|<|>|=)?\s*(?P<version>0\.(?:2|3|4)\.\d+)", spec):
        op = match.group("op") or "=="
        version = ensure_version(match.group("version"))
        if op == "^":
            clauses.append((">=", version))
            clauses.append(("<", _caret_upper_bound(version)))
        elif op == "=":
            clauses.append(("==", version))
        else:
            clauses.append((op, version))
    return clauses


def _has_lower_bound(spec: str) -> bool:
    return any(op in {">=", ">", "=="} for op, _version in _parse_clauses(spec))


def _caret_upper_bound(version: VyperVersion) -> VyperVersion:
    # Vyper versions in this tool are all 0.x. The useful historical caret
    # range is therefore the current minor line.
    return VyperVersion(version.major, version.minor + 1, 0)


def _satisfies(version: VyperVersion, op: str, bound: VyperVersion) -> bool:
    if op == "==":
        return version == bound
    if op == "!=":
        return version != bound
    if op == ">=":
        return version >= bound
    if op == ">":
        return version > bound
    if op == "<=":
        return version <= bound
    if op == "<":
        return version < bound
    raise ValueError(f"unsupported version operator: {op}")
