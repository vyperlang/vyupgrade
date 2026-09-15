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
        r"^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:public\s*\(\s*)?constant\s*\([^#\n=]+\)\s*\)?\s*=\s*(?P<expr>[^\n#]+)",
        re.MULTILINE,
    )
    mask = code_mask(source)
    for match in constant_re.finditer(source):
        if match.group("name") not in values and span_is_code(mask, match.start(), match.end()):
            value = eval_integer_constant_expr(match.group("expr"), values)
            if value is not None:
                values[match.group("name")] = value
    return values


# Bound fallback work independently of compiler timeouts. Unknown or larger
# expressions remain source text; compiler-provided values take precedence.
_MAX_BITS = 1024
_MAX_NODES = 256


def eval_integer_constant_expr(expr: str, values: dict[str, int]) -> int | None:
    if len(expr) > 4096:
        return None
    try:
        node = ast.parse(expr.strip(), mode="eval")
        if sum(1 for _ in ast.walk(node)) > _MAX_NODES:
            return None
        return _eval_integer_ast(node.body, values)
    except (SyntaxError, ValueError, RecursionError):
        return None


def _bounded(value: int | None) -> int | None:
    return value if type(value) is int and value.bit_length() <= _MAX_BITS else None


def _eval_integer_ast(node: ast.AST, values: dict[str, int]) -> int | None:
    if isinstance(node, ast.Constant):
        return _bounded(node.value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"min_value", "max_value"}
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Name)
    ):
        match = re.fullmatch(r"(u?)int(\d+)", node.args[0].id)
        if match is None:
            return None
        bits = int(match.group(2))
        if not 8 <= bits <= 256 or bits % 8:
            return None
        unsigned = bool(match.group(1))
        if node.func.id == "min_value":
            return 0 if unsigned else -(2 ** (bits - 1))
        return 2 ** (bits if unsigned else bits - 1) - 1
    if isinstance(node, ast.Name):
        return _bounded(values.get(node.id))
    if isinstance(node, ast.UnaryOp):
        operand = _eval_integer_ast(node.operand, values)
        if operand is not None:
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
        return None
    if not isinstance(node, ast.BinOp):
        return None
    left = _eval_integer_ast(node.left, values)
    right = _eval_integer_ast(node.right, values)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return _bounded(left + right)
    if isinstance(node.op, ast.Sub):
        return _bounded(left - right)
    if isinstance(node.op, ast.Mult):
        return _bounded(left * right)
    if isinstance(node.op, ast.FloorDiv) and right != 0:
        quotient = abs(left) // abs(right)
        return -quotient if (left < 0) != (right < 0) else quotient
    if isinstance(node.op, ast.Mod) and right != 0:
        remainder = abs(left) % abs(right)
        return -remainder if left < 0 else remainder
    if isinstance(node.op, ast.Pow) and 0 <= right <= _MAX_BITS:
        if abs(left) > 1 and left.bit_length() * right > _MAX_BITS:
            return None
        return _bounded(left**right)
    return None
