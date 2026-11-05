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


# VY124 widens only in aggressive mode with the default VY125 wildcard rewrite ignored.
WIDEN = {"aggressive": True, "ignore": frozenset({"VY125"})}


def _migrate(
    tmp_path: Path,
    config,
    files: dict[str, str],
    target: str,
    target_version: str = "0.5.0a3",
    **options,
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
        config(target_version=target_version, source_snapshot=snapshot, **options),
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
        **WIDEN,
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
        **WIDEN,
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
        **WIDEN,
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
        **WIDEN,
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
        **WIDEN,
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
        **WIDEN,
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
def version() -> String[VERSION_LENGTH + 1]:
    return "1.0.0"
""",
        },
        "IFoo.vyi",
        **WIDEN,
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
        **WIDEN,
    )

    assert result.source.count("def version() -> String[1]: ...") == 2
    assert "\ndef version() -> String[5]: ...\n" in result.source


def test_vyi_widening_is_idempotent(tmp_path: Path, config) -> None:
    files = {
        "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
        "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
    }
    first = _migrate(tmp_path, config, files, "IFoo.vyi", **WIDEN)

    second = _migrate(
        tmp_path, config, {**files, "IFoo.vyi": first.source}, "IFoo.vyi", **WIDEN
    )

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
        **WIDEN,
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

    result = apply_rules(source, config(target_version="0.5.0a3", **WIDEN))

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
    [
        {"ignore": frozenset({"VY125"})},
        {"aggressive": True, "select": frozenset({"VYD019"})},
        {"aggressive": True, "ignore": frozenset({"VY124", "VY125"})},
    ],
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


def _wildcards(result: RewriteResult) -> list[tuple[str, str]]:
    return [(fix.before, fix.after) for fix in result.fixes if fix.rule == "VY125"]


@pytest.mark.parametrize(
    ("declared", "implemented", "wildcard"),
    [
        ("String[1]", "String[5]", "String[...]"),
        ("Bytes[4]", "Bytes[32]", "Bytes[...]"),
        ("DynArray[uint256, 2]", "DynArray[uint256, 8]", "DynArray[uint256, ...]"),
        ("DynArray[String[1], 2]", "DynArray[String[7], 2]", "DynArray[String[...], 2]"),
        ("(String[1], uint256)", "(String[5], uint256)", "(String[...], uint256)"),
    ],
)
def test_exceeded_vyi_return_bound_becomes_wildcard(
    tmp_path: Path, config, declared: str, implemented: str, wildcard: str
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

    assert f"def version() -> {wildcard}: ..." in result.source
    assert _wildcards(result) == [(declared, wildcard)]
    assert result.diagnostics == []


@pytest.mark.parametrize("options", [{}, {"aggressive": True}])
def test_wildcard_takes_precedence_over_widening(tmp_path: Path, config, options) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
        },
        "IFoo.vyi",
        **options,
    )

    assert _wildcards(result) == [("String[1]", "String[...]")]
    assert _widenings(result) == []


def test_named_constant_bounds_are_compared_and_replaced(tmp_path: Path, config) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "interfaces/IERC20.vyi": """#pragma version 0.4.3
SYMBOL_MAX_LEN: constant(uint256) = 32

@external
@view
def symbol() -> String[SYMBOL_MAX_LEN]: ...

@external
@view
def name() -> String[SYMBOL_MAX_LEN]: ...
""",
            "Vault.vy": """#pragma version 0.4.3
from interfaces import IERC20

implements: IERC20

NAME_LEN: constant(uint256) = 32
SYMBOL_LEN: constant(uint256) = 34

symbol: public(reentrant(String[SYMBOL_LEN]))
name: public(String[NAME_LEN])
""",
        },
        "interfaces/IERC20.vyi",
    )

    assert "def symbol() -> String[...]: ..." in result.source
    assert "def name() -> String[SYMBOL_MAX_LEN]: ..." in result.source
    assert _wildcards(result) == [("String[SYMBOL_MAX_LEN]", "String[...]")]


def test_wildcard_is_idempotent(tmp_path: Path, config) -> None:
    files = {
        "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
        "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
    }
    first = _migrate(tmp_path, config, files, "IFoo.vyi")

    second = _migrate(tmp_path, config, {**files, "IFoo.vyi": first.source}, "IFoo.vyi")

    assert second.source == first.source
    assert second.fixes == []


def test_wildcard_requires_0_5_target(tmp_path: Path, config) -> None:
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
    assert result.fixes == []


CALLER = """#pragma version 0.4.3
from . import IFoo

token: IFoo

@external
@view
def typed(a: address) -> uint256:
    narrow: String[1] = staticcall IFoo(a).version()
    self.check(staticcall self.token.version())
    return len(narrow)

@external
@view
def returned(a: address) -> String[1]:
    return staticcall IFoo(a).version()

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall IFoo(a).version())

@external
@view
def compared(a: address) -> bool:
    return staticcall self.token.version() == "1" or (staticcall IFoo(a).version()) == "2"

@internal
@pure
def check(version: String[1]):
    pass
"""


def _migrate_caller(tmp_path: Path, config, caller: str, **options) -> RewriteResult:
    return _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
            "Caller.vy": caller,
        },
        "Caller.vy",
        **options,
    )


def test_untyped_calls_through_wildcard_keep_the_old_bound(tmp_path: Path, config) -> None:
    result = _migrate_caller(tmp_path, config, CALLER)

    bound = "self.__vyupgrade_return_bound"
    assert "    narrow: String[1] = staticcall IFoo(a).version()\n" in result.source
    # An internal-call argument has an expected type too, but passing it through
    # the bound function is equivalent, so the rule does not tell them apart.
    assert f"    self.check({bound}(staticcall self.token.version()))\n" in result.source
    assert "    return staticcall IFoo(a).version()\n" in result.source
    assert f"    return len({bound}(staticcall IFoo(a).version()))\n" in result.source
    assert (
        f"    return {bound}(staticcall self.token.version()) == \"1\""
        f" or ({bound}(staticcall IFoo(a).version())) == \"2\"\n"
    ) in result.source
    assert result.source.endswith(
        "\n\n@internal\n@pure\ndef __vyupgrade_return_bound(returned: String[1]) -> String[1]:\n"
        "    return returned\n"
    )
    assert result.source.count("def __vyupgrade_return_bound") == 1
    assert [fix.after for fix in result.fixes if fix.rule == "VY125"] == [
        f"{bound}(staticcall self.token.version())",
        f"{bound}(staticcall IFoo(a).version())",
        f"{bound}(staticcall self.token.version())",
        f"{bound}(staticcall IFoo(a).version())",
    ]


def test_discarded_extcall_through_wildcard_keeps_the_old_bound(tmp_path: Path, config) -> None:
    caller = """#pragma version 0.4.3
from . import IFoo

@external
def poke(a: address):
    extcall IFoo(a).version()  # result unused
"""
    result = _migrate_caller(tmp_path, config, caller)

    assert (
        "    self.__vyupgrade_return_bound(extcall IFoo(a).version())  # result unused\n"
        in result.source
    )


def test_calls_are_left_alone_without_the_wildcard(tmp_path: Path, config) -> None:
    result = _migrate_caller(tmp_path, config, CALLER, **WIDEN)

    assert "__vyupgrade" not in result.source
    assert _wildcards(result) == []


def test_untyped_call_with_unnameable_return_type_is_diagnosed(tmp_path: Path, config) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="(String[1], uint256)"),
            "Foo.vy": IMPLEMENTATION.format(return_type="(String[5], uint256)"),
            "Caller.vy": """#pragma version 0.4.3
from . import IFoo

@external
def poke(a: address):
    extcall IFoo(a).version()
""",
        },
        "Caller.vy",
    )

    assert "    extcall IFoo(a).version()\n" in result.source
    assert _wildcards(result) == []
    assert [(diag.rule, diag.line) for diag in result.diagnostics] == [("VYD020", 6)]


def test_inline_interface_wildcard_and_call_sites(config) -> None:
    source = """#pragma version 0.4.3
interface IFoo:
    def version() -> String[1]: view

implements: IFoo

@external
@view
def version() -> String[5]:
    return "1.0.0"

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall IFoo(a).version())
"""

    result = apply_rules(source, config(target_version="0.5.0a3"))

    assert "    def version() -> String[...]: view\n" in result.source
    assert (
        "    return len(self.__vyupgrade_return_bound(staticcall IFoo(a).version()))\n"
        in result.source
    )
    assert "def __vyupgrade_return_bound(returned: String[1]) -> String[1]:" in result.source
