import pytest

from vyupgrade.rule_groups.numeric_constant_helpers import eval_integer_constant_expr, integer_constant_values


@pytest.mark.parametrize(("expression", "expected"), [
    ("max_value(int128)", 2**127 - 1), ("min_value(int8)", -128),
    ("max_value(uint8)", 255), ("min_value(uint8)", 0),
    ("-5 // 3", -1), ("5 // -3", -1), ("-5 // -3", 1),
    ("-5 % 3", -2), ("5 % -3", 2), ("-5 % -3", -2),
    ("2**256 - 1", 2**256 - 1),
])
def test_fallback_uses_vyper_integer_semantics(expression, expected):
    assert eval_integer_constant_expr(expression, {}) == expected


@pytest.mark.parametrize("expression", [
    "2 ** 1000000000", "2 ** -1", "2**1024", "1 // 0", "1 % 0",
    "True", "max_value(int7)", "max_value(int264)", "max_value(int8, x=1)",
    "1+" * 300 + "1", "unknown(1)", "max_value(bytes32)",
])
def test_fallback_fails_closed_and_bounds_work(expression):
    assert eval_integer_constant_expr(expression, {}) is None


def test_compiler_constant_evidence_takes_precedence():
    ast = {"ast_type": "Module", "body": [{
        "ast_type": "VariableDecl", "is_constant": True,
        "target": {"ast_type": "Name", "id": "VALUE"},
        "value": {"ast_type": "Int", "value": 7},
    }]}
    assert integer_constant_values("VALUE: constant(int128) = 3\n", ast) == {"VALUE": 7}
