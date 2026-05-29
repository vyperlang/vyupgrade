from __future__ import annotations

from vyupgrade.ast_facts import calls, integer_constants, node_span, root_ast, source_segment
from vyupgrade.analysis import infer_expr_type, parse_source_facts


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


def test_expr_type_extracts_min_max_value_type() -> None:
    assert infer_expr_type("max_value(int128)", {}, None) == "int128"
    assert infer_expr_type("min_value(int256)", {}, None) == "int256"
