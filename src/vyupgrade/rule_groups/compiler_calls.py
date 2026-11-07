"""Use 0.3.10 call-site facts on the exact program the source compiler analyzed."""

from __future__ import annotations

import re
from hashlib import sha256
from itertools import pairwise
from typing import Any

from ..ast_facts import iter_nodes, node_span
from ..models import Fix
from ..rule_registry import RuleContext
from ..source import TextEdit, apply_edits, line_number
from ..versions import PRAGMA_RE


_NEXT_CODE_LINE = re.compile(r"^([ \t]*)(?=[^ \t\r\n#])", re.MULTILINE)


def _without_version_header(source: str) -> str:
    offset = 0
    for line in source.splitlines(keepends=True):
        if PRAGMA_RE.fullmatch(line):
            return source[:offset] + source[offset + len(line) :]
        if line.strip() and not line.lstrip().startswith("#"):
            break
        offset += len(line)
    return source


def proven_call_keywords(context: RuleContext) -> tuple[str, list[Fix], set[int]] | None:
    evidence = context.config.source_external_calls
    ast = context.config.source_ast
    if not evidence or not ast:
        return None
    original = context.original_source
    # The 0.3.10 CLI normalizes line endings before reporting `src`. CRLF
    # offsets cannot index the captured bytes without a separate position map.
    if (
        original is None
        or "\r" in original
        or evidence.get("source_sha256sum") != sha256(original.encode()).hexdigest()
    ):
        return None
    sites = evidence.get("calls")
    if not isinstance(sites, list) or not sites:
        return None
    by_id = {
        site.get("node_id"): site
        for site in sites
        if isinstance(site, dict) and type(site.get("node_id")) is int
    }
    if len(by_id) != len(sites):
        return None

    module = ast.get("ast")
    if not isinstance(module, dict) or not isinstance(module.get("body"), list):
        return None
    module_span = node_span(module)
    if module_span is None:
        return None
    exact_program = original == context.source or (
        _without_version_header(original) == _without_version_header(context.source)
    )
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    protected_lines: set[int] = set()
    # Use an existing function-name index only to locate the candidate. Its
    # original signature and implementation must still match byte-for-byte.
    names = {}
    for row, name in context.facts.function_names.items():
        names[name] = None if name in names else row
    for function in module["body"]:
        if not isinstance(function, dict) or function.get("ast_type") != "FunctionDef":
            continue
        row = names.get(function.get("name"))
        span = node_span(function)
        if (
            not row
            or span is None
            or span.source_id != module_span.source_id
            or not 0 <= span.start < span.end <= len(original)
        ):
            continue
        start = context.line_offsets[row - 1]
        last_newline = original.find("\n", span.end)
        end = len(original) if last_newline < 0 else last_newline + 1
        original_function = original[span.start : end]
        candidate_end = start + len(original_function)
        if (
            not context.source.startswith(original_function, start)
            or not context.code_mask[start]
            or not original_function.startswith("def ")
            or (
                last_newline < 0
                and context.source[candidate_end : candidate_end + 1] not in {"", "\n"}
            )
        ):
            continue
        # A matching prefix alone can conceal a newly appended statement.
        following = _NEXT_CODE_LINE.search(context.source, candidate_end)
        if following and following.group(1):
            continue
        delta = start - span.start
        planned = _function_edits(context, function, by_id, original, delta, exact_program)
        if planned is None:
            continue
        local_edits, local_fixes = planned
        edits.extend(local_edits)
        fixes.extend(local_fixes)
        last_row = line_number(context.source, candidate_end - 1)
        protected_lines.update(range(row, last_row + 1))
    if not protected_lines:
        return None
    return apply_edits(context.source, edits), fixes, protected_lines


def _function_edits(
    context: RuleContext,
    function: dict[str, Any],
    by_id: dict[int, dict],
    original: str,
    delta: int,
    exact_program: bool,
) -> tuple[list[TextEdit], list[Fix]] | None:
    function_span = node_span(function)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    ranges: list[tuple[int, int]] = []
    postfix_calls = set()
    for parent in iter_nodes(function):
        if parent.get("ast_type") not in {"Subscript", "Attribute"}:
            continue
        base = parent.get("value")
        if isinstance(base, dict) and base.get("ast_type") == "Call":
            # When a caller supplied parentheses, the parent starts at the
            # opening paren rather than at the nested Call; reuse those parens.
            outer = node_span(parent)
            inner = node_span(base)
            if inner is not None and outer is not None and outer.start == inner.start:
                postfix_calls.add(base.get("node_id"))
    for node in iter_nodes(function, "Call"):
        func = node.get("func")
        if not isinstance(func, dict):
            return None
        if func.get("ast_type") != "Attribute":
            continue
        receiver = func.get("value")
        if not isinstance(receiver, dict):
            return None
        if receiver.get("ast_type") == "Name" and receiver.get("id") == "self":
            continue
        site = by_id.get(node.get("node_id"))
        span = node_span(node)
        if (
            site is None
            or site.get("src") != node.get("src")
            or site.get("method") != func.get("attr")
            or span is None
            or span.source_id != function_span.source_id
            or not function_span.start <= span.start < span.end <= function_span.end
        ):
            return None
        mode = site.get("mutability")
        if mode not in ("view", "pure", "nonpayable", "payable"):
            return None
        if not exact_program:
            # Another pass can intentionally change a local interface's call
            # mode (VY014). Unknown imports also lack target-side evidence.
            methods = context.facts.interfaces.get(site.get("interface"), {})
            if methods.get(site["method"]) != mode:
                return None
        start, end = span.start + delta, span.end + delta
        before = original[span.start : span.end]
        if context.source[start:end] != before:
            return None
        ranges.append((start, end))
        keyword, code = (
            ("staticcall", "VY041") if mode in ("view", "pure") else ("extcall", "VY040")
        )
        if not context.is_enabled(code):
            continue
        line = line_number(context.source, start)
        after = f"{keyword} {before}"
        fixes.append(Fix(code, line, f"added {keyword} to {mode} external call", before, after))
        if node["node_id"] in postfix_calls and context.is_enabled("VY042"):
            edits.extend((TextEdit(start, start, f"({keyword} "), TextEdit(end, end, ")")))
            fixes.append(
                Fix(
                    "VY042",
                    line,
                    "parenthesized external call before member access",
                    after,
                    f"({after})",
                )
            )
        else:
            edits.append(TextEdit(start, start, keyword + " "))

    # A nested receiver can give two Call nodes the same start and require
    # precedence-sensitive parentheses. Delegate nested calls to the legacy pass.
    ranges.sort()
    if not ranges or any(end > right for (_, end), (right, _) in pairwise(ranges)):
        return None
    return edits, fixes
