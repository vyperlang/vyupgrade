from __future__ import annotations

from pathlib import Path

import pytest

from vyupgrade.compiler import resolve_import_closure
from vyupgrade.models import RewriteResult
from vyupgrade.rules import apply_rules


INTERFACE = """#pragma version 0.4.3
@external
@view
def version() -> {return_type}: ...
"""

IMPLEMENTATION = """#pragma version 0.4.3
from . import IFoo

implements: IFoo

@external
@view
def version() -> {return_type}:
    return empty({return_type})
"""


def _migrate(
    tmp_path: Path, config, files: dict[str, str], target: str, target_version: str = "0.5.0a3"
) -> RewriteResult:
    sources: dict[Path, str] = {}
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        sources[path] = source
    snapshot = resolve_import_closure(sources, (tmp_path,))
    path = tmp_path / target
    return apply_rules(
        sources[path],
        config(target_version=target_version, source_snapshot=snapshot, aggressive=True),
        path,
    )


def _widenings(result: RewriteResult) -> list[tuple[str, str]]:
    return [(fix.before, fix.after) for fix in result.fixes if fix.rule == "VY124"]


@pytest.mark.parametrize(
    ("declared", "implemented"),
    [
        ("String[1]", "String[5]"),
        ("Bytes[4]", "Bytes[32]"),
        ("DynArray[uint256, 2]", "DynArray[uint256, 8]"),
        ("DynArray[String[1], 2]", "DynArray[String[7], 3]"),
        ("(String[1], uint256)", "(String[5], uint256)"),
    ],
)
def test_vyi_return_bound_widens_to_implementation(
    tmp_path: Path, config, declared: str, implemented: str
) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type=declared),
            "Foo.vy": IMPLEMENTATION.format(return_type=implemented),
        },
        "IFoo.vyi",
    )

    assert f"def version() -> {implemented}: ..." in result.source
    assert _widenings(result) == [(declared, implemented)]


def test_vyi_return_bound_widens_to_widest_implementation(tmp_path: Path, config) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
            "Short.vy": IMPLEMENTATION.format(return_type="String[3]"),
            "Getter.vy": """#pragma version 0.4.3
from . import IFoo

implements: IFoo

version: public(String[10])
""",
            "Caller.vy": """#pragma version 0.4.3
from . import IFoo

@external
@view
def version() -> String[64]:
    return staticcall IFoo(msg.sender).version()
""",
        },
        "IFoo.vyi",
    )

    assert "def version() -> String[10]: ..." in result.source
    assert _widenings(result) == [("String[1]", "String[10]")]


def test_public_mapping_getter_implements_with_its_value_type(tmp_path: Path, config) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "INames.vyi": """#pragma version 0.4.3
@external
@view
def names(i: uint256) -> String[1]: ...
""",
            "Names.vy": """#pragma version 0.4.3
from . import INames

implements: INames

names: public(HashMap[uint256, String[12]])
""",
        },
        "INames.vyi",
    )

    assert "def names(i: uint256) -> String[12]: ..." in result.source
    assert _widenings(result) == [("String[1]", "String[12]")]



def test_vyi_bound_already_covering_implementations_is_unchanged(
    tmp_path: Path, config
) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[10]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
        },
        "IFoo.vyi",
    )

    assert "def version() -> String[10]: ..." in result.source
    assert _widenings(result) == []


def test_aliased_absolute_interface_import_is_resolved(tmp_path: Path, config) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "interfaces/IFoo.vyi": INTERFACE.format(return_type="Bytes[1]"),
            "contracts/Foo.vy": """#pragma version 0.4.3
from interfaces import IFoo as IVersioned

implements: IVersioned

@external
@view
def version() -> Bytes[32]:
    return b""
""",
        },
        "interfaces/IFoo.vyi",
    )

    assert "def version() -> Bytes[32]: ..." in result.source
    assert _widenings(result) == [("Bytes[1]", "Bytes[32]")]


def test_same_named_interface_in_another_directory_is_not_widened(
    tmp_path: Path, config
) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "other/IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "other/Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
        },
        "IFoo.vyi",
    )

    assert _widenings(result) == []


def test_unprovable_implementation_bound_is_left_for_validation(
    tmp_path: Path, config
) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": """#pragma version 0.4.3
from . import IFoo

implements: IFoo

VERSION_LENGTH: constant(uint256) = 5

@external
@view
def version() -> String[VERSION_LENGTH]:
    return "1.0.0"
""",
        },
        "IFoo.vyi",
    )

    assert "def version() -> String[1]: ..." in result.source
    assert _widenings(result) == []


def test_vyi_widening_skips_comments_and_docstrings(tmp_path: Path, config) -> None:
    interface = '''#pragma version 0.4.3
"""
def version() -> String[1]: ...
"""
# def version() -> String[1]: ...
@external
@view
def version() -> String[1]: ...
'''
    result = _migrate(
        tmp_path,
        config,
        {"IFoo.vyi": interface, "Foo.vy": IMPLEMENTATION.format(return_type="String[5]")},
        "IFoo.vyi",
    )

    assert result.source.count("def version() -> String[1]: ...") == 2
    assert "\ndef version() -> String[5]: ...\n" in result.source


def test_vyi_widening_is_idempotent(tmp_path: Path, config) -> None:
    files = {
        "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
        "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
    }
    first = _migrate(tmp_path, config, files, "IFoo.vyi")

    second = _migrate(tmp_path, config, {**files, "IFoo.vyi": first.source}, "IFoo.vyi")

    assert second.source == first.source
    assert _widenings(second) == []


def test_vyi_widening_requires_0_5_target(tmp_path: Path, config) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
        },
        "IFoo.vyi",
        target_version="0.4.3",
    )

    assert "def version() -> String[1]: ..." in result.source
    assert _widenings(result) == []


def test_inline_interface_return_bounds_widen_to_implementation(config) -> None:
    source = """#pragma version 0.4.3
interface IFoo:
    def version() -> String[1]: view
    def data() -> Bytes[2]: view
    def ids() -> DynArray[uint256, 1]: view

implements: IFoo

@external
@view
def version() -> String[5]:
    return "1.0.0"

@external
@view
def data() -> Bytes[3]:
    return b"abc"

@external
@view
def ids() -> DynArray[uint256, 4]:
    return [1, 2, 3, 4]
"""

    result = apply_rules(source, config(target_version="0.5.0a3", aggressive=True))

    assert "    def version() -> String[5]: view\n" in result.source
    assert "    def data() -> Bytes[3]: view\n" in result.source
    assert "    def ids() -> DynArray[uint256, 4]: view\n" in result.source
    assert _widenings(result) == [
        ("String[1]", "String[5]"),
        ("Bytes[2]", "Bytes[3]"),
        ("DynArray[uint256, 1]", "DynArray[uint256, 4]"),
    ]


def test_inline_interface_without_implements_is_unchanged(config) -> None:
    source = """#pragma version 0.4.3
interface IOracle:
    def version() -> String[1]: view

@external
@view
def version() -> String[5]:
    return "1.0.0"
"""

    result = apply_rules(source, config(target_version="0.5.0a3"))

    assert "    def version() -> String[1]: view\n" in result.source
    assert _widenings(result) == []


@pytest.mark.parametrize(
    "options",
    [{}, {"aggressive": True, "select": ("VYD019",)},
     {"aggressive": True, "ignore": ("VY124",)}],
)
def test_interface_widening_requires_aggressive_and_enabled_fix(config, options) -> None:
    source = """#pragma version 0.4.3
interface IFoo:
    def version() -> String[1]: view

implements: IFoo

@external
@view
def version() -> String[5]:
    return "1.0.0"
"""
    result = apply_rules(source, config(target_version="0.5.0a3", **options))

    assert "def version() -> String[1]: view" in result.source
    assert _widenings(result) == []
    assert [diag.rule for diag in result.diagnostics] == ["VYD019"]
