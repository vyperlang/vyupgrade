"""Name positional custom error arguments using the source compiler's declaration."""

from __future__ import annotations

import ast
import re
from typing import Any

from ..ast_facts import CompilerSource, SourceSpan, iter_nodes
from ..models import Diagnostic, Fix
from ..rule_registry import Rule, RuleContext, crossing
from ..source import (
    TextEdit,
    apply_edits,
    find_matching_open,
    lone_cr_as_lf,
    split_top_level_arg_spans,
)


# An argument can start with parentheses omitted from its AST span. Skip only
# trivia so the field name lands outside those parentheses.
_LEADING_TRIVIA = re.compile(r"(?:\s+|\#[^\n]*(?:\n|$)|\\\r?\n)*")


def _field_names(node: dict[str, Any]) -> tuple[str, ...] | None:
    body = node.get("body")
    if not isinstance(body, list):
        return None
    names = []
    for field in body:
        if not isinstance(field, dict) or field.get("ast_type") != "AnnAssign":
            return None
        target = field.get("target")
        if not isinstance(target, dict) or target.get("ast_type") != "Name":
            return None
        name = target.get("id")
        if not isinstance(name, str) or not name.isidentifier() or name in names:
            return None
        names.append(name)
    return tuple(names)


def _declarations(modules: list[Any]) -> dict[tuple[int, int], tuple[str, ...] | None]:
    declarations = {}
    for module in modules:
        if not isinstance(module, dict) or module.get("ast_type") != "Module":
            continue
        source_id, body = module.get("source_id"), module.get("body")
        if type(source_id) is not int or not isinstance(body, list):
            continue
        for node in body:
            if not isinstance(node, dict) or node.get("ast_type") != "ErrorDef":
                continue
            node_id = node.get("node_id")
            if type(node_id) is int:
                key = source_id, node_id
                declarations[key] = None if key in declarations else _field_names(node)
    return declarations


def _same_callee(text: str, node: Any) -> bool:
    # Python parses Vyper's dotted names, including comments, grouping and NFKC
    # identifier normalization. Check the spelling at the proposed edit site.
    try:
        expr = ast.parse(f"({text}\n)", mode="eval").body
    except (SyntaxError, ValueError):
        return False
    while isinstance(expr, ast.Attribute) and isinstance(node, dict):
        if node.get("ast_type") != "Attribute" or node.get("attr") != expr.attr:
            return False
        expr, node = expr.value, node.get("value")
    return (
        isinstance(expr, ast.Name)
        and isinstance(node, dict)
        and node.get("ast_type") == "Name"
        and node.get("id") == expr.id
    )


def _name_arguments(
    source: CompilerSource, call: SourceSpan, node: dict[str, Any], fields: tuple[str, ...]
) -> str | None:
    args = node["args"]
    original = source.original[call.start : call.end]
    text = lone_cr_as_lf(original)  # Keep offsets and every original line ending.
    if not text.endswith(")"):
        return None
    opening = find_matching_open(text, len(text) - 1)
    if opening is None or not _same_callee(text[:opening], node.get("func")):
        return None
    parts = split_top_level_arg_spans(text[opening + 1 : -1])
    if parts is None:
        return None
    bounds = []
    for start, end, raw in parts:
        first = _LEADING_TRIVIA.match(raw).end()
        if first < len(raw):  # A comment after a trailing comma has no argument.
            bounds.append((opening + 1 + start + first, opening + 1 + end))
    if len(bounds) != len(fields) or len(args) != len(fields):
        return None

    edits = []
    for (start, end), arg, field in zip(bounds, args, fields, strict=True):
        span = (
            source.span(arg, source_id=call.source_id, byte_columns=True)
            if isinstance(arg, dict)
            else None
        )
        if span is None or not (call.start + start <= span.start < span.end <= call.start + end):
            return None
        edits.append(TextEdit(start, start, f"{field}="))
    return apply_edits(original, edits)


def _rewrite_positional_custom_errors(
    context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = context.source
    output = context.compiler_facts.annotated_ast or {}
    root = output.get("ast")
    if not isinstance(root, dict) or root.get("ast_type") != "Module":
        return source, [], []
    compiler_source = CompilerSource(source)
    if not compiler_source.matches_digest(root.get("source_sha256sum")):
        return source, [], []
    imports = output.get("imports", [])
    if not isinstance(imports, list):
        return source, [], []
    declarations = _declarations([root, *imports])
    edits, fixes = [], []
    for call in iter_nodes(root, "Call"):
        typ = call.get("type")
        if (
            not isinstance(typ, dict)
            or typ.get("typeclass") != "error"
            or call.get("keywords") != []
        ):
            continue
        ref = typ.get("type_decl_node")
        if (
            not isinstance(ref, dict)
            or type(ref.get("source_id")) is not int
            or type(ref.get("node_id")) is not int
        ):
            continue
        fields = declarations.get((ref["source_id"], ref["node_id"]))
        args = call.get("args")
        span = compiler_source.span(call, source_id=root.get("source_id"), byte_columns=True)
        if not fields or not isinstance(args, list) or span is None:
            continue
        after = _name_arguments(compiler_source, span, call, fields)
        if after is not None:
            before = source[span.start : span.end]
            edits.append(TextEdit(span.start, span.end, after))
            fixes.append(
                Fix(
                    "VY127",
                    call["lineno"],
                    "named custom error arguments",
                    before,
                    after,
                )
            )
    try:
        rewritten = apply_edits(source, edits)
    except ValueError:
        # Conflicting compiler spans cannot identify a unique source edit.
        return source, [], []
    return rewritten, fixes, []


RULES = (
    Rule(
        "positional_custom_errors",
        _rewrite_positional_custom_errors,
        changes=(crossing("VY127", "0.5.0b2"),),
    ),
)
