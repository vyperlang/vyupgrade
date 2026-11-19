from __future__ import annotations

import hashlib

import pytest

from vyupgrade.ast_facts import (
    CompilerSource,
    SourceSpan,
    calls,
    compiler_source_sha256,
    integer_constants,
    node_span,
    root_ast,
    source_segment,
)
from vyupgrade.analysis import infer_expr_type, parse_source_facts
from vyupgrade.vyper_builtins import BUILTIN_INTERFACES


def test_compiler_source_translates_mixed_newlines_without_changing_captured_text():
    original = "é\r\nB\rC\nD"
    compiler = CompilerSource(original)
    normalized = "é\nB\nC\nD"
    assert compiler.normalized == normalized
    assert compiler_source_sha256(original) == hashlib.sha256(normalized.encode()).hexdigest()
    assert compiler.matches_digest(compiler_source_sha256(original))
    assert not compiler.matches_digest(hashlib.sha256(original.encode()).hexdigest())
    assert not compiler.matches_digest(None)
    assert [compiler.original_offset(i) for i in range(len(normalized) + 1)] == [
        0,
        1,
        3,
        4,
        5,
        6,
        7,
        8,
    ]
    assert compiler.original_offset(-1) is None
    assert compiler.original_offset(len(normalized) + 1) is None
    span = compiler.span({"src": "2:3:4"}, source_id=4)
    assert span == SourceSpan(original.index("B"), 3, 4)
    assert source_segment(original, span) == "B\rC"


def test_compiler_source_resolves_byte_columns_on_each_endpoint():
    source = "é\r\n  ï + (û\r     + ô)\n"
    compiler = CompilerSource(source)
    # b1 adds byte columns to line starts measured in normalized characters.
    line_two, line_three = 2, 11
    first_column = len("  ï + ".encode())
    last_column = len("     + ô)".encode())
    raw_start, raw_end = line_two + first_column, line_three + last_column
    node = {
        "src": f"{raw_start}:{raw_end - raw_start}:2",
        "lineno": 2,
        "col_offset": first_column,
        "end_lineno": 3,
        "end_col_offset": last_column,
    }
    span = compiler.span(node, source_id=2, byte_columns=True)
    assert source_segment(source, span) == "(û\r     + ô)"
    assert compiler.span(node, source_id=1, byte_columns=True) is None
    assert (
        compiler.span(dict(node, col_offset=first_column + 1), source_id=2, byte_columns=True)
        is None
    )


@pytest.mark.parametrize(
    "positions",
    [
        {"src": "0:2:0", "lineno": 1, "col_offset": 1, "end_lineno": 1, "end_col_offset": 2},
        {"src": "1:1:0", "lineno": 1, "col_offset": 1, "end_lineno": 1, "end_col_offset": 2},
        {"src": "0:8:0", "lineno": 1, "col_offset": 0, "end_lineno": 1, "end_col_offset": 8},
        {"src": "0:2:0", "lineno": True, "col_offset": 0, "end_lineno": 1, "end_col_offset": 2},
        {"src": "0:1:0"},
        {"src": "not a span"},
    ],
)
def test_compiler_source_rejects_inconsistent_or_non_utf8_boundary_evidence(positions):
    assert CompilerSource("é").span(positions, source_id=0, byte_columns=True) is None


def test_ast_facts_extract_integer_constants() -> None:
    ast = {
        "ast": {
            "ast_type": "Module",
            "body": [
                {
                    "ast_type": "VariableDecl",
                    "is_constant": True,
                    "target": {"ast_type": "Name", "id": "BAL_SHIFT", "src": "0:9:0"},
                    "value": {"ast_type": "Int", "value": -16, "src": "30:3:0"},
                },
                {
                    "ast_type": "VariableDecl",
                    "is_constant": False,
                    "target": {"ast_type": "Name", "id": "stored"},
                    "value": {"ast_type": "Int", "value": 1},
                },
            ],
        }
    }

    assert integer_constants(ast) == {"BAL_SHIFT": -16}


def test_ast_facts_extract_call_spans() -> None:
    source = "return shift(x, -BAL_SHIFT)"
    ast = {
        "ast_type": "Module",
        "body": [
            {
                "ast_type": "Return",
                "value": {
                    "ast_type": "Call",
                    "src": "7:20:0",
                    "func": {"ast_type": "Name", "id": "shift"},
                    "args": [
                        {"ast_type": "Name", "id": "x", "src": "13:1:0"},
                        {
                            "ast_type": "UnaryOp",
                            "src": "16:10:0",
                            "operand": {"ast_type": "Name", "id": "BAL_SHIFT"},
                        },
                    ],
                },
            }
        ],
    }

    shift = next(calls(ast, "shift"))

    assert root_ast(ast) is ast
    assert len(shift.args) == 2
    assert source_segment(source, shift.span) == "shift(x, -BAL_SHIFT)"
    assert node_span(shift.args[1]) is not None


def _legacy_constant_type(type_name: str) -> dict[str, object]:
    return {
        "ast_type": "Call",
        "func": {"ast_type": "Name", "id": "constant"},
        "args": [{"ast_type": "Name", "id": type_name}],
    }


def test_ast_facts_extract_legacy_list_body_integer_constants() -> None:
    ast = {
        "ast": [
            {
                "ast_type": "AnnAssign",
                "target": {"ast_type": "Name", "id": "N_COINS"},
                "annotation": _legacy_constant_type("int128"),
                "value": {"ast_type": "Num", "n": 3},
            },
            {
                "ast_type": "AnnAssign",
                "target": {"ast_type": "Name", "id": "NEGATIVE"},
                "annotation": _legacy_constant_type("int128"),
                "value": {
                    "ast_type": "UnaryOp",
                    "op": {"ast_type": "USub"},
                    "operand": {"ast_type": "Num", "n": 1},
                },
            },
            {
                # Pre-0.2 compilers give hex literals as integer ``Num`` nodes.
                "ast_type": "AnnAssign",
                "target": {"ast_type": "Name", "id": "MASK"},
                "annotation": _legacy_constant_type("bytes32"),
                "value": {"ast_type": "Num", "n": 2**248},
            },
            {
                "ast_type": "AnnAssign",
                "target": {"ast_type": "Name", "id": "OWNER"},
                "annotation": _legacy_constant_type("address"),
                "value": {"ast_type": "Num", "n": 1},
            },
            {
                "ast_type": "AnnAssign",
                "target": {"ast_type": "Name", "id": "stored"},
                "annotation": {"ast_type": "Name", "id": "uint256"},
                "value": {"ast_type": "Num", "n": 5},
            },
            {
                "ast_type": "Expr",
                "value": {
                    "ast_type": "Call",
                    "func": {"ast_type": "Name", "id": "shift"},
                    "args": [{"ast_type": "Name", "id": "x"}],
                    "src": "0:8:0",
                },
            },
        ]
    }

    assert root_ast(ast)["ast_type"] == "Module"
    assert integer_constants(ast) == {"N_COINS": 3, "NEGATIVE": -1}
    assert next(calls(ast, "shift")).name == "shift"


def test_source_facts_skip_event_fields() -> None:
    source = """event DelegateBoost:
    _expire_time: uint256

MIN_DELEGATION_TIME: constant(uint256) = 86400

@external
def f(_expire_time: int256):
    time: int256 = convert(block.timestamp, int256)
    assert _expire_time > time + MIN_DELEGATION_TIME
"""

    facts = parse_source_facts(source)

    assert facts.global_vars == {"MIN_DELEGATION_TIME": "constant(uint256)"}
    assert facts.vars_at_line(8)["_expire_time"] == "int256"


def test_source_facts_record_inline_function_body_headers() -> None:
    source = """@external
@pure
def wrappedAsset() -> address: return SHARE
"""

    facts = parse_source_facts(source)

    assert facts.function_names == {3: "wrappedAsset"}
    assert facts.function_decorators[3] == ("external", "pure")
    assert facts.function_returns[3] == "address"


def test_source_facts_record_primitive_storage_vars() -> None:
    source = """owner: public(address)
aave_referral: uint256
balances: HashMap[address, uint256]

@external
def f():
    aave_referral: bytes32 = convert(self.aave_referral, bytes32)
"""

    facts = parse_source_facts(source)

    assert facts.storage_vars["owner"] == "address"
    assert facts.storage_vars["aave_referral"] == "uint256"
    assert facts.storage_vars["balances"] == "HashMap[address, uint256]"
    assert infer_expr_type("self.aave_referral", facts.vars_at_line(6), facts) == "uint256"


def test_source_facts_do_not_mutate_builtin_interface_registry() -> None:
    source = """interface IERC20:
    def foo(): view
"""

    parse_source_facts(source)

    assert "foo" not in BUILTIN_INTERFACES["IERC20"]


def test_source_facts_end_backslash_continued_function_headers() -> None:
    source = """interface IGauge:
    def totalSupply() -> uint256: view

interface ICLGauge:
    def rewardRate() -> uint256: view

@internal
def first(_data: address[4])\\
    -> uint256:
    gauge: IGauge = IGauge(_data[0])
    return gauge.totalSupply()

@external
def second(_gauge: address)\\
    -> uint256:
    gauge: ICLGauge = ICLGauge(_gauge)
    return gauge.rewardRate()
"""

    facts = parse_source_facts(source)

    assert facts.function_names == {8: "first", 14: "second"}
    assert facts.function_ends[8] == 13
    assert facts.vars_at_line(10)["gauge"] == "IGauge"
    assert facts.vars_at_line(16)["gauge"] == "ICLGauge"


def test_expr_type_extracts_min_max_value_type() -> None:
    assert infer_expr_type("max_value(int128)", {}, None) == "int128"
    assert infer_expr_type("min_value(int256)", {}, None) == "int256"


def test_expr_type_extracts_prevrandao_type() -> None:
    assert infer_expr_type("block.prevrandao", {}, None) == "bytes32"


def test_expr_type_extracts_indexed_tuple_call_component() -> None:
    source = """interface Aggregator:
    def latestRoundData() -> (uint256, int256, uint256): view

agg: Aggregator
"""

    facts = parse_source_facts(source)

    assert infer_expr_type("(agg.latestRoundData())[1]", facts.vars_at_line(4), facts) == "int256"


def test_compiler_source_checks_ordinary_ast_character_columns_if_present():
    compiler = CompilerSource("é\r\nüτ()")
    node = {"src": "3:3:0", "lineno": 2, "col_offset": 1, "end_lineno": 2, "end_col_offset": 4}
    assert source_segment(compiler.original, compiler.span(node, source_id=0)) == "τ()"
    assert compiler.span(dict(node, end_col_offset=5), source_id=0) is None
