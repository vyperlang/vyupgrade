"""Typed, strict defaults loaded from ``[tool.vyupgrade]``."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
import tomllib

from .models import DEFAULT_COMPILER_TIMEOUT_SECONDS, DEFAULT_NETWORK_TIMEOUT_SECONDS


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    paths: tuple[str, ...] = ()
    target_version: str = "0.4.3"
    source_version: str | None = None
    strip_pragma: bool = False
    report_json: str | None = None
    aggressive: bool = False
    include_dependencies: bool = False
    closure_output: str | None = None
    closure_archive: str | None = None
    source_python: str | None = None
    target_python: str | None = None
    compiler_search_paths: tuple[str, ...] = ()
    compiler_timeout: float = DEFAULT_COMPILER_TIMEOUT_SECONDS
    network_timeout: float = DEFAULT_NETWORK_TIMEOUT_SECONDS
    split_interfaces: bool = False
    format: str = "none"
    allow_unvalidated_source: bool = False
    allow_abi_change: bool = False
    allow_method_id_change: bool = False
    allow_storage_layout_change: bool = False


def load_project_config(path: Path, *, required: bool = False) -> ProjectConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return ProjectConfig()
        raise ConfigurationError(f"configuration file does not exist: {path}") from None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"could not read configuration {path}: {exc}") from exc
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise ConfigurationError("tool must be a TOML table")
    raw = tool.get("vyupgrade", {})
    if not isinstance(raw, dict):
        raise ConfigurationError("tool.vyupgrade must be a TOML table")
    schema = {field.name.replace("_", "-"): field for field in fields(ProjectConfig)}
    values = {}
    for key, value in raw.items():
        normalized = value
        if key not in schema:
            raise ConfigurationError(f"unknown tool.vyupgrade option: {key}")
        field = schema[key]
        default = field.default
        if isinstance(default, bool):
            valid = type(value) is bool
            expected = "a boolean (true or false)"
        elif isinstance(default, tuple):
            valid = isinstance(value, list) and all(
                isinstance(item, str) and item.strip() for item in value
            )
            expected = "an array of nonempty strings"
            if valid:
                normalized = tuple(value)
        elif isinstance(default, (int, float)):
            valid = type(value) in {int, float}
            expected = "a number of seconds"
        else:
            valid = isinstance(value, str) and bool(value.strip())
            expected = "a nonempty string"
        if not valid:
            raise ConfigurationError(f"tool.vyupgrade.{key} must be {expected}")
        values[field.name] = normalized
    config = ProjectConfig(**values)
    if config.format not in {"none", "mamushi"}:
        raise ConfigurationError("tool.vyupgrade.format must be 'none' or 'mamushi'")
    return config
