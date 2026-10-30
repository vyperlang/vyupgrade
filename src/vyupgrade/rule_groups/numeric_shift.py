"""Operator rewrites for legacy ``shift()`` calls whose amount is only known at runtime."""

from __future__ import annotations

import ast
import re

from ..analysis import SourceFacts, infer_expr_type, is_integer_type, normalize_type
from ..source import TextEdit, apply_edits, find_matching
from .numeric_types import is_signed_integer_type, is_unsigned_integer_type

# Builtins without side effects. The sign-dispatching rewrite evaluates the amount
# twice, and before the shifted value, which only pure operands make unobservable.
_PURE_BUILTINS = frozenset(
    {
        "abs",
        "convert",
        "len",
        "max",
        "max_value",
        "min",
        "min_value",
        "unsafe_add",
        "unsafe_div",
        "unsafe_mul",
        "unsafe_sub",
    }
)
_INTEGER_LITERAL = re.compile(r"0x[0-9A-Fa-f_]+|\d[\d_]*")


def constant_shift_replacement(value: str, amount: int) -> str:
    word = amount % 2**256
    signed_amount = word - 2**256 if word >= 2**255 else word
    operator = "<<" if signed_amount >= 0 else ">>"
    return f"({_operand(value)} {operator} {abs(signed_amount)})"


def runtime_shift_replacement(
    value: str, amount: str, vars_for_line: dict[str, str], facts: SourceFacts,
    constant_values: dict[str, int] | None = None,
) -> str | None:
    """Return ``<<``/``>>`` source equal to ``shift(value, amount)``, or None.

    Legacy ``shift`` compiles to ``if slt(amount, 0) then gshr(0 - amount, value)
    else shl(amount, value)``: bit 255 of the amount word picks the direction and
    the right-shift distance is that word's two's-complement negation. The rewrite
    mirrors both, so ``min_value(int256)`` still shifts right by ``2**255`` where
    ``-amount`` would revert, and a ``uint256`` amount of at least ``2**255`` still
    shifts right.
    """
    if not (_pure(value) and _pure(amount)):
        return None
    amount_type = integer_expression_type(amount, vars_for_line, facts, constant_values)
    amount = _inline_integer_constants(amount, constant_values or {})
    shifted, distance = _operand(value), _operand(amount)
    if is_signed_integer_type(amount_type):
        magnitude = f"unsafe_sub(0, convert(convert({amount}, bytes32), uint256))"
        return (
            f"(({shifted} >> {magnitude}) if {distance} < 0 "
            f"else ({shifted} << convert({amount}, uint256)))"
        )
    if amount_type == "uint256":
        return (
            f"(({shifted} >> unsafe_sub(0, {amount})) if {distance} >= 2**255 "
            f"else ({shifted} << {distance}))"
        )
    if is_unsigned_integer_type(amount_type):
        return f"({shifted} << {distance})"
    return None


def _inline_integer_constants(expr: str, constants: dict[str, int]) -> str:
    """Preserve legacy constant coercion without removing checked casts or arithmetic."""
    lines = expr.splitlines(keepends=True)
    edits: list[TextEdit] = []
    # _pure already parsed this expression. AST names exclude attributes and text
    # inside literals; Python's column offsets count UTF-8 bytes, not characters.
    for node in ast.walk(ast.parse(expr, mode="eval")):
        if not isinstance(node, ast.Name) or node.id not in constants:
            continue
        prefix = lines[node.lineno - 1].encode("utf-8")[:node.col_offset].decode("utf-8")
        start = sum(map(len, lines[:node.lineno - 1])) + len(prefix)
        value = constants[node.id]
        replacement = f"({value})" if value < 0 else str(value)
        edits.append(TextEdit(start, start + len(node.id), replacement))
    return apply_edits(expr, edits)


def integer_expression_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts,
    constant_values: dict[str, int] | None = None,
) -> str | None:
    """Return the integer type of ``expr``, or None when source text does not determine it.

    Vyper requires both operands of ``+ - * / %`` to share one type (untyped
    literals adapt), so arithmetic has the type its typed operands agree on.
    """
    expr = _strip_parens(expr.strip())
    inferred = infer_expr_type(expr, vars_for_line, facts)
    if is_integer_type(inferred):
        return normalize_type(inferred)
    operands = _arithmetic_operands(expr)
    if operands == [expr]:
        return None
    operand_types = {
        integer_expression_type(operand, vars_for_line, facts, constant_values)
        for operand in operands
        if not _INTEGER_LITERAL.fullmatch(operand)
        and operand not in (constant_values or {})
    }
    return operand_types.pop() if len(operand_types) == 1 else None


def _pure(expr: str) -> bool:
    """True when ``expr`` can be copied as text and evaluated repeatedly, in any order."""
    if "#" in expr:
        return False
    try:
        expression = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(expression):
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id not in _PURE_BUILTINS
        ):
            return False
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "msg"
            and node.attr == "gas"
        ):
            return False
    return True


def _operand(expr: str) -> str:
    """Parenthesize ``expr`` unless it is a name, literal, call, subscript, or attribute."""
    return expr if re.fullmatch(r"[\w.]*", _outside_brackets(expr)) else f"({expr})"


def _outside_brackets(expr: str) -> str:
    depth = 0
    kept: list[str] = []
    for char in expr:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0:
            kept.append(char)
    return "".join(kept)


def _arithmetic_operands(expr: str) -> list[str]:
    """Split ``expr`` at top-level ``+ - * / %``; unary and doubled operators add no operand."""
    operands: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(expr):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0 and char in "+-*/%":
            operands.append(expr[start:index].strip())
            start = index + 1
    operands.append(expr[start:].strip())
    return [operand for operand in operands if operand]


def _strip_parens(expr: str) -> str:
    while expr.startswith("(") and find_matching(expr, 0) == len(expr) - 1:
        expr = expr[1:-1].strip()
    return expr
