from __future__ import annotations

import re

from ..analysis import SourceFacts
from ..source import logical_statements


def shift_call_bindings(source: str, facts: SourceFacts) -> dict[int, dict[str, str]]:
    """Resolve locals at shift calls, including calls on continuation lines."""
    lines = source.splitlines()
    scopes = [(0, {**facts.global_vars, **facts.storage_vars})]
    pending: dict[str, str] = {}
    calls: dict[int, dict[str, str]] = {}
    for first, last in logical_statements(source):
        line, indent = first.start
        while scopes[-1][0] > indent:
            scopes.pop()
        if indent > scopes[-1][0]:
            scopes.append((indent, pending))
        pending = {}
        if first.string == "def" and line in facts.function_names:
            pending = facts.function_params[facts.function_names[line]].copy()
        elif line in facts.local_declarations:
            name, typ = facts.local_declarations[line]
            if first.string == "for":
                pending = {name: typ}
            else:
                scopes[-1][1][name] = typ
        # A one-line compound statement (``def f(n: int256): return shift(x, n)``)
        # keeps its body on the header row, so its pending bindings apply there too.
        visible = [*(scope for _, scope in scopes), pending]
        for row in range(line, last.end[0] + 1):
            if "shift" in lines[row - 1]:
                calls[row] = {name: typ for scope in visible for name, typ in scope.items()}
    return calls


def vars_for_argument(
    source: str, arg_start: int, arg: str, vars_for_line: dict[str, str]
) -> dict[str, str]:
    name = arg.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return vars_for_line
    declared_type = nearest_declared_var_type(source, arg_start, name)
    if declared_type is not None:
        scoped = dict(vars_for_line)
        scoped[name] = declared_type
        return scoped
    loop_type = nearest_loop_var_type(source, arg_start, name)
    if loop_type is None:
        return vars_for_line
    scoped = dict(vars_for_line)
    scoped[name] = loop_type
    return scoped


def nearest_declared_var_type(source: str, index: int, name: str) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    current_line = source[
        line_start : source.find("\n", line_start)
        if source.find("\n", line_start) != -1
        else len(source)
    ]
    current_indent = len(current_line) - len(current_line.lstrip(" "))
    for line in reversed(source[:line_start].splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent > current_indent:
            continue
        if re.match(rf"for\s+{re.escape(name)}(?::[^:]+)?\s+in\b", stripped):
            return None
        decl = re.match(rf"{re.escape(name)}\s*:\s*([^=]+?)\s*=", stripped)
        if decl:
            return decl.group(1).strip()
        if re.match(r"(?:@|\s*def\s+)", stripped) and indent < current_indent:
            return None
    return None


def nearest_loop_var_type(source: str, index: int, name: str) -> str | None:
    line_start = source.rfind("\n", 0, index) + 1
    current_line = source[
        line_start : source.find("\n", line_start)
        if source.find("\n", line_start) != -1
        else len(source)
    ]
    current_indent = len(current_line) - len(current_line.lstrip(" "))
    prefix = source[:line_start].splitlines()
    for line in reversed(prefix):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent >= current_indent:
            continue
        loop_match = re.match(rf"for\s+{re.escape(name)}\s*:\s*([^:]+?)\s+in\b", stripped)
        if loop_match:
            return loop_match.group(1).strip()
        if re.match(r"(?:@|\s*def\s+)", stripped) and indent < current_indent:
            return None
    return None
