from __future__ import annotations


from ..analysis import SourceFacts, infer_expr_type, is_integer_type, normalize_type
from ..rule_helpers import literal_integer as _literal_integer
from .numeric_types import same_integer_signedness as _same_integer_signedness


def _cast_integer_arg_to_expected(
    value: str, expected_type: str | None, vars_for_line: dict[str, str], facts: SourceFacts
) -> str:
    if not is_integer_type(expected_type) or value.strip().startswith("convert("):
        return value
    actual_type = infer_expr_type(value, vars_for_line, facts)
    if not is_integer_type(actual_type) or _same_integer_signedness(actual_type, expected_type):
        return value
    return f"convert({value}, {normalize_type(expected_type or '')})"


def _cast_integer_arg_to_exact_expected(
    value: str, expected_type: str | None, vars_for_line: dict[str, str], facts: SourceFacts
) -> str:
    stripped = value.strip()
    if (
        not is_integer_type(expected_type)
        or stripped.startswith("convert(")
        or _literal_integer(stripped)
    ):
        return value
    actual_type = infer_expr_type(stripped, vars_for_line, facts)
    if not is_integer_type(actual_type) or normalize_type(actual_type or "") == normalize_type(
        expected_type or ""
    ):
        return value
    return f"convert({value}, {normalize_type(expected_type or '')})"



