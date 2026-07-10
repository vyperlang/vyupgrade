#!/usr/bin/env python3
from __future__ import annotations

import argparse
import tarfile
import tomllib
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

if __package__:
    from .release_notes import changelog_release_notes
else:
    from release_notes import changelog_release_notes


def release_preflight(
    tag: str,
    *,
    pyproject_path: Path = Path("pyproject.toml"),
    lock_path: Path = Path("uv.lock"),
    changelog_path: Path = Path("CHANGELOG.md"),
    dist_path: Path | None = None,
) -> str:
    pyproject = _read_toml(pyproject_path)
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"{pyproject_path} does not contain a [project] table")
    name = _required_string(project, "name", pyproject_path)
    version = _required_string(project, "version", pyproject_path)

    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ValueError(f"tag {tag!r} does not match project version {version!r} ({expected_tag})")

    changelog = changelog_path.read_text(encoding="utf-8")
    notes = changelog_release_notes(changelog, tag)
    _validate_lock_version(_read_toml(lock_path), lock_path, name, version)
    if dist_path is not None:
        _validate_distribution(dist_path, name, version)
    return notes


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _required_string(table: dict[str, Any], key: str, path: Path) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} project {key} must be a nonempty string")
    return value


def _validate_lock_version(lock: dict[str, Any], path: Path, name: str, version: str) -> None:
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError(f"{path} does not contain package entries")
    project_name = canonicalize_name(name)
    projects = [
        package
        for package in packages
        if isinstance(package, dict)
        and canonicalize_name(str(package.get("name", ""))) == project_name
        and isinstance(package.get("source"), dict)
        and package["source"].get("editable") == "."
    ]
    if len(projects) != 1:
        raise ValueError(f"{path} must contain one editable {name!r} project package")
    lock_version = projects[0].get("version")
    if lock_version != version:
        raise ValueError(
            f"{path} project version {lock_version!r} does not match {version!r}"
        )


def _validate_distribution(dist_path: Path, name: str, version: str) -> None:
    if not dist_path.is_dir():
        raise ValueError(f"distribution directory does not exist: {dist_path}")
    files = sorted(path for path in dist_path.iterdir() if path.is_file())
    wheels = [path for path in files if path.suffix == ".whl"]
    sdists = [path for path in files if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"{dist_path} must contain exactly one wheel and one .tar.gz sdist "
            f"(found {len(wheels)} wheel(s), {len(sdists)} sdist(s))"
        )

    wheel_name, wheel_version, _build, _tags = parse_wheel_filename(wheels[0].name)
    _validate_artifact_identity(wheels[0], wheel_name, wheel_version, name, version)
    _validate_metadata(wheels[0], _wheel_metadata(wheels[0]), name, version)

    sdist_name, sdist_version = parse_sdist_filename(sdists[0].name)
    _validate_artifact_identity(sdists[0], sdist_name, sdist_version, name, version)
    _validate_metadata(sdists[0], _sdist_metadata(sdists[0]), name, version)


def _validate_artifact_identity(
    path: Path, artifact_name: str, artifact_version: Version, name: str, version: str
) -> None:
    if canonicalize_name(artifact_name) != canonicalize_name(name):
        raise ValueError(f"artifact filename has the wrong project name: {path.name}")
    if artifact_version != Version(version):
        raise ValueError(f"artifact filename has the wrong project version: {path.name}")


def _wheel_metadata(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(members) != 1:
            raise ValueError(f"{path} must contain exactly one .dist-info/METADATA file")
        return archive.read(members[0])


def _sdist_metadata(path: Path) -> bytes:
    with tarfile.open(path, "r:gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.isfile() and Path(member.name).name == "PKG-INFO"
        ]
        if len(members) != 1:
            raise ValueError(f"{path} must contain exactly one PKG-INFO file")
        file = archive.extractfile(members[0])
        if file is None:
            raise ValueError(f"could not read metadata from {path}")
        return file.read()


def _validate_metadata(path: Path, payload: bytes, name: str, version: str) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(payload)
    metadata_name = metadata.get("Name")
    metadata_version = metadata.get("Version")
    if not metadata_name or canonicalize_name(metadata_name) != canonicalize_name(name):
        raise ValueError(f"{path} metadata project name {metadata_name!r} does not match {name!r}")
    if metadata_version != version:
        raise ValueError(
            f"{path} metadata version {metadata_version!r} does not match {version!r}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate release metadata and optionally built distributions"
    )
    parser.add_argument("tag")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--notes-output", type=Path)
    args = parser.parse_args(argv)

    try:
        notes = release_preflight(
            args.tag,
            pyproject_path=args.pyproject,
            lock_path=args.lock,
            changelog_path=args.changelog,
            dist_path=args.dist,
        )
        if args.notes_output is None:
            print(notes, end="")
        else:
            args.notes_output.parent.mkdir(parents=True, exist_ok=True)
            args.notes_output.write_text(notes, encoding="utf-8")
            print(f"release preflight passed for {args.tag}")
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"release preflight failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
