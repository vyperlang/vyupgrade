from __future__ import annotations

# Keep the existing helper imports available to rule-only callers.
from ..constant_evaluation import (
    eval_integer_constant_expr,
    integer_constant_values as integer_constant_values,
)
from ..source import split_top_level_args


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
