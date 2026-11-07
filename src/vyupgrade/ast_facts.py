from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any


@dataclass(frozen=True)
class SourceSpan:
    start: int
    length: int
    source_id: int

    @property
    def end(self) -> int:
        return self.start + self.length


def _compiler_text(source: str) -> str:
    # Vyper's CLI reads in text mode with universal newlines.
    return source.replace("\r\n", "\n").replace("\r", "\n")


def compiler_source_sha256(source: str) -> str:
    """Digest of the compiler text. Byte attestations hash the original separately."""
    return sha256(_compiler_text(source).encode("utf-8")).hexdigest()


class CompilerSource:
    """Translate source compiler positions into offsets in the captured UTF-8 text."""

    def __init__(self, original: str) -> None:
        self.original = original
        self.normalized = _compiler_text(original)
        self._original_lines = [0]
        self._compiler_lines = [0]
        collapsed = 0
        for newline in re.finditer(r"\r\n|[\r\n]", original):
            collapsed += len(newline[0]) - 1
            self._original_lines.append(newline.end())
            self._compiler_lines.append(newline.end() - collapsed)

    def matches_digest(self, digest: object) -> bool:
        return (
            isinstance(digest, str)
            and sha256(self.normalized.encode("utf-8")).hexdigest() == digest
        )

    def original_offset(self, normalized_offset: int) -> int | None:
        if type(normalized_offset) is not int or not 0 <= normalized_offset <= len(self.normalized):
            return None
        row = bisect_right(self._compiler_lines, normalized_offset) - 1
        return self._original_lines[row] + normalized_offset - self._compiler_lines[row]

    def _position(
        self, row: Any, column: Any, raw_position: int, *, byte_columns: bool
    ) -> int | None:
        if (
            type(row) is not int
            or not 1 <= row <= len(self._compiler_lines)
            or type(column) is not int
        ):
            return None
        start = self._compiler_lines[row - 1]
        stop = (
            self._compiler_lines[row] - 1
            if row < len(self._compiler_lines)
            else len(self.normalized)
        )
        if start + column != raw_position or column < 0:
            return None
        if not byte_columns:
            return self._original_lines[row - 1] + column if start + column <= stop else None
        line = self.normalized[start:stop].encode("utf-8")
        if column > len(line):
            return None
        try:
            return self._original_lines[row - 1] + len(line[:column].decode("utf-8"))
        except UnicodeDecodeError:
            return None

    def span(
        self, node: dict[str, Any], *, source_id: int, byte_columns: bool = False
    ) -> SourceSpan | None:
        """0.3.10 src counts characters. b1 adds UTF-8 columns to character line starts."""
        raw = node_span(node)
        if type(source_id) is not int or source_id < 0 or raw is None or raw.source_id != source_id:
            return None
        if byte_columns or any(
            k in node for k in ("lineno", "col_offset", "end_lineno", "end_col_offset")
        ):
            start = self._position(
                node.get("lineno"), node.get("col_offset"), raw.start, byte_columns=byte_columns
            )
            end = self._position(
                node.get("end_lineno"),
                node.get("end_col_offset"),
                raw.end,
                byte_columns=byte_columns,
            )
        else:
            start = self.original_offset(raw.start)
            end = self.original_offset(raw.end)
        if start is None or end is None or start >= end:
            return None
        return SourceSpan(start, end - start, source_id)


@dataclass(frozen=True)
class AstCall:
    name: str
    span: SourceSpan | None
    args: tuple[dict[str, Any], ...]
    node: dict[str, Any]


def root_ast(output: dict[str, Any]) -> dict[str, Any]:
    ast = output.get("ast", output)
    if isinstance(ast, list):
        return {"ast_type": "Module", "body": ast}
    if not isinstance(ast, dict):
        raise TypeError("Vyper AST output must contain an object or legacy body list")
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
    for node in iter_nodes(root_ast(output), "AnnAssign"):
        if not _legacy_constant_annotation(node.get("annotation")):
            continue
        name = _name_id(node.get("target"))
        value = node.get("value")
        raw_value = _integer_literal(value)
        if name is not None and raw_value is not None:
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
        yield AstCall(
            call_name, node_span(node), tuple(arg for arg in args if isinstance(arg, dict)), node
        )


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


def _legacy_constant_annotation(node: Any) -> bool:
    if not isinstance(node, dict) or node.get("ast_type") != "Call":
        return False
    func = node.get("func")
    return (
        isinstance(func, dict) and func.get("ast_type") == "Name" and func.get("id") == "constant"
    )


def _integer_literal(node: Any) -> int | None:
    if not isinstance(node, dict):
        return None
    if node.get("ast_type") == "Int":
        value = node.get("value")
        return value if isinstance(value, int) else None
    if node.get("ast_type") == "Num":
        value = node.get("n")
        return value if isinstance(value, int) else None
    if node.get("ast_type") == "UnaryOp":
        value = _integer_literal(node.get("operand"))
        if value is None:
            return None
        op = node.get("op")
        if isinstance(op, dict) and op.get("ast_type") == "USub":
            return -value
        if isinstance(op, dict) and op.get("ast_type") == "UAdd":
            return value
    return None
