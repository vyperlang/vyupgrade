from __future__ import annotations

import pytest

from vyupgrade.analysis import infer_expr_type, iterable_element_type, parse_source_facts


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("DynArray[DynArray[uint256, 2], 3]", "DynArray[uint256, 2]"),
        ("DynArray [DynArray [uint256, 2], 3]", "DynArray [uint256, 2]"),
        ("DynArray[uint256[2], 3]", "uint256[2]"),
        ("uint256[2][3]", "uint256[2]"),
        ("DynArray[uint256, 2][3]", "DynArray[uint256, 2]"),
        ("(uint256, address)[3]", "(uint256, address)"),
        ("Bytes[32]", None),
        ("String[32]", None),
        ("HashMap[address, uint256]", None),
    ],
)
def test_iterable_element_type_balances_nested_types(
    type_name: str, expected: str | None
) -> None:
    assert iterable_element_type(type_name) == expected


def test_binding_visibility_follows_statements_and_blocks() -> None:
    source = """interface Read:
    def query() -> uint256: view
interface Write:
    def query() -> uint256: nonpayable
@external
def route(
    a: address, flag: bool,
) -> uint256:
    if flag:
        client: Read = Read(a)
        return (
            client.query()  # client: Write is a comment
)
    else:
        client: Write = Write(a)
        return client.query()
@external
def another(a: address) -> address: return a
"""
    facts = parse_source_facts(source)
    assert facts.vars_at_line(8) == {"a": "address", "flag": "bool"}
    assert "client" not in facts.vars_at_line(9)
    assert facts.vars_at_line(12)["client"] == "Read"
    assert "client" not in facts.vars_at_line(14)
    assert facts.vars_at_line(16)["client"] == "Write"
    assert facts.vars_at_line(18) == {"a": "address"}


def test_nested_loop_scopes_and_unannotated_iteration() -> None:
    source = (
        "@external\n"
        "def read(batches: uint8[2][2]):\n"
        "\tfor batch in batches:\n"
        "\t\tfor i: uint256 in range(2):\n"
        "\t\t\tx: uint8 = batch[i]\n"
        "\t\tfor i: int128 in range(2):\n"
        "\t\t\ty: int128 = i\n"
        "\tpass\n"
    )
    facts = parse_source_facts(source)
    assert facts.loop_vars_at_line(3) == set()
    assert facts.loop_vars_at_line(4) == {"batch"}
    assert facts.loop_vars_at_line(5) == {"batch", "i"}
    assert facts.vars_at_line(5)["i"] == "uint256"
    assert "batch" not in facts.vars_at_line(5)
    assert facts.vars_at_line(5, inferred_loop_types={3: "uint8[2]"})["batch"] == "uint8[2]"
    assert facts.loop_vars_at_line(6) == {"batch"}
    assert "x" not in facts.vars_at_line(6)
    assert facts.vars_at_line(7)["i"] == "int128"
    assert facts.vars_at_line(8) == {"batches": "uint8[2][2]"}


def test_one_line_function_and_loop_bodies_have_bindings() -> None:
    facts = parse_source_facts(
        "@external\ndef f(x: uint256) -> uint256: return x\n"
        "@external\ndef g():\n    for x: int128 in range(3): pass\n    pass\n"
    )
    assert facts.vars_at_line(2) == {"x": "uint256"}
    assert facts.vars_at_line(5) == {"x": "int128"}
    assert facts.loop_vars_at_line(5) == {"x"}
    assert facts.vars_at_line(6) == {}


def test_local_tuple_annotations_preserve_nested_types_and_require_typed_members():
    source = """@external
def f(u: uint256, n: int256):
    nested: ((uint256, int256), (address, bool)) = ((u, n), (self, True))
    single: (uint256,) = (u,)
    grouped: ((uint256, int256)) = (u, n)
    invalid: (uint256, unknown()) = (u, n)
    pass
"""
    facts = parse_source_facts(source)
    variables = facts.vars_at_line(7)
    assert "invalid" not in variables
    for expression, expected in (
        ("nested[0][1]", "int256"), ("nested[1][0]", "address"), ("single[0]", "uint256"),
        ("single[1]", None), ("grouped[1]", "int256"),
    ):
        assert infer_expr_type(expression, variables, facts) == expected
