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
#pragma nonreentrancy on
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
    # Only VY125 is selected, so the version pragma stays and the rerun applies it again.
    first = _migrate(tmp_path, config, files, "IFoo.vyi", select=frozenset({"VY125"}))

    second = _migrate(
        tmp_path, config, {**files, "IFoo.vyi": first.source}, "IFoo.vyi",
        select=frozenset({"VY125"}),
    )

    assert first.fixes
    assert second.source == first.source
    assert second.fixes == []


@pytest.mark.parametrize(
    ("target_version", "options", "rule"),
    [
        (version, options, rule)
        for version in ("0.4.3", "0.5.0a1", "0.5.0a2")
        for options, rule in (({}, "VY125"), (WIDEN, "VY124"))
    ],
)
def test_return_bound_rules_cross_at_0_5_0a2(
    tmp_path: Path, config, target_version: str, options: dict, rule: str
) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
        },
        "IFoo.vyi",
        target_version=target_version,
        **options,
    )

    rewritten = target_version == "0.5.0a2"
    assert ("def version() -> String[1]: ..." in result.source) is not rewritten
    assert any(fix.rule == rule for fix in result.fixes) is rewritten


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
    # The parameter name avoids the caller's own `returned`.
    assert result.source.endswith(
        "\n\n@internal\n@pure\n"
        "def __vyupgrade_return_bound(returned_2: String[1]) -> String[1]:\n"
        "    return returned_2\n"
    )
    assert result.source.count("def __vyupgrade_return_bound") == 1
    assert [fix.after for fix in result.fixes if fix.rule == "VY125"] == [
        f"{bound}(staticcall self.token.version())",
        f"{bound}(staticcall IFoo(a).version())",
        f"{bound}(staticcall self.token.version())",
        f"{bound}(staticcall IFoo(a).version())",
    ]


def test_discarded_extcall_through_wildcard_keeps_the_old_bound(tmp_path: Path, config) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": """#pragma version 0.4.3
@external
def bump() -> String[1]: ...
""",
            "Foo.vy": """#pragma version 0.4.3
from . import IFoo

implements: IFoo

@external
def bump() -> String[5]:
    return "1.0.0"
""",
            "Caller.vy": """#pragma version 0.4.3
from . import IFoo

@external
def poke(a: address):
    extcall IFoo(a).bump()  # result unused
""",
        },
        "Caller.vy",
    )

    assert (
        "    self.__vyupgrade_return_bound(extcall IFoo(a).bump())  # result unused\n"
        in result.source
    )


@pytest.mark.parametrize(
    "statement",
    [
        "wide: String[10] = staticcall IFoo(a).version()",
        "wide: String[10] = empty(String[10])\n    wide = staticcall IFoo(a).version()",
    ],
)
def test_call_expecting_a_wider_type_keeps_the_old_bound(
    tmp_path: Path, config, statement: str
) -> None:
    caller = f"""#pragma version 0.4.3
from . import IFoo

@external
@view
def wide(a: address) -> String[10]:
    {statement}
    return wide

@external
@view
def returned(a: address) -> String[10]:
    return staticcall IFoo(a).version()
"""
    result = _migrate_caller(tmp_path, config, caller)

    assert "self.__vyupgrade_return_bound(staticcall IFoo(a).version())\n    return wide" in (
        result.source
    )
    assert "    return self.__vyupgrade_return_bound(staticcall IFoo(a).version())\n" in (
        result.source
    )


def test_call_expecting_the_old_type_by_named_constant_is_unchanged(
    tmp_path: Path, config
) -> None:
    caller = """#pragma version 0.4.3
from . import IFoo

ONE: constant(uint256) = 1

@external
@view
def narrow(a: address) -> String[ONE]:
    narrow: String[ONE] = staticcall IFoo(a).version()
    return staticcall IFoo(a).version()
"""
    result = _migrate_caller(tmp_path, config, caller)

    assert "__vyupgrade" not in result.source


@pytest.mark.parametrize(
    ("receiver", "extra"),
    [
        ("(IFoo(a))", ""),
        ("((IFoo(a)))", ""),
        ("IFoo(\n        a\n    )", ""),
        ("IFoo(a) ", ""),
        ("(self.token)", ""),
        ("self . token", ""),
        ("self.tok(a)", "\n@internal\n@view\ndef tok(a: address) -> IFoo:\n    return IFoo(a)\n"),
    ],
)
def test_parenthesized_and_internal_call_receivers_are_wrapped(
    tmp_path: Path, config, receiver: str, extra: str
) -> None:
    caller = f"""#pragma version 0.4.3
from . import IFoo

token: IFoo

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall {receiver}.version())
{extra}"""
    result = _migrate_caller(tmp_path, config, caller)

    assert (
        f"    return len(self.__vyupgrade_return_bound(staticcall {receiver}.version()))\n"
        in result.source
    )


@pytest.mark.parametrize("spacing", ["\n        ", "  # the version\n        ", " \\\n        "])
def test_keyword_on_a_previous_line_is_wrapped_with_the_call(
    tmp_path: Path, config, spacing: str
) -> None:
    caller = f"""#pragma version 0.4.3
from . import IFoo

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall{spacing}IFoo(a).version())
"""
    result = _migrate_caller(tmp_path, config, caller)

    assert (
        f"    return len(self.__vyupgrade_return_bound(staticcall{spacing}IFoo(a).version()))\n"
        in result.source
    )


@pytest.mark.parametrize("spacing", ["  # the version\n        ", " \\\n        "])
def test_spacing_between_receiver_and_method_is_wrapped(
    tmp_path: Path, config, spacing: str
) -> None:
    caller = f"""#pragma version 0.4.3
from . import IFoo

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall IFoo(a).{spacing}version())
"""
    result = _migrate_caller(tmp_path, config, caller)

    assert (
        f"    return len(self.__vyupgrade_return_bound(staticcall IFoo(a).{spacing}version()))\n"
    ) in result.source


def test_parenthesized_call_after_the_keyword_is_wrapped_with_it(
    tmp_path: Path, config
) -> None:
    caller = """#pragma version 0.4.3
from . import IFoo

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall (IFoo(a).version()))
"""
    result = _migrate_caller(tmp_path, config, caller)

    assert (
        "    return len(self.__vyupgrade_return_bound(staticcall (IFoo(a).version())))\n"
        in result.source
    )


def test_call_inside_another_function_named_like_the_bound_is_wrapped(
    tmp_path: Path, config
) -> None:
    caller = """#pragma version 0.4.3
from . import IFoo

@external
@view
def measured(a: address) -> uint256:
    return len(self.__vyupgrade_return_bound_custom(staticcall IFoo(a).version()))

@internal
@pure
def __vyupgrade_return_bound_custom(returned: String[10]) -> String[10]:
    return returned
"""
    result = _migrate_caller(tmp_path, config, caller)

    assert (
        "self.__vyupgrade_return_bound_custom("
        "self.__vyupgrade_return_bound(staticcall IFoo(a).version()))"
    ) in result.source


def test_call_with_an_unresolved_receiver_is_diagnosed(tmp_path: Path, config) -> None:
    caller = """#pragma version 0.4.3
from . import IFoo
import lib

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall self.tokens(a)[0].version()) + len(self.version())

@external
@view
def read(a: address) -> String[10]:
    return staticcall lib.tok(a).version()

@internal
@view
def tokens(a: address) -> DynArray[IFoo, 1]:
    return [IFoo(a)]

@internal
@pure
def version() -> String[1]:
    return "1"
"""
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
            "lib.vy": """#pragma version 0.4.3
import IFoo

@internal
@pure
def tok(a: address) -> IFoo:
    return IFoo(a)
""",
            "Caller.vy": caller,
        },
        "Caller.vy",
    )

    assert "__vyupgrade" not in result.source
    assert [(diag.rule, diag.line) for diag in result.diagnostics] == [
        ("VYD020", 8),
        ("VYD020", 13),
    ]


def test_calls_through_an_interface_only_imported_indirectly_are_diagnosed(
    tmp_path: Path, config
) -> None:
    caller = """#pragma version 0.4.3
import lib

@external
@view
def read(a: address) -> String[10]:
    return staticcall lib.IFoo(a).version()

@external
@view
def measured(a: address) -> uint256:
    return len(staticcall lib.tok(a).version())

@external
@view
def passed(token: lib.IFoo) -> String[10]:
    return staticcall token.version()

@external
@view
def returned(a: address) -> String[10]:
    return staticcall self.tok(a).version()

@internal
@pure
def tok(a: address) -> lib.IFoo:
    return lib.IFoo(a)
"""
    result = _migrate(
        tmp_path,
        config,
        {
            "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
            "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
            "lib.vy": """#pragma version 0.4.3
import IFoo

@internal
@pure
def tok(a: address) -> IFoo:
    return IFoo(a)
""",
            "Caller.vy": caller,
        },
        "Caller.vy",
    )

    assert "__vyupgrade" not in result.source
    assert [(diag.rule, diag.line) for diag in result.diagnostics] == [
        ("VYD020", 7),
        ("VYD020", 12),
        ("VYD020", 17),
        ("VYD020", 22),
    ]


def test_calls_through_a_module_qualified_inline_interface_are_wrapped(
    tmp_path: Path, config
) -> None:
    result = _migrate(
        tmp_path,
        config,
        {
            "Foo.vy": """#pragma version 0.4.3
interface IToken:
    def decimals() -> uint8: view

interface IFoo:
    def version() -> String[1]: view
    def tokens() -> DynArray[IToken, 1]: view

implements: IFoo

@external
@view
def version() -> String[5]:
    return "1.0.0"

@external
@view
def tokens() -> DynArray[IToken, 2]:
    return []
""",
            "Caller.vy": """#pragma version 0.4.3
import Foo

@external
@view
def read(a: address) -> String[10]:
    return staticcall Foo.IFoo(a).version()

@external
@view
def count(a: address) -> uint256:
    return len(staticcall Foo.IFoo(a).tokens())
""",
        },
        "Caller.vy",
    )

    assert "    return self.__vyupgrade_return_bound(staticcall Foo.IFoo(a).version())\n" in (
        result.source
    )
    assert "def __vyupgrade_return_bound(returned: String[1]) -> String[1]:" in result.source
    assert "    return len(self.__vyupgrade_return_bound_2(staticcall Foo.IFoo(a).tokens()))\n" in (
        result.source
    )
    assert (
        "def __vyupgrade_return_bound_2(returned: DynArray[Foo.IToken, 1])"
        " -> DynArray[Foo.IToken, 1]:"
    ) in result.source
    assert result.diagnostics == []


def test_call_wrapping_is_idempotent(tmp_path: Path, config) -> None:
    files = {
        "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
        "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
        "Caller.vy": CALLER,
    }
    first = _migrate(tmp_path, config, files, "Caller.vy", select=frozenset({"VY125"}))

    second = _migrate(
        tmp_path, config, {**files, "Caller.vy": first.source}, "Caller.vy",
        select=frozenset({"VY125"}),
    )

    assert first.fixes
    assert second.source == first.source
    assert second.fixes == []


def test_calls_are_left_alone_without_the_wildcard(tmp_path: Path, config) -> None:
    result = _migrate_caller(tmp_path, config, CALLER, **WIDEN)

    assert "__vyupgrade" not in result.source
    assert _wildcards(result) == []


STRUCT_FILES = {
    "common.vy": """#pragma version 0.4.3
struct Point:
    x: uint256
""",
    "IFoo.vyi": """#pragma version 0.4.3
import common

struct Own:
    y: uint256

@external
@view
def points() -> DynArray[common.Point, 1]: ...

@external
@view
def owns() -> DynArray[Own, 1]: ...
""",
    "Foo.vy": """#pragma version 0.4.3
import common
import IFoo

implements: IFoo

@external
@view
def points() -> DynArray[common.Point, 2]:
    return []

@external
@view
def owns() -> DynArray[IFoo.Own, 2]:
    return []
""",
}


def test_interface_module_struct_written_qualified_by_its_implementation(
    tmp_path: Path, config
) -> None:
    result = _migrate(tmp_path, config, STRUCT_FILES, "IFoo.vyi")

    assert _wildcards(result) == [
        ("DynArray[common.Point, 1]", "DynArray[common.Point, ...]"),
        ("DynArray[Own, 1]", "DynArray[Own, ...]"),
    ]


def test_untyped_calls_name_interface_module_struct_types_through_imports(
    tmp_path: Path, config
) -> None:
    caller = """#pragma version 0.4.3
import common as c
import IFoo

@external
@view
def count(a: address) -> uint256:
    return len(staticcall IFoo(a).points()) + len(staticcall IFoo(a).owns())
"""
    result = _migrate(tmp_path, config, {**STRUCT_FILES, "Caller.vy": caller}, "Caller.vy")

    assert (
        "    return len(self.__vyupgrade_return_bound(staticcall IFoo(a).points()))"
        " + len(self.__vyupgrade_return_bound_2(staticcall IFoo(a).owns()))\n"
    ) in result.source
    assert (
        "def __vyupgrade_return_bound(returned: DynArray[c.Point, 1]) -> DynArray[c.Point, 1]:"
        in result.source
    )
    assert (
        "def __vyupgrade_return_bound_2(returned: DynArray[IFoo.Own, 1])"
        " -> DynArray[IFoo.Own, 1]:"
    ) in result.source
    assert result.diagnostics == []


def test_untyped_call_with_unnameable_return_type_is_diagnosed(tmp_path: Path, config) -> None:
    # The caller does not import `common`, so it can't name `common.Point`.
    caller = """#pragma version 0.4.3
import IFoo

@external
@view
def count(a: address) -> uint256:
    return len(staticcall IFoo(a).points())
"""
    result = _migrate(tmp_path, config, {**STRUCT_FILES, "Caller.vy": caller}, "Caller.vy")

    assert "    return len(staticcall IFoo(a).points())\n" in result.source
    assert _wildcards(result) == []
    assert [(diag.rule, diag.line) for diag in result.diagnostics] == [("VYD020", 7)]


def test_inline_interface_return_types_are_nameable_by_their_file(config) -> None:
    source = """#pragma version 0.4.3
struct Point:
    x: uint256

interface IFoo:
    def points() -> (DynArray[Point, 1], uint256): view

implements: IFoo

@external
@view
def points() -> (DynArray[Point, 2], uint256):
    return [], 0

@external
@view
def count(a: address) -> uint256:
    points: DynArray[Point, 1] = []
    n: uint256 = 0
    points, n = staticcall IFoo(a).points()
    return len(points)
"""

    result = apply_rules(source, config(target_version="0.5.0a3"))

    assert "    def points() -> (DynArray[Point, ...], uint256): view\n" in result.source
    assert "    points, n = self.__vyupgrade_return_bound(staticcall IFoo(a).points())\n" in (
        result.source
    )
    assert (
        "def __vyupgrade_return_bound(returned: (DynArray[Point, 1], uint256))"
        " -> (DynArray[Point, 1], uint256):"
    ) in result.source
    assert result.diagnostics == []


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


BRANCH_LOCAL_INTERFACES = """#pragma version 0.4.3
interface Narrow:
    def data() -> Bytes[1]: view

interface Wide:
    def data() -> Bytes[5]: view

implements: Narrow

@external
@view
def data() -> Bytes[5]:
    return b"hello"

@external
@view
def read(a: address, narrow: bool) -> uint256:
    if narrow:
        ref: Narrow = Narrow(a)
        return len(staticcall ref.data())
    else:
        ref: Wide = Wide(a)
        return len(staticcall ref.data())
"""


def test_branch_local_receivers_are_typed_by_their_own_declaration(config) -> None:
    result = apply_rules(BRANCH_LOCAL_INTERFACES, config(target_version="0.5.0a3"))

    assert "    def data() -> Bytes[...]: view\n" in result.source
    assert (
        "        ref: Narrow = Narrow(a)\n"
        "        return len(self.__vyupgrade_return_bound(staticcall ref.data()))\n"
    ) in result.source
    assert (
        "        ref: Wide = Wide(a)\n        return len(staticcall ref.data())\n"
    ) in result.source
    assert result.diagnostics == []


UNRESOLVED_INLINE_RECEIVER = """#pragma version 0.4.3
interface IFoo:
    def data() -> Bytes[1]: view

implements: IFoo

@external
@view
def data() -> Bytes[5]:
    return b"hello"

@external
@view
def read(a: address) -> Bytes[5]:
    return staticcall self.refs(a)[0].data()

@internal
@view
def refs(a: address) -> DynArray[IFoo, 1]:
    return [IFoo(a)]
"""


def test_unresolved_receiver_keeps_the_old_bound(config) -> None:
    result = apply_rules(UNRESOLVED_INLINE_RECEIVER, config(target_version="0.5.0a3"))

    assert "    def data() -> Bytes[1]: view\n" in result.source
    assert "__vyupgrade" not in result.source
    assert _wildcards(result) == []
    assert [(diag.rule, diag.line) for diag in result.diagnostics] == [
        ("VYD020", 3),
        ("VYD020", 15),
    ]
    assert "kept interface return bound Bytes[1]" in result.diagnostics[0].message


def test_unresolved_receiver_in_another_contract_keeps_the_vyi_bound(
    tmp_path: Path, config
) -> None:
    files = {
        "IFoo.vyi": INTERFACE.format(return_type="String[1]"),
        "Foo.vy": IMPLEMENTATION.format(return_type="String[5]"),
        "Caller.vy": """#pragma version 0.4.3
import IFoo

@external
@view
def read(a: address) -> String[5]:
    return staticcall self.tokens(a)[0].version()

@internal
@view
def tokens(a: address) -> DynArray[IFoo, 1]:
    return [IFoo(a)]
""",
    }

    interface = _migrate(tmp_path, config, files, "IFoo.vyi")
    caller = _migrate(tmp_path, config, files, "Caller.vy")

    assert "def version() -> String[1]: ..." in interface.source
    assert _wildcards(interface) == []
    assert [diag.rule for diag in interface.diagnostics] == ["VYD020"]
    assert "Caller.vy:7" in interface.diagnostics[0].message
    assert "__vyupgrade" not in caller.source
    assert [(diag.rule, diag.line) for diag in caller.diagnostics] == [("VYD020", 7)]


def test_unnameable_old_type_keeps_the_vyi_bound(tmp_path: Path, config) -> None:
    caller = """#pragma version 0.4.3
import IFoo

@external
@view
def count(a: address) -> uint256:
    return len(staticcall IFoo(a).points())
"""
    files = {**STRUCT_FILES, "Caller.vy": caller}

    interface = _migrate(tmp_path, config, files, "IFoo.vyi")

    assert "def points() -> DynArray[common.Point, 1]: ..." in interface.source
    # The other function's callers can name its old type, so it still takes the wildcard.
    assert _wildcards(interface) == [("DynArray[Own, 1]", "DynArray[Own, ...]")]
    assert [diag.rule for diag in interface.diagnostics] == ["VYD020"]
