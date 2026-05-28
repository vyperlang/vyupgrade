from __future__ import annotations

from vyupgrade.ast_facts import calls, integer_constants, node_span, root_ast, source_segment


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
