from __future__ import annotations

import ast
import re

from ..ast_facts import integer_constants as ast_integer_constants
from ..source import code_mask, split_top_level_args, span_is_code


def constant_range_iteration_bound(args: str, values: dict[str, int]) -> int | None:
    parts = split_top_level_args(args)
    if parts is None:
        return None
    if len(parts) == 1:
        stop = eval_integer_constant_expr(parts[0], values)
        if stop is None or stop < 0:
            return None
        return stop
    if len(parts) != 2:
        return None
    start = eval_integer_constant_expr(parts[0], values)
    stop = eval_integer_constant_expr(parts[1], values)
    if start is None or stop is None or stop < start:
        return None
    return stop - start


def integer_constant_values(
    source: str, source_ast: dict[str, object] | None = None
) -> dict[str, int]:
    values: dict[str, int] = ast_integer_constants(source_ast) if source_ast is not None else {}
    constant_re = re.compile(
        r"^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\s*\([^#\n=]+\)\s*=\s*(?P<expr>[^\n#]+)",
        re.MULTILINE,
    )
    mask = code_mask(source)
    for match in constant_re.finditer(source):
        if span_is_code(mask, match.start(), match.end()):
            value = eval_integer_constant_expr(match.group("expr"), values)
            if value is not None:
                values[match.group("name")] = value
    return values


def eval_integer_constant_expr(expr: str, values: dict[str, int]) -> int | None:
    try:
        node = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        return None
    return _eval_integer_ast(node.body, values)


def _eval_integer_ast(node: ast.AST, values: dict[str, int]) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_integer_ast(node.operand, values)
        if operand is None:
            return None
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        return None
    if isinstance(node, ast.BinOp):
        left = _eval_integer_ast(node.left, values)
        right = _eval_integer_ast(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv) and right != 0:
            return left // right
        if isinstance(node.op, ast.Mod) and right != 0:
            return left % right
        if isinstance(node.op, ast.Pow) and right >= 0:
            return left**right
    return None
