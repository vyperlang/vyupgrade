"""Operator rewrites for legacy ``shift()`` calls whose amount is only known at runtime."""

from __future__ import annotations

import keyword
import re

from ..analysis import SourceFacts, infer_expr_type, is_integer_type, normalize_type
from ..source import find_matching
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
_CALLEE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_IMPURE_TEXT = re.compile(r"#|\b(?:extcall|staticcall)\b|\bmsg\.gas\b")
_INTEGER_LITERAL = re.compile(r"0x[0-9A-Fa-f_]+|\d[\d_]*")


def runtime_shift_replacement(
    value: str, amount: str, vars_for_line: dict[str, str], facts: SourceFacts
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
    amount_type = integer_expression_type(amount, vars_for_line, facts)
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


def integer_expression_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts
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
        integer_expression_type(operand, vars_for_line, facts)
        for operand in operands
        if not _INTEGER_LITERAL.fullmatch(operand)
    }
    return operand_types.pop() if len(operand_types) == 1 else None


def _pure(expr: str) -> bool:
    """True when ``expr`` can be copied as text and evaluated repeatedly, in any order."""
    if _IMPURE_TEXT.search(expr):
        return False
    return all(
        match.group(1) in _PURE_BUILTINS or keyword.iskeyword(match.group(1))
        for match in _CALLEE.finditer(expr)
    )


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
