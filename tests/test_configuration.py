from pathlib import Path

import pytest

from vyupgrade.cli import main
from vyupgrade.configuration import ConfigurationError, ProjectConfig, load_project_config


@pytest.mark.parametrize(
    "key",
    [
        "allow-unvalidated-source",
        "allow-abi-change",
        "allow-method-id-change",
        "allow-storage-layout-change",
        "aggressive",
        "include-dependencies",
        "strip-pragma",
        "split-interfaces",
    ],
)
def test_quoted_false_cannot_enable_boolean_options(tmp_path: Path, capsys, key: str) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(f'[tool.vyupgrade]\n{key} = "false"\n')
    contract = tmp_path / "contract.vy"
    original = "#pragma version 0.4.3\n"
    contract.write_text(original)
    assert main([str(contract), "--write", "--config", str(config)]) == 4
    assert f"{key} must be a boolean" in capsys.readouterr().err
    assert contract.read_text() == original


@pytest.mark.parametrize(
    "entry",
    [
        'paths = "contracts"',
        "paths = [1]",
        'compiler-search-paths = [""]',
        "source-version = []",
        "target-version = 4",
        "report-json = true",
        "allow-abi-change = 1",
        "network-timeout = true",
        'compiler-timeout = "120"',
        'format = "unknown"',
        'target-python = ""',
        "allow-abi-changes = true",
    ],
)
def test_invalid_config_is_a_usage_error(tmp_path: Path, capsys, entry: str) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(f"[tool.vyupgrade]\n{entry}\n")
    assert main(["--config", str(config)]) == 4
    assert "tool.vyupgrade" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["[tool.vyupgrade", "tool = []", "[tool]\nvyupgrade = false"])
def test_malformed_configuration_is_not_silently_ignored(tmp_path: Path, text: str) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(text)
    with pytest.raises(ConfigurationError):
        load_project_config(config)


def test_explicit_missing_configuration_is_an_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "missing.toml"
    assert load_project_config(path) == ProjectConfig()
    assert main(["--config", str(path)]) == 4
    assert "does not exist" in capsys.readouterr().err


def test_typed_configuration_preserves_false_and_numeric_budgets(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("""[tool.vyupgrade]
paths = ["contracts/"]
allow-abi-change = false
aggressive = true
compiler-timeout = 1.5
compiler-search-paths = ["lib/"]
source-version = "infer"
""")
    config = load_project_config(path)
    assert config.paths == ("contracts/",)
    assert config.allow_abi_change is False
    assert config.aggressive is True
    assert config.compiler_timeout == 1.5
    assert config.compiler_search_paths == ("lib/",)
    assert config.source_version == "infer"
