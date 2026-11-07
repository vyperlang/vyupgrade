from __future__ import annotations

from pathlib import Path

import pytest

from vyupgrade.compiler import resolve_import_closure
from vyupgrade.models import Config, RewriteResult
from vyupgrade.rules import apply_rules


SOURCE = "#pragma version 0.4.3\nfrom . import lib\ninitializes: lib  # keep this comment\n"


def _migrate(tmp_path: Path, module: str, source: str = SOURCE, **kwargs) -> RewriteResult:
    path = tmp_path / "main.vy"
    path.write_text(source)
    (tmp_path / "lib.vy").write_text(module)
    snapshot = resolve_import_closure({path: source}, (tmp_path,))
    return apply_rules(
        source,
        Config(
            paths=(path,),
            target_version="0.5.0b2",
            source_snapshot=snapshot,
            select=frozenset({"VY126"}),
            **kwargs,
        ),
        path,
    )


@pytest.mark.parametrize(
    "module",
    [
        "",
        '("Library documentation")\n',
        '"Library " "documentation"\n',
        '"""\ninitializes: fake\nx: uint256\n@nonreentrant\n"""\n',
        "# x: uint256\n# @abstract\n",
        "X: constant(uint256) = 42\n",
        "(X): constant(uint256) = 42\n",
        "X: (constant(uint256)) = 42\n",
        "X: public((constant(uint256))) = 42\n",
        "X: public(constant(uint256)) = 42\n",
        "X: public(reentrant(constant(uint256))) = 42\n",
        "X: reentrant(public(constant(uint256))) = 42\n",
        "X: constant(\n    uint256\n) = 42\n",
        "@internal\n@pure\ndef f() -> uint256:\n    x: uint256 = 42\n    return x\n",
        "struct S:\n    x: uint256\nevent E:\n    x: uint256\nflag F:\n    A\n",
        "interface I:\n    def f() -> uint256: view\n",
        "import child\nexports: child.f\n",
        "#pragma nonreentrancy on\n@external\n@pure\ndef f() -> uint256:\n    return 1\n",
        "#pragma nonreentrancy on\n@external\n@reentrant\ndef f():\n    pass\n",
        "#pragma nonreentrancy on\n@internal\ndef f():\n    pass\n",
    ],
)
def test_drop_stateless_initialization(tmp_path: Path, module: str) -> None:
    result = _migrate(tmp_path, module)
    assert result.source == SOURCE.replace("initializes: lib", "")
    assert [fix.rule for fix in result.fixes] == ["VY126"]
    assert result.fixes[0].line == 3
    assert not result.diagnostics


@pytest.mark.parametrize(
    "module",
    [
        "x: uint256\n",
        "x: public(uint256)\n",
        "x: transient(uint256)\n",
        "X: immutable(uint256)\n",
        "x: HashMap[address, uint256]\n",
        "x: UnknownType\n",
        "@deploy\ndef __init__():\n    pass\n",
        "@deploy\ndef __\uff49\uff4e\uff49\uff54__():\n    pass\n",
        "@external\n@nonreentrant\ndef f():\n    pass\n",
        "@external\n@(nonreentrant)\ndef f():\n    pass\n",
        "@(external)\n@view\ndef f():\n    pass\n#pragma nonreentrancy on\n",
        "@internal\n@(abstract)\ndef f():\n    ...\n",
        "@external\n@abstract\ndef f():\n    ...\n",
        "uses: child\n",
        "initializes: child\n",
        "initializes: child[dep := state]\n",
        "#pragma nonreentrancy on\n@external\n@view\ndef f():\n    pass\n",
        "@external\n@view\ndef f():\n    pass\n#pragma nonreentrancy on\n",
        "@external\n@view\ndef f():\n    #pragma nonreentrancy on\n    pass\n",
        "unknown_new_syntax\n",
        "x: constant(\n",
    ],
)
def test_retain_stateful_abstract_or_unknown_module(tmp_path: Path, module: str) -> None:
    result = _migrate(tmp_path, module)
    assert result.source == SOURCE
    assert not result.fixes


@pytest.mark.parametrize(
    ("source_version", "target_version", "removed"),
    [
        ("0.4.0", "0.5.0b2", True),
        ("0.5.0a1", "0.5.0b2", True),
        ("0.5.0b1", "0.5.0b2", True),
        ("0.4.3", "0.5.0b1", False),
        ("0.5.0b2", "0.5.0b2", False),
    ],
)
def test_version_crossing(
    tmp_path: Path, source_version: str, target_version: str, removed: bool
) -> None:
    path = tmp_path / "main.vy"
    path.write_text(SOURCE)
    (tmp_path / "lib.vy").write_text("")
    snapshot = resolve_import_closure({path: SOURCE})
    result = apply_rules(
        SOURCE,
        Config(
            paths=(path,),
            source_version=source_version,
            target_version=target_version,
            source_snapshot=snapshot,
            select=frozenset({"VY126"}),
        ),
        path,
    )
    assert bool(result.fixes) == removed


@pytest.mark.parametrize(
    "declaration",
    [
        "import lib as alias\ninitializes: alias\n",
        "from . import lib as alias\ninitializes: alias\n",
        "import lib\nfrom . import (\n    lib as alias,\n)\ninitializes: alias\n",
        "from pkg import lib as alias\ninitializes: alias\n",
        "import pkg.lib as alias\ninitializes: alias\n",
        "import lib\ninitializes: (\n    lib\n)\n",
        "import lib\n(initializes): lib\n",
    ],
)
def test_resolve_import_aliases(tmp_path: Path, declaration: str) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "lib.vy").write_text("")
    result = _migrate(tmp_path, "", "#pragma version 0.4.3\n" + declaration)
    assert [fix.rule for fix in result.fixes] == ["VY126"]


def test_snapshot_bytes_not_mutable_files(tmp_path: Path) -> None:
    path = tmp_path / "main.vy"
    path.write_text(SOURCE)
    module = tmp_path / "lib.vy"
    module.write_text("x: uint256\n")
    snapshot = resolve_import_closure({path: SOURCE})
    module.write_text("")
    result = apply_rules(
        SOURCE,
        Config(
            paths=(path,),
            target_version="0.5.0b2",
            select=frozenset({"VY126"}),
            source_snapshot=snapshot,
        ),
        path,
    )
    assert result.source == SOURCE


@pytest.mark.parametrize("include_dependencies", [False, True])
def test_nested_initializations_follow_migration_scope(
    tmp_path: Path, include_dependencies: bool
) -> None:
    (tmp_path / "child.vy").write_text("#pragma version 0.4.3\nX: constant(uint256) = 1\n")
    result = _migrate(
        tmp_path,
        "#pragma version 0.4.3\nimport child\ninitializes: child\n",
        include_dependencies=include_dependencies,
    )
    assert bool(result.fixes) == include_dependencies


def test_nested_stateful_module_stays_initialized(tmp_path: Path) -> None:
    (tmp_path / "child.vy").write_text("x: uint256\n")
    result = _migrate(tmp_path, "import child\ninitializes: child\n", include_dependencies=True)
    assert result.source == SOURCE


def test_nested_import_cycle_is_retained(tmp_path: Path) -> None:
    result = _migrate(tmp_path, "import main\ninitializes: main\n", include_dependencies=True)
    assert result.source == SOURCE


def test_missing_snapshot_or_import_is_retained(tmp_path: Path) -> None:
    config = Config(paths=(tmp_path,), target_version="0.5.0b2", select=frozenset({"VY126"}))
    assert apply_rules(SOURCE, config).source == SOURCE
    assert _migrate(
        tmp_path, "", SOURCE.replace("from . import lib", "import missing")
    ).source == SOURCE.replace("from . import lib", "import missing")


def test_comments_strings_selection_and_idempotence(tmp_path: Path) -> None:
    source = '#pragma version 0.4.3\n"""\ninitializes: lib\n"""\nimport lib\n# initializes: lib\ninitializes: lib\n'
    result = _migrate(tmp_path, "", source)
    assert result.source.count("initializes: lib") == 2
    assert not _migrate(tmp_path, "", result.source).fixes
    assert not _migrate(tmp_path, "", ignore=frozenset({"VY126"})).fixes


@pytest.mark.parametrize("separator", ["\f", "\v", "\u2028"])
def test_comment_line_separators_preserve_edit_offsets(tmp_path: Path, separator: str) -> None:
    source = SOURCE.replace(
        "from . import lib", f"# comment with{separator} a separator\nfrom . import lib"
    )
    result = _migrate(tmp_path, "", source)
    assert result.source == source.replace("initializes: lib", "")
    assert result.fixes[0].before == "initializes: lib"
    assert result.fixes[0].line == 4


def test_comments_inside_removed_declaration_are_preserved(tmp_path: Path) -> None:
    source = SOURCE.replace(
        "initializes: lib",
        "initializes: (\n    # user comment\n    #pragma nonreentrancy on\n    lib\n)",
    )
    result = _migrate(tmp_path, "", source)
    assert "initializes:" not in result.source
    assert "# user comment" in result.source
    assert "#pragma nonreentrancy on" in result.source
    assert "# keep this comment" in result.source
    assert not _migrate(tmp_path, "", result.source).fixes


def test_concrete_module_takes_precedence_over_json_interface(tmp_path: Path) -> None:
    (tmp_path / "lib.json").write_text("[]")
    (tmp_path / "lib.vyi").write_text("@external\n@view\ndef f(): ...\n")
    result = _migrate(tmp_path, "X: constant(uint256) = 1\n")
    assert [fix.rule for fix in result.fixes] == ["VY126"]


@pytest.mark.parametrize(
    ("root_source", "sibling_source", "removed"),
    [
        ("x: uint256\n", "X: constant(uint256) = 1\n", False),
        ("X: constant(uint256) = 1\n", "x: uint256\n", False),
        ("X: constant(uint256) = 1\n", "Y: constant(uint256) = 2\n", True),
    ],
)
def test_absolute_import_requires_every_candidate_to_be_stateless(
    tmp_path: Path, root_source: str, sibling_source: str, removed: bool
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "middle.vy").write_text("import leaf\ninitializes: leaf\n")
    (tmp_path / "pkg" / "leaf.vy").write_text(sibling_source)
    (tmp_path / "leaf.vy").write_text(root_source)
    path = tmp_path / "main.vy"
    source = "#pragma version 0.4.3\nfrom pkg import middle\ninitializes: middle\n"
    path.write_text(source)
    snapshot = resolve_import_closure({path: source})
    result = apply_rules(
        source,
        Config(
            paths=(path,),
            target_version="0.5.0b2",
            include_dependencies=True,
            source_snapshot=snapshot,
            select=frozenset({"VY126"}),
        ),
        path,
    )
    assert bool(result.fixes) == removed
    assert ("initializes: middle" not in result.source) == removed


def test_cr_only_line_endings_keep_stateful_module_initialized(tmp_path: Path) -> None:
    result = _migrate(tmp_path, "#pragma version 0.4.3\rcounter: public(uint256)\r")
    assert result.source == SOURCE
    assert not result.fixes


@pytest.mark.parametrize(("leaf", "removed"), [("X: constant(uint256) = 1\n", True), ("x: uint256\n", False)])
def test_dotted_initialization_resolves_through_intermediate_module(
    tmp_path: Path, leaf: str, removed: bool
) -> None:
    (tmp_path / "leaf.vy").write_text(leaf)
    source = "#pragma version 0.4.3\nfrom . import lib\ninitializes: lib.leaf\n"
    result = _migrate(tmp_path, "from . import leaf\n", source)
    assert ("initializes: lib.leaf" not in result.source) == removed
    assert bool(result.fixes) == removed


def test_builtin_stdlib_module_initialization_is_dropped(tmp_path: Path) -> None:
    source = "#pragma version 0.4.3\nimport math\ninitializes: math\n"
    result = _migrate(tmp_path, "", source)
    assert result.source == "#pragma version 0.4.3\nimport math\n\n"
    assert [fix.rule for fix in result.fixes] == ["VY126"]
