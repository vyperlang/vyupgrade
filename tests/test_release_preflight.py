from __future__ import annotations

import io
import importlib.util
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


def _load_release_preflight_module():
    script = Path(__file__).parents[1] / "scripts" / "release_preflight.py"
    spec = importlib.util.spec_from_file_location("vyupgrade_release_preflight_script", script)
    assert spec is not None
    assert spec.loader is not None
    sys.path.insert(0, str(script.parent))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


release_preflight = _load_release_preflight_module()


def _release_fixture(
    root: Path,
    *,
    version: str = "1.2.3",
    lock_version: str | None = None,
    wheel_filename_version: str | None = None,
    wheel_metadata_version: str | None = None,
) -> tuple[Path, Path, Path, Path]:
    pyproject = root / "pyproject.toml"
    lock = root / "uv.lock"
    changelog = root / "CHANGELOG.md"
    dist = root / "dist"
    pyproject.write_text(
        f'[project]\nname = "vyupgrade"\nversion = "{version}"\n', encoding="utf-8"
    )
    lock.write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[[package]]",
                'name = "vyupgrade"',
                f'version = "{lock_version or version}"',
                'source = { editable = "." }',
                "",
            ]
        ),
        encoding="utf-8",
    )
    changelog.write_text(
        f"# Changelog\n\n## {version} - 2026-07-10\n\n- Shipped safely.\n",
        encoding="utf-8",
    )
    dist.mkdir()

    wheel_version = wheel_filename_version or version
    wheel = dist / f"vyupgrade-{wheel_version}-py3-none-any.whl"
    wheel_metadata = (
        "Metadata-Version: 2.4\n"
        "Name: vyupgrade\n"
        f"Version: {wheel_metadata_version or version}\n\n"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"vyupgrade-{wheel_version}.dist-info/METADATA", wheel_metadata.encode()
        )

    sdist = dist / f"vyupgrade-{version}.tar.gz"
    sdist_metadata = (
        f"Metadata-Version: 2.4\nName: vyupgrade\nVersion: {version}\n\n"
    ).encode()
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo(f"vyupgrade-{version}/PKG-INFO")
        member.size = len(sdist_metadata)
        archive.addfile(member, io.BytesIO(sdist_metadata))
    return pyproject, lock, changelog, dist


def test_release_preflight_validates_metadata_and_returns_notes(tmp_path: Path) -> None:
    pyproject, lock, changelog, dist = _release_fixture(tmp_path)

    notes = release_preflight.release_preflight(
        "v1.2.3",
        pyproject_path=pyproject,
        lock_path=lock,
        changelog_path=changelog,
        dist_path=dist,
    )

    assert notes == "- Shipped safely.\n"


def test_release_preflight_rejects_tag_and_lock_version_drift(tmp_path: Path) -> None:
    pyproject, lock, changelog, dist = _release_fixture(tmp_path, lock_version="1.2.2")

    with pytest.raises(ValueError, match=r"tag .* does not match project version"):
        release_preflight.release_preflight(
            "v1.2.2",
            pyproject_path=pyproject,
            lock_path=lock,
            changelog_path=changelog,
            dist_path=dist,
        )

    with pytest.raises(ValueError, match=r"project version '1.2.2' does not match '1.2.3'"):
        release_preflight.release_preflight(
            "v1.2.3",
            pyproject_path=pyproject,
            lock_path=lock,
            changelog_path=changelog,
            dist_path=dist,
        )


def test_release_preflight_rejects_artifact_filename_version_drift(tmp_path: Path) -> None:
    pyproject, lock, changelog, dist = _release_fixture(
        tmp_path, wheel_filename_version="1.2.2"
    )

    with pytest.raises(ValueError, match="filename has the wrong project version"):
        release_preflight.release_preflight(
            "v1.2.3",
            pyproject_path=pyproject,
            lock_path=lock,
            changelog_path=changelog,
            dist_path=dist,
        )


def test_release_preflight_rejects_artifact_metadata_version_drift(tmp_path: Path) -> None:
    pyproject, lock, changelog, dist = _release_fixture(
        tmp_path, wheel_metadata_version="1.2.2"
    )

    with pytest.raises(
        ValueError, match=r"metadata version '1.2.2' does not match '1.2.3'"
    ):
        release_preflight.release_preflight(
            "v1.2.3",
            pyproject_path=pyproject,
            lock_path=lock,
            changelog_path=changelog,
            dist_path=dist,
        )


def test_release_preflight_cli_writes_validated_notes(tmp_path: Path) -> None:
    pyproject, lock, changelog, dist = _release_fixture(tmp_path)
    notes_path = tmp_path / "artifacts" / "release-notes.md"

    exit_code = release_preflight.main(
        [
            "v1.2.3",
            "--pyproject",
            str(pyproject),
            "--lock",
            str(lock),
            "--changelog",
            str(changelog),
            "--dist",
            str(dist),
            "--notes-output",
            str(notes_path),
        ]
    )

    assert exit_code == 0
    assert notes_path.read_text(encoding="utf-8") == "- Shipped safely.\n"
