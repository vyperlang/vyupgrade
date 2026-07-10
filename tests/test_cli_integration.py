from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vyupgrade.cli import main
from vyupgrade.compiler import (
    CompileResult,
    compare_artifact_details,
    compare_artifacts,
    compile_source_file,
    compile_target_source,
    unavailable_validation_artifacts,
)
from vyupgrade.models import Config


def test_real_compiler_nested_module_layout_is_comparable(tmp_path: Path) -> None:
    module = tmp_path / "ledger.vy"
    module.write_text(
        """#pragma version 0.4.3
owner: public(address)

@deploy
def __init__():
    self.owner = msg.sender
""",
        encoding="utf-8",
    )
    contract = tmp_path / "main.vy"
    source = """#pragma version 0.4.3
import ledger

initializes: ledger
exports: ledger.__interface__

@deploy
def __init__():
    ledger.__init__()
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(
        paths=(contract,),
        target_version="0.4.3",
        compiler_search_paths=(tmp_path,),
    )

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    assert layout["storage_layout"] == {
        "ledger": {"owner": {"type": "address", "n_slots": 1, "slot": 0}}
    }


def test_real_compiler_code_only_immutable_layout_is_empty_storage(tmp_path: Path) -> None:
    contract = tmp_path / "immutable_only.vy"
    source = """#pragma version 0.4.3
OWNER: immutable(address)

@deploy
def __init__():
    OWNER = msg.sender

@view
@external
def owner() -> address:
    return OWNER
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    assert layout == {
        "code_layout": {
            "OWNER": {"type": "address", "length": 32, "offset": 0}
        }
    }


def test_real_legacy_immutable_layout_compares_with_modern_code_only_target(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "legacy_immutable_only.vy"
    legacy_source = """# @version 0.3.10
OWNER: immutable(address)

@external
def __init__():
    OWNER = msg.sender

@view
@external
def owner() -> address:
    return OWNER
"""
    target_source = legacy_source.replace(
        "# @version 0.3.10", "#pragma version 0.4.3"
    ).replace("@external\ndef __init__", "@deploy\ndef __init__", 1)
    contract.write_text(legacy_source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.3.10")
    target_compile = compile_target_source(contract, target_source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert source_compile.artifacts is not None
    assert source_compile.artifacts["layout"] == {
        "code_layout": {
            "OWNER": {"type": "address", "length": 32, "offset": 0}
        },
        "storage_layout": {},
    }


def test_real_legacy_width_inference_matches_modern_layout(tmp_path: Path) -> None:
    contract = tmp_path / "storage_widths.vy"
    legacy_source = """# @version 0.3.10
fixed_values: uint256[3]
dynamic_values: DynArray[uint256, 3]
blob: Bytes[64]
label: String[32]
pools: HashMap[address, uint256]
"""
    target_source = legacy_source.replace(
        "# @version 0.3.10", "#pragma version 0.4.3"
    )
    contract.write_text(legacy_source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.3.10")
    target_compile = compile_target_source(contract, target_source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    storage = layout["storage_layout"]
    assert isinstance(storage, dict)
    assert {
        name: entry["n_slots"]
        for name, entry in storage.items()
        if isinstance(entry, dict)
    } == {
        "fixed_values": 3,
        "dynamic_values": 4,
        "blob": 3,
        "label": 2,
        "pools": 1,
    }


def test_real_compiler_nested_array_layouts_are_comparable(tmp_path: Path) -> None:
    contract = tmp_path / "nested_storage_widths.vy"
    source = """#pragma version 0.4.3
from ethereum.ercs import IERC20

tokens: IERC20[3]
dynamic_values: DynArray[uint256, 3][3]
mixed_values: DynArray[DynArray[uint256, 3][3], 3][5]
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    storage = layout["storage_layout"]
    assert isinstance(storage, dict)
    tokens = storage["tokens"]
    dynamic_values = storage["dynamic_values"]
    assert isinstance(tokens, dict)
    assert isinstance(dynamic_values, dict)
    assert str(tokens["type"]).endswith("IERC20.vyi[3]")
    assert dynamic_values["type"] == "DynArray[uint256, 3][3]"
    assert {
        name: entry["n_slots"]
        for name, entry in storage.items()
        if isinstance(entry, dict)
    } == {
        "tokens": 3,
        "dynamic_values": 12,
        "mixed_values": 185,
    }


def test_real_legacy_flag_layout_matches_modern_layout(tmp_path: Path) -> None:
    contract = tmp_path / "flag_storage.vy"
    source = """#pragma version 0.4.0
flag Permission:
    READ
    WRITE

permission: Permission
permissions: HashMap[address, Permission]
"""
    target = source.replace("#pragma version 0.4.0", "#pragma version 0.4.3")
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert source_compile.artifacts is not None
    assert target_compile.artifacts is not None
    source_layout = source_compile.artifacts["layout"]
    target_layout = target_compile.artifacts["layout"]
    assert isinstance(source_layout, dict)
    assert isinstance(target_layout, dict)
    source_storage = source_layout["storage_layout"]
    target_storage = target_layout["storage_layout"]
    assert isinstance(source_storage, dict)
    assert isinstance(target_storage, dict)
    assert source_storage["permission"] == {
        "slot": 0,
        "type": "flag Permission('READ','WRITE')",
        "n_slots": 1,
    }
    assert target_storage["permission"] == {
        "slot": 0,
        "type": "Permission",
        "n_slots": 1,
    }


def test_real_inline_interface_marker_omission_uses_target_ast(tmp_path: Path) -> None:
    contract = tmp_path / "interface_storage.vy"
    source = """#pragma version 0.4.0
interface Pool:
    def ping() -> uint256: view

pool: Pool
pools: HashMap[address, Pool]
"""
    target = source.replace("#pragma version 0.4.0", "#pragma version 0.4.3")
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert target_compile.artifacts is not None
    assert target_compile.artifacts["ast"]["ast"]["ast_type"] == "Module"
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)


def test_real_interface_marker_does_not_alias_one_slot_struct(tmp_path: Path) -> None:
    contract = tmp_path / "interface_to_struct.vy"
    source = """#pragma version 0.4.0
interface Pool:
    def ping() -> uint256: view

pool: Pool
pools: HashMap[address, Pool]
"""
    target = """#pragma version 0.4.3
struct Pool:
    value: uint256

pool: Pool
pools: HashMap[address, Pool]
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert compare_artifacts(source_compile, target_compile) == (True, True, False)
    storage_diff = compare_artifact_details(source_compile, target_compile)[2]
    assert len(storage_diff) == 2
    assert any("changed storage: pool " in line for line in storage_diff)
    assert any("changed storage: pools " in line for line in storage_diff)


def test_real_root_interface_does_not_prove_imported_struct_annotation(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.vy"
    child.write_text(
        """#pragma version 0.4.3
struct Pool:
    value: uint256
""",
        encoding="utf-8",
    )
    contract = tmp_path / "root.vy"
    source = """#pragma version 0.4.0
interface Pool:
    def ping() -> uint256: view

item: Pool
items: HashMap[address, Pool]
"""
    target = """#pragma version 0.4.3
interface Pool:
    def ping() -> uint256: view

import child

item: child.Pool
items: HashMap[address, child.Pool]
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(
        paths=(contract,),
        target_version="0.4.3",
        compiler_search_paths=(tmp_path,),
    )

    source_compile = compile_source_file(contract, config, "0.4.0")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert compare_artifacts(source_compile, target_compile) == (True, True, False)
    storage_diff = compare_artifact_details(source_compile, target_compile)[2]
    assert len(storage_diff) == 2
    assert any("changed storage: item " in line for line in storage_diff)
    assert any("changed storage: items " in line for line in storage_diff)


@pytest.mark.parametrize("struct_name", ["IFoo", "ERC20"])
def test_real_interface_named_struct_keeps_two_slot_width(
    tmp_path: Path,
    struct_name: str,
) -> None:
    contract = tmp_path / f"{struct_name.lower()}_struct.vy"
    source = f"""#pragma version 0.4.3
struct {struct_name}:
    left: uint256
    right: uint256

value: {struct_name}
"""
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, source, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, True)
    assert target_compile.artifacts is not None
    layout = target_compile.artifacts["layout"]
    assert isinstance(layout, dict)
    storage = layout["storage_layout"]
    assert isinstance(storage, dict)
    assert storage["value"] == {"slot": 0, "type": struct_name, "n_slots": 2}

    legacy_without_width = CompileResult(
        "passed",
        artifacts={"layout": {"value": {"slot": 0, "type": struct_name}}},
    )
    assert compare_artifacts(legacy_without_width, target_compile) == (
        None,
        None,
        False,
    )


def test_real_ierc20_and_erc20_struct_names_remain_distinct(tmp_path: Path) -> None:
    contract = tmp_path / "token_struct.vy"
    source = """#pragma version 0.4.3
struct IERC20:
    left: uint256
    right: uint256

value: IERC20
"""
    target = source.replace("IERC20", "ERC20")
    contract.write_text(source, encoding="utf-8")
    config = Config(paths=(contract,), target_version="0.4.3")

    source_compile = compile_source_file(contract, config, "0.4.3")
    target_compile = compile_target_source(contract, target, config)

    assert source_compile.status == "passed"
    assert target_compile.status == "passed"
    assert unavailable_validation_artifacts(source_compile) == []
    assert unavailable_validation_artifacts(target_compile) == []
    assert compare_artifacts(source_compile, target_compile) == (True, True, False)
    assert compare_artifact_details(source_compile, target_compile)[2] == [
        "changed storage: value slot 0 IERC20 -> 0 ERC20"
    ]


def test_write_mode_validates_against_target_compiler(tmp_path: Path) -> None:
    contract = tmp_path / "migration_03.vy"
    shutil.copyfile(Path("tests/fixtures/migration_03.vy"), contract)

    report = tmp_path / "report.json"
    code = main(
        [
            str(contract),
            "--write",
            "--allow-unvalidated-source",
            "--report-json",
            str(report),
        ]
    )

    assert code == 0
    rewritten = contract.read_text()
    assert "#pragma version 0.4.3" in rewritten
    assert "staticcall self.token.balanceOf(msg.sender)" in rewritten
    assert "for i: uint256 in range(3):" in rewritten
    data = json.loads(report.read_text())
    assert data["write_requested"] is True
    assert data["wrote_changes"] is True
    assert data["validation_decision"]["status"] == "waived"
    assert data["files"][0]["validation"]["target_compile"] == "passed"


def test_target_validation_uses_rewritten_import_overlay(tmp_path: Path) -> None:
    (tmp_path / "lib.vy").write_text(
        """# pragma version 0.4.0
X: constant(uint256) = 1
""",
        encoding="utf-8",
    )
    (tmp_path / "main.vy").write_text(
        """# pragma version 0.4.0
import lib

@external
def x() -> uint256:
    return lib.X
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(tmp_path), "--check", "--report-json", str(report)])

    assert code == 1
    data = json.loads(report.read_text())
    assert {
        file["path"].rsplit("/", 1)[-1]: file["validation"]["target_compile"]
        for file in data["files"]
    } == {"lib.vy": "passed", "main.vy": "passed"}


def test_repeated_singleton_natspec_fields_compile_after_rewrite(tmp_path: Path) -> None:
    contract = tmp_path / "documented.vy"
    contract.write_text(
        '''# @version 0.3.10
@external
def collect_fees() -> uint256:
    """
    @dev First detail
    @dev Second detail
    @dev Third detail
    @dev Fourth detail
    """
    return 0
''',
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code == 1
    file = json.loads(report.read_text(encoding="utf-8"))["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert file["validation"]["decision"]["status"] == "passed"


def test_legacy_erc4626_interface_getter_stub_compiles_but_width_is_unproven(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "ERC4626Mock.vy"
    shutil.copyfile(Path("tests/fixtures/erc4626_interface_getter.vy"), contract)
    report = tmp_path / "report.json"

    code = main([str(contract), "--check", "--report-json", str(report)])

    assert code == 7
    file = json.loads(report.read_text(encoding="utf-8"))["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert file["validation"]["decision"]["status"] == "blocked"
    assert file["validation"]["storage_layout_equal"] is False
    assert file["validation"]["storage_layout_diff"] == [
        "changed storage: asset slot 0 ERC20 -> 0 interface IERC20 "
        "(n_slots unknown -> 1)"
    ]


def test_alpha_target_validation_uses_alpha_compiler(tmp_path: Path) -> None:
    contract = tmp_path / "sqrt.vy"
    contract.write_text(
        """#pragma version 0.4.3
@external
def f(x: uint256) -> uint256:
    return isqrt(x)
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main(
        [
            str(contract),
            "--check",
            "--target-version",
            "0.5.0a2",
            "--report-json",
            str(report),
        ]
    )

    assert code == 1
    data = json.loads(report.read_text())
    file = data["files"][0]
    assert file["validation"]["target_compile"] == "passed"
    assert any(fix["rule"] == "VY101" for fix in file["fixes"])


def test_standalone_interface_receives_target_validation(tmp_path: Path) -> None:
    interface = tmp_path / "IToken.vyi"
    interface.write_text(
        """# @version 0.3.10
@view
@external
def balanceOf(owner: address) -> uint256: ...
""",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    code = main([str(interface), "--check", "--report-json", str(report)])

    assert code == 1
    file = json.loads(report.read_text())["files"][0]
    assert file["validation"]["source_compile"] == "skipped"
    assert file["validation"]["source_unavailable_artifacts"] == []
    assert file["validation"]["target_compile"] == "passed"
    assert file["validation"]["decision"]["status"] == "passed"


def test_invalid_standalone_interface_is_not_written(tmp_path: Path) -> None:
    interface = tmp_path / "IBroken.vyi"
    original = "# @version 0.3.10\n@external\ndef broken(: ...\n"
    interface.write_text(original, encoding="utf-8")
    report = tmp_path / "report.json"

    code = main([str(interface), "--write", "--report-json", str(report)])

    assert code == 2
    assert interface.read_text(encoding="utf-8") == original
    file = json.loads(report.read_text())["files"][0]
    assert file["validation"]["target_compile"] == "failed"
    assert file["validation"]["decision"]["status"] == "blocked"


def test_target_validation_does_not_normalize_selected_candidate(tmp_path: Path) -> None:
    contract = tmp_path / "selected.vy"
    original = "# @version 0.3.10\n@external\ndef __init__():\n    pass\n"
    contract.write_text(original, encoding="utf-8")

    code = main([str(contract), "--write", "--select", "VY002"])

    assert code == 2
    assert contract.read_text(encoding="utf-8") == original
