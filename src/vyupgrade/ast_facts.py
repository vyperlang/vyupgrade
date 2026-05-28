from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class SourceSpan:
    start: int
    length: int
    source_id: int

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(frozen=True)
class AstCall:
    name: str
    span: SourceSpan | None
    args: tuple[dict[str, Any], ...]
    node: dict[str, Any]


def root_ast(output: dict[str, Any]) -> dict[str, Any]:
    ast = output.get("ast", output)
    if not isinstance(ast, dict):
        raise TypeError("Vyper AST output must contain an object")
    return ast


def iter_nodes(node: dict[str, Any], ast_type: str | None = None) -> Iterator[dict[str, Any]]:
    if ast_type is None or node.get("ast_type") == ast_type:
        yield node
    for value in node.values():
        if isinstance(value, dict):
            yield from iter_nodes(value, ast_type)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from iter_nodes(item, ast_type)


def node_span(node: dict[str, Any]) -> SourceSpan | None:
    raw = node.get("src")
    if not isinstance(raw, str):
        return None
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    try:
        return SourceSpan(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def source_segment(source: str, span: SourceSpan | None) -> str | None:
    if span is None:
        return None
    return source[span.start : span.end]


def integer_constants(output: dict[str, Any]) -> dict[str, int]:
    constants: dict[str, int] = {}
    for node in iter_nodes(root_ast(output), "VariableDecl"):
        if not node.get("is_constant"):
            continue
        name = _name_id(node.get("target"))
        value = node.get("value")
        if name is None or not isinstance(value, dict) or value.get("ast_type") != "Int":
            continue
        raw_value = value.get("value")
        if isinstance(raw_value, int):
            constants[name] = raw_value
    return constants


def calls(output: dict[str, Any], name: str | None = None) -> Iterator[AstCall]:
    for node in iter_nodes(root_ast(output), "Call"):
        call_name = _call_name(node.get("func"))
        if call_name is None or (name is not None and call_name != name):
            continue
        args = node.get("args", [])
        if not isinstance(args, list):
            args = []
        yield AstCall(call_name, node_span(node), tuple(arg for arg in args if isinstance(arg, dict)), node)


def _call_name(func: Any) -> str | None:
    if not isinstance(func, dict):
        return None
    if func.get("ast_type") == "Name":
        return _name_id(func)
    if func.get("ast_type") == "Attribute":
        attr = func.get("attr")
        return attr if isinstance(attr, str) else None
    return None


def _name_id(node: Any) -> str | None:
    if not isinstance(node, dict) or node.get("ast_type") != "Name":
        return None
    name = node.get("id")
    return name if isinstance(name, str) else None
