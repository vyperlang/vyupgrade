"""Parse and compare compiler-produced Vyper storage layouts.

This module is intentionally independent from compiler process orchestration.
It accepts raw compiler artifacts, validates and canonicalizes them fail-closed,
then compares the typed layouts without importing :mod:`vyupgrade.compiler`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import NamedTuple


__all__ = (
    "StorageLayout",
    "StorageLayoutComparison",
    "StorageValue",
    "compare_storage_layouts",
    "parse_storage_layout",
)


UINT256_MAX_DECIMAL = str(2**256 - 1)
UINT256_LIMIT = 2**256
LAYOUT_WRAPPER_KEYS = frozenset({"storage_layout", "transient_storage_layout", "code_layout"})
STORAGE_LEAF_KEYS = frozenset({"slot", "type", "location", "n_slots"})
GENERATED_REENTRANCY_GAP_RE = re.compile(r"^_vyupgrade_reentrancy_lock_slot(?:_[1-9][0-9]*)?$")
STORAGE_PATH_ATOM_RE = re.compile(
    r"(?P<path>(?:[^,\[\]\n]+[/\\])+)(?P<base>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<extension>\.vyi?)?(?=[ \t]*(?:$|[,\[\]\)]))"
)
PLAIN_STORAGE_TYPE_FILE_RE = re.compile(
    r"(?P<base>[A-Za-z_][A-Za-z0-9_]*)(?P<extension>\.vyi?)"
    r"(?=[ \t]*(?:$|[,\[\]\)]))"
)


@dataclass(frozen=True, slots=True)
class _StorageLayoutEntry:
    name: str
    slot: int
    type_name: str
    location: str
    n_slots: int | None


class StorageValue(NamedTuple):
    """One canonical storage variable."""

    slot: int
    type_name: str
    n_slots: int | None


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """Canonical persistent and transient storage namespaces."""

    persistent: dict[str, StorageValue]
    transient: dict[str, StorageValue]


@dataclass(frozen=True, slots=True)
class StorageLayoutComparison:
    """The semantic result and complete diagnostics for one layout comparison."""

    equal: bool
    differences: tuple[str, ...]


_StorageInterfaceMarker = tuple[tuple[int, ...], str]


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _normalize_storage_slot(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < UINT256_LIMIT else None
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = int(value, 0)
    except ValueError:
        return None
    return normalized if 0 <= normalized < UINT256_LIMIT else None


def _normalize_storage_size(value: object) -> int | None:
    normalized = _normalize_storage_slot(value)
    return normalized if normalized is not None and normalized > 0 else None


def _classify_layout_artifact(
    value: object,
) -> tuple[tuple[_StorageLayoutEntry, ...], tuple[_StorageLayoutEntry, ...]] | None:
    """Return complete persistent/transient layouts, or fail closed.

    Vyper <=0.3 emitted a flat mapping while Vyper 0.4 module layouts are
    recursively namespaced below ``storage_layout``.  This classifier is shared
    by artifact validation and canonicalization so a node cannot be accepted by
    one path and silently discarded by the other.
    """
    if not isinstance(value, dict):
        return None
    legacy_wrapper_leaves = _legacy_wrapper_leaf_names(value)
    if legacy_wrapper_leaves is None:
        return None
    has_storage_wrapper = (
        "storage_layout" in value and "storage_layout" not in legacy_wrapper_leaves
    )
    has_transient_wrapper = (
        "transient_storage_layout" in value
        and "transient_storage_layout" not in legacy_wrapper_leaves
    )
    has_code_wrapper = "code_layout" in value and "code_layout" not in legacy_wrapper_leaves
    if has_storage_wrapper or has_transient_wrapper or has_code_wrapper:
        if legacy_wrapper_leaves or value.keys() - LAYOUT_WRAPPER_KEYS:
            return None
        storage = value.get("storage_layout", {})
        transient = value.get("transient_storage_layout", {})
        persistent_entries = _classify_storage_namespace(storage, location="storage")
        transient_entries = _classify_storage_namespace(transient, location="transient")
        code_entries = _classify_code_namespace(value.get("code_layout", {}))
        if (
            persistent_entries is None
            or transient_entries is None
            or code_entries is None
            or not _valid_layout_entry_sets(persistent_entries, transient_entries)
            or not _valid_code_entry_spans(code_entries)
        ):
            return None
        return persistent_entries, transient_entries

    entries = _classify_storage_namespace(value)
    if entries is None:
        return None
    persistent_entries = tuple(entry for entry in entries if entry.location == "storage")
    transient_entries = tuple(entry for entry in entries if entry.location == "transient")
    if not _valid_layout_entry_sets(persistent_entries, transient_entries):
        return None
    return persistent_entries, transient_entries


def _storage_leaf_node(value: object) -> bool:
    return isinstance(value, dict) and any(
        field in value and not isinstance(value[field], dict) for field in ("slot", "type")
    )


def _legacy_wrapper_leaf_names(value: dict[str, object]) -> set[str] | None:
    legacy_names: set[str] = set()
    for name in value.keys() & LAYOUT_WRAPPER_KEYS:
        node = value[name]
        if not _storage_leaf_node(node):
            continue
        if not isinstance(node, dict) or node.get("location") not in {"storage", "transient"}:
            return None
        legacy_names.add(name)
    return legacy_names


def _classify_storage_namespace(
    value: object,
    prefix: tuple[str, ...] = (),
    *,
    location: str | None = None,
) -> tuple[_StorageLayoutEntry, ...] | None:
    if not isinstance(value, dict):
        return None
    if prefix and not value:
        return None
    entries: list[_StorageLayoutEntry] = []
    for name, node in value.items():
        if not _nonempty_string(name) or not isinstance(node, dict):
            return None

        # A scalar slot or type marks a leaf.  Dict-valued keys named ``slot``
        # or ``type`` remain valid namespace/variable names.
        if _storage_leaf_node(node):
            if node.keys() - STORAGE_LEAF_KEYS:
                return None
            slot = _normalize_storage_slot(node.get("slot"))
            type_name = node.get("type")
            leaf_location = node.get("location", location or "storage")
            has_n_slots = "n_slots" in node
            raw_n_slots = node.get("n_slots")
            n_slots = _normalize_storage_size(raw_n_slots) if has_n_slots else None
            if (
                slot is None
                or not _nonempty_string(type_name)
                or leaf_location not in {"storage", "transient"}
                or (location is not None and leaf_location != location)
                or (has_n_slots and n_slots is None)
                or any(isinstance(field, (dict, list)) for field in node.values())
            ):
                return None
            entries.append(
                _StorageLayoutEntry(
                    ".".join((*prefix, name)),
                    slot,
                    type_name,
                    leaf_location,
                    n_slots,
                )
            )
            continue

        nested = _classify_storage_namespace(node, (*prefix, name), location=location)
        if nested is None:
            return None
        entries.extend(nested)
    if len(entries) != len({entry.name for entry in entries}):
        return None
    return tuple(entries)


def _classify_code_namespace(
    value: object,
    prefix: tuple[str, ...] = (),
) -> tuple[tuple[str, int, int], ...] | None:
    if not isinstance(value, dict):
        return None
    if prefix and not value:
        return None
    entries: list[tuple[str, int, int]] = []
    for name, node in value.items():
        if not _nonempty_string(name) or not isinstance(node, dict):
            return None
        is_leaf = any(
            field in node and not isinstance(node[field], dict)
            for field in ("offset", "type", "length")
        )
        if is_leaf:
            if node.keys() != {"offset", "type", "length"}:
                return None
            offset = _normalize_storage_slot(node.get("offset"))
            length = _normalize_storage_size(node.get("length"))
            if offset is None or length is None or not _nonempty_string(node.get("type")):
                return None
            entries.append((".".join((*prefix, name)), offset, length))
            continue
        nested = _classify_code_namespace(node, (*prefix, name))
        if nested is None:
            return None
        entries.extend(nested)
    if len(entries) != len({name for name, _offset, _length in entries}):
        return None
    return tuple(entries)


def _valid_layout_entry_sets(
    persistent: tuple[_StorageLayoutEntry, ...],
    transient: tuple[_StorageLayoutEntry, ...],
) -> bool:
    entries = (*persistent, *transient)
    if entries and len({entry.n_slots is None for entry in entries}) != 1:
        return False
    return _valid_storage_entry_spans(persistent) and _valid_storage_entry_spans(transient)


def _valid_storage_entry_spans(entries: tuple[_StorageLayoutEntry, ...]) -> bool:
    spans: list[tuple[int, int]] = []
    for entry in entries:
        size = entry.n_slots or 1
        end = entry.slot + size
        if end > UINT256_LIMIT:
            return False
        spans.append((entry.slot, end))
    spans.sort()
    return all(current[0] >= previous[1] for previous, current in pairwise(spans))


def _valid_code_entry_spans(entries: tuple[tuple[str, int, int], ...]) -> bool:
    spans: list[tuple[int, int]] = []
    for _name, offset, length in entries:
        end = offset + length
        if end > UINT256_LIMIT:
            return False
        spans.append((offset, end))
    spans.sort()
    return all(current[0] >= previous[1] for previous, current in pairwise(spans))


def _target_storage_interface_markers(
    ast: object,
) -> dict[str, frozenset[_StorageInterfaceMarker]]:
    if not isinstance(ast, dict):
        return {}
    root = ast.get("ast", ast)
    if not isinstance(root, dict) or root.get("ast_type") != "Module":
        return {}
    body = root.get("body")
    if not isinstance(body, list):
        return {}
    interface_names = frozenset(
        name
        for node in body
        if isinstance(node, dict) and node.get("ast_type") == "InterfaceDef"
        if isinstance(name := node.get("name"), str) and name
    )
    evidence: dict[str, frozenset[_StorageInterfaceMarker]] = {}
    for node in body:
        if not isinstance(node, dict) or node.get("ast_type") not in {
            "VariableDecl",
            "AnnAssign",
        }:
            continue
        name = _ast_name(node.get("target"))
        if name is None:
            continue
        markers = _target_annotation_interface_markers(node.get("annotation"), interface_names)
        if markers:
            evidence[name] = markers
    return evidence


def _target_annotation_interface_markers(
    annotation: object,
    interface_names: frozenset[str],
) -> frozenset[_StorageInterfaceMarker]:
    if not isinstance(annotation, dict):
        return frozenset()
    ast_type = annotation.get("ast_type")
    if ast_type == "Name":
        name = _ast_name(annotation)
        if name in interface_names:
            return frozenset({((), name)})
        return frozenset()
    if ast_type == "Call":
        wrapper = _ast_name(annotation.get("func"))
        args = annotation.get("args")
        if (
            wrapper not in {"constant", "immutable", "public", "transient"}
            or not isinstance(args, list)
            or len(args) != 1
        ):
            return frozenset()
        return _target_annotation_interface_markers(args[0], interface_names)
    if ast_type != "Subscript":
        return frozenset()

    value = annotation.get("value")
    generic = _ast_name(value)
    args = _ast_subscript_args(annotation.get("slice"))
    if generic in {"HashMap", "DynArray"}:
        if len(args) != 2:
            return frozenset()
        markers: set[_StorageInterfaceMarker] = set()
        for index, arg in enumerate(args):
            markers.update(
                ((index, *path), name)
                for path, name in _target_annotation_interface_markers(arg, interface_names)
            )
        return frozenset(markers)

    return frozenset(
        ((-1, *path), name)
        for path, name in _target_annotation_interface_markers(value, interface_names)
    )


def _ast_name(node: object) -> str | None:
    if not isinstance(node, dict) or node.get("ast_type") != "Name":
        return None
    name = node.get("id")
    return name if isinstance(name, str) and name else None


def _ast_subscript_args(slice_node: object) -> list[object]:
    if not isinstance(slice_node, dict):
        return []
    if slice_node.get("ast_type") == "Index":
        return _ast_subscript_args(slice_node.get("value"))
    if slice_node.get("ast_type") == "Tuple":
        for field in ("elements", "elts"):
            values = slice_node.get(field)
            if isinstance(values, list):
                return values
        return []
    return [slice_node]


def parse_storage_layout(layout: object) -> StorageLayout | None:
    classified = _classify_layout_artifact(layout)
    if classified is None:
        return None
    storage_entries, transient_entries = classified
    storage = _normalize_layout_entries(storage_entries)
    transient = _normalize_layout_entries(transient_entries)
    if storage is None or transient is None:
        return None
    return StorageLayout(storage, transient)


def _normalize_layout_entries(
    entries: tuple[_StorageLayoutEntry, ...],
) -> dict[str, StorageValue] | None:
    normalized: dict[str, StorageValue] = {}
    for entry in entries:
        canonical_type, type_safe = _canonical_storage_type(entry.type_name)
        if not type_safe:
            return None
        inferred_width = _known_storage_width(canonical_type)
        if inferred_width == 0:
            return None
        if (
            inferred_width is not None
            and entry.n_slots is not None
            and entry.n_slots != inferred_width
        ):
            return None
        canonical_width = inferred_width if inferred_width is not None else entry.n_slots
        normalized_name = (
            f"$nonreentrant:{entry.slot}" if canonical_type == "nonreentrant lock" else entry.name
        )
        if normalized_name in normalized:
            return None
        normalized[normalized_name] = StorageValue(entry.slot, canonical_type, canonical_width)
    if not _valid_canonical_storage_spans(normalized):
        return None
    return normalized


def _valid_canonical_storage_spans(
    layout: dict[str, StorageValue],
) -> bool:
    spans: list[tuple[int, int]] = []
    for slot, _type_name, width in layout.values():
        end = slot + (width or 1)
        if end > UINT256_LIMIT:
            return False
        spans.append((slot, end))
    spans.sort()
    return all(current[0] >= previous[1] for previous, current in pairwise(spans))


def _known_storage_width(
    type_name: str,
) -> int | None:
    """Infer widths encoded by Vyper's stable storage type grammar.

    ``None`` means the canonical spelling does not carry enough structure to
    prove a width (for example, a bare user-defined struct name). ``0`` marks a
    recognized but invalid or uint256-overflowing type expression.
    """
    type_name = type_name.strip()
    if type_name == "nonreentrant lock":
        return 1
    if type_name in {"address", "bool", "decimal"}:
        return 1
    integer = re.fullmatch(r"u?int(?P<bits>[0-9]+)?", type_name)
    if integer is not None:
        bits = int(integer.group("bits") or "256")
        return 1 if 8 <= bits <= 256 and bits % 8 == 0 else 0
    fixed_bytes = re.fullmatch(r"bytes(?P<size>[0-9]+)", type_name)
    if fixed_bytes is not None:
        size = int(fixed_bytes.group("size"))
        return 1 if 1 <= size <= 32 else 0
    if re.fullmatch(r"interface\s+[A-Za-z_][A-Za-z0-9_]*", type_name):
        return 1

    bounded_bytes = re.fullmatch(r"(?:Bytes|String)\[(?P<size>[0-9]+)\]", type_name)
    if bounded_bytes is not None:
        size = int(bounded_bytes.group("size"))
        return _checked_storage_width(1 + (size + 31) // 32) if size > 0 else 0

    hashmap_args = _storage_generic_args(type_name, "HashMap")
    if hashmap_args is not None:
        return 1 if len(hashmap_args) == 2 and all(hashmap_args) else 0

    # Parse an outer fixed-array suffix before treating a leading DynArray as
    # malformed.  Vyper emits types such as ``DynArray[uint256, 3][3]``.
    fixed_array = re.fullmatch(r"(?P<element>.+)\[(?P<length>[0-9]+)\]", type_name)
    if fixed_array is not None:
        length = int(fixed_array.group("length"))
        element_width = _known_storage_width(
            fixed_array.group("element"),
        )
        if length <= 0 or element_width == 0:
            return 0
        if element_width is None:
            return None
        return _checked_storage_width(element_width * length)

    dynarray_args = _storage_generic_args(type_name, "DynArray")
    if dynarray_args is not None:
        if len(dynarray_args) != 2 or not dynarray_args[0]:
            return 0
        try:
            length = int(dynarray_args[1], 10)
        except ValueError:
            return 0
        element_width = _known_storage_width(
            dynarray_args[0],
        )
        if length <= 0 or element_width == 0:
            return 0
        if element_width is None:
            return None
        return _checked_storage_width(1 + element_width * length)

    return None


def _checked_storage_width(value: int) -> int:
    return value if 0 < value < UINT256_LIMIT else 0


def _storage_generic_args(type_name: str, generic: str) -> list[str] | None:
    prefix = f"{generic}["
    if not type_name.startswith(prefix):
        return None
    open_index = len(generic)
    close_index = _matching_square_bracket(type_name, open_index)
    if close_index != len(type_name) - 1:
        return []
    return _split_storage_type_args(type_name[open_index + 1 : close_index])


def _split_storage_type_args(value: str) -> list[str]:
    args: list[str] = []
    start = 0
    stack: list[str] = []
    pairs = {"]": "[", ")": "("}
    for index, char in enumerate(value):
        if char in "[(":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return []
        elif char == "," and not stack:
            args.append(value[start:index].strip())
            start = index + 1
    if stack:
        return []
    args.append(value[start:].strip())
    return args


def _canonical_storage_type(type_name: str) -> tuple[str, bool]:
    original_type = type_name
    type_name, paths_safe = _normalize_storage_type_paths(type_name)
    if not paths_safe:
        # A comma or bracket inside a path segment is indistinguishable from a
        # type delimiter without compiler-owned structure.  Preserve the
        # complete spelling so comparison fails closed instead of partially
        # stripping a suffix from the path.
        return original_type, False
    type_name = type_name.replace(" declaration object", "")
    type_name = re.sub(
        r"\b(?:enum|flag) ([A-Za-z_][A-Za-z0-9_]*)\([^][]*\)",
        r"\1",
        type_name,
    )
    type_name = _canonical_max_value_arrays(type_name)
    type_name = _strip_legacy_hashmap_storage_suffixes(type_name)
    return type_name, True


def _normalize_storage_type_paths(type_name: str) -> tuple[str, bool]:
    output: list[str] = []
    index = 0
    at_atom_start = True
    while index < len(type_name):
        if at_atom_start:
            while index < len(type_name) and type_name[index] in " \t":
                output.append(type_name[index])
                index += 1
            path_match = STORAGE_PATH_ATOM_RE.match(type_name, index)
            if path_match is not None:
                if _path_match_has_ambiguous_comma_suffix(
                    type_name, path_match.end()
                ) or _path_match_has_ambiguous_array_suffix(type_name, path_match.end()):
                    return type_name, False
                output.append(_canonical_file_type_atom(path_match))
                index = path_match.end()
                at_atom_start = False
                continue
            file_match = PLAIN_STORAGE_TYPE_FILE_RE.match(type_name, index)
            if file_match is not None:
                output.append(_canonical_file_type_atom(file_match))
                index = file_match.end()
                at_atom_start = False
                continue
            if index < len(type_name) and (
                type_name[index] in "/\\"
                or (
                    index + 2 < len(type_name)
                    and type_name[index].isalpha()
                    and type_name[index + 1] == ":"
                    and type_name[index + 2] in "/\\"
                )
            ):
                return type_name, False

        char = type_name[index]
        output.append(char)
        index += 1
        at_atom_start = char in "[,"
    normalized = "".join(output)
    if not _valid_storage_type_delimiters(normalized):
        return type_name, False
    return normalized, True


def _canonical_file_type_atom(match: re.Match[str]) -> str:
    base = match.group("base")
    if match.group("extension") == ".vyi":
        return f"interface {base}"
    # Only a .vyi suffix is compiler-owned evidence that this path names an
    # interface handle.  Preserve extensionless and .vy paths so unrelated
    # modules or structs with the same basename cannot compare equal.
    return match.group(0)


def _path_match_has_ambiguous_comma_suffix(type_name: str, end: int) -> bool:
    cursor = end
    while cursor < len(type_name) and type_name[cursor] in " \t":
        cursor += 1
    if cursor >= len(type_name) or type_name[cursor] != ",":
        return False
    next_delimiter = re.search(r"[,\[\]]", type_name[cursor + 1 :])
    segment_end = len(type_name) if next_delimiter is None else cursor + 1 + next_delimiter.start()
    return any(separator in type_name[cursor + 1 : segment_end] for separator in "/\\")


def _path_match_has_ambiguous_array_suffix(type_name: str, end: int) -> bool:
    cursor = end
    while cursor < len(type_name) and type_name[cursor] in " \t":
        cursor += 1
    if cursor >= len(type_name) or type_name[cursor] != "[":
        return False
    close_index = _matching_square_bracket(type_name, cursor)
    if close_index is None:
        return True
    length = type_name[cursor + 1 : close_index].strip()
    if not length.isdigit():
        return True
    next_index = close_index + 1
    while next_index < len(type_name) and type_name[next_index] in " \t":
        next_index += 1
    return next_index < len(type_name) and type_name[next_index] in "/\\"


def _valid_storage_type_delimiters(type_name: str) -> bool:
    stack: list[str] = []
    pairs = {"]": "[", ")": "("}
    for char in type_name:
        if char in "[(":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return False
        elif char == "," and not stack:
            return False
    if stack:
        return False

    for match in re.finditer(r"\b(?:HashMap|DynArray)\s*\[", type_name):
        open_index = type_name.find("[", match.start())
        close_index = _matching_square_bracket(type_name, open_index)
        if close_index is None:
            return False
        depth = 0
        commas = 0
        for char in type_name[open_index + 1 : close_index]:
            if char in "[(":
                depth += 1
            elif char in "])":
                depth -= 1
            elif char == "," and depth == 0:
                commas += 1
        if commas != 1:
            return False
    return True


def _canonical_max_value_arrays(type_name: str) -> str:
    pattern = re.compile(
        rf"\b(?P<element>[A-Za-z_][A-Za-z0-9_]*|u?int(?:\d+)?|bytes(?:\d+)?|address|bool)"
        rf"\[{UINT256_MAX_DECIMAL}\]"
    )
    while True:
        updated = pattern.sub(r"HashMap[uint256, \g<element>]", type_name)
        if updated == type_name:
            return type_name
        type_name = updated


def _strip_legacy_hashmap_storage_suffixes(type_name: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(type_name):
        if not type_name.startswith("HashMap[", index):
            output.append(type_name[index])
            index += 1
            continue
        open_index = index + len("HashMap")
        close_index = _matching_square_bracket(type_name, open_index)
        if close_index is None:
            output.append(type_name[index])
            index += 1
            continue
        inner = _strip_legacy_hashmap_storage_suffixes(type_name[open_index + 1 : close_index])
        output.append(f"HashMap[{inner}]")
        index = close_index + 1
        if index < len(type_name) and type_name[index] == "[":
            suffix_end = _matching_square_bracket(type_name, index)
            if suffix_end is not None:
                index = suffix_end + 1
    return "".join(output)


def _matching_square_bracket(value: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(value)):
        char = value[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
    return None


def compare_storage_layouts(
    source: StorageLayout,
    target: StorageLayout,
    *,
    target_ast: object | None = None,
) -> StorageLayoutComparison:
    """Compare two parsed layouts using target AST interface evidence."""

    differences = tuple(
        _storage_layout_diff(
            source.persistent,
            source.transient,
            target.persistent,
            target.transient,
            target_interface_markers=_target_storage_interface_markers(target_ast),
        )
    )
    return StorageLayoutComparison(
        equal=all(line.startswith("moved storage to transient: ") for line in differences),
        differences=differences,
    )


def _storage_layout_diff(
    source: dict[str, StorageValue],
    source_transient: dict[str, StorageValue],
    target: dict[str, StorageValue],
    target_transient: dict[str, StorageValue],
    *,
    target_interface_markers: Mapping[str, frozenset[_StorageInterfaceMarker]] | None = None,
) -> list[str]:
    interface_markers = target_interface_markers or {}
    moved_locks = _moved_nonreentrant_locks(
        source,
        target,
        source_transient,
        target_transient,
    )
    preserved_lock_gaps = _counterpart_nonreentrant_storage_gaps(
        source,
        target,
        excluded_source_names=set(moved_locks),
    )
    moved_lock_names = set(moved_locks) | set(preserved_lock_gaps)
    preserved_gap_names = set(preserved_lock_gaps.values())
    moved_transient_names = set(moved_locks.values())
    return [
        *(
            f"moved storage to transient: {name} slot {_slot_type(source[name])} -> {target_name} slot {_slot_type(target_transient[target_name])}"
            for name, target_name in moved_locks.items()
        ),
        *_mapping_diff_lines(
            source,
            target,
            removed=lambda key, value: f"removed storage: {key} slot {_slot_type(value)}",
            added=lambda key, value: f"added storage: {key} slot {_slot_type(value)}",
            changed=lambda key, before, after: [
                _changed_storage_line("storage", key, before, after)
            ],
            skip_removed=moved_lock_names,
            skip_added=preserved_gap_names,
            equivalent=lambda key, source_value, target_value: _storage_values_equal(
                source_value,
                target_value,
                target_interface_markers=interface_markers.get(key, frozenset()),
            ),
        ),
        *_mapping_diff_lines(
            source_transient,
            target_transient,
            removed=lambda key, value: f"removed transient storage: {key} slot {_slot_type(value)}",
            added=lambda key, value: f"added transient storage: {key} slot {_slot_type(value)}",
            changed=lambda key, before, after: [
                _changed_storage_line("transient storage", key, before, after)
            ],
            skip_added=moved_transient_names,
            equivalent=lambda key, source_value, target_value: _storage_values_equal(
                source_value,
                target_value,
                target_interface_markers=interface_markers.get(key, frozenset()),
            ),
        ),
    ]


def _mapping_diff_lines(
    source: Mapping[str, object],
    target: Mapping[str, object],
    *,
    removed: Callable[[str, object], str],
    added: Callable[[str, object], str],
    changed: Callable[[str, object, object], list[str]],
    skip_removed: set[str] | None = None,
    skip_added: set[str] | None = None,
    equivalent: Callable[[str, object, object], bool] | None = None,
) -> list[str]:
    skip_removed = skip_removed or set()
    skip_added = skip_added or set()
    lines = [
        removed(key, source[key]) for key in sorted((source.keys() - target.keys()) - skip_removed)
    ]
    lines.extend(
        added(key, target[key]) for key in sorted((target.keys() - source.keys()) - skip_added)
    )
    for key in sorted(source.keys() & target.keys()):
        if not (
            equivalent(key, source[key], target[key]) if equivalent else source[key] == target[key]
        ):
            lines.extend(changed(key, source[key], target[key]))
    return lines


def _moved_nonreentrant_locks(
    source: dict[str, StorageValue],
    target: dict[str, StorageValue],
    source_transient: dict[str, StorageValue],
    target_transient: dict[str, StorageValue],
) -> dict[str, str]:
    transient_locks = {
        name: value
        for name, value in sorted(target_transient.items())
        if name not in source_transient and value[1] == "nonreentrant lock"
    }
    moved: dict[str, str] = {}
    for name, value in sorted(source.items()):
        if name in target:
            continue
        if not name.startswith("$nonreentrant:") or value[1] != "nonreentrant lock":
            continue
        candidates = [
            target_name
            for target_name, target_value in transient_locks.items()
            if _storage_move_compatible(value, target_value)
        ]
        if not candidates:
            continue
        target_name = name if name in candidates else candidates[0]
        moved[name] = target_name
        transient_locks.pop(target_name)
    return moved


def _counterpart_nonreentrant_storage_gaps(
    source: dict[str, StorageValue],
    target: dict[str, StorageValue],
    *,
    excluded_source_names: set[str],
) -> dict[str, str]:
    candidates = {
        name: value
        for name, value in target.items()
        if name not in source
        and GENERATED_REENTRANCY_GAP_RE.fullmatch(name)
        and value[1] == "uint256"
    }
    preserved: dict[str, str] = {}
    for source_name, source_value in sorted(source.items()):
        if (
            source_name in target
            or source_name in excluded_source_names
            or not source_name.startswith("$nonreentrant:")
            or source_value[1] != "nonreentrant lock"
            or not _known_single_slot(source_value)
        ):
            continue
        matches = [
            target_name
            for target_name, target_value in candidates.items()
            if source_value[0] == target_value[0]
            and _known_single_slot(target_value)
            and source_value[2] == target_value[2]
        ]
        if len(matches) != 1:
            continue
        target_name = matches[0]
        preserved[source_name] = target_name
        candidates.pop(target_name)
    return preserved


def _storage_move_compatible(
    source: StorageValue,
    target: StorageValue,
) -> bool:
    return (
        source[1] == target[1] == "nonreentrant lock"
        and _known_single_slot(source)
        and _known_single_slot(target)
        and source[2] == target[2]
    )


def _known_single_slot(value: StorageValue) -> bool:
    return value[2] == 1


def _storage_values_equal(
    source: object,
    target: object,
    *,
    target_interface_markers: frozenset[_StorageInterfaceMarker] = frozenset(),
) -> bool:
    if not isinstance(source, tuple) or not isinstance(target, tuple):
        return source == target
    source_slot, source_type, source_size = source
    target_slot, target_type, target_size = target
    return (
        source_slot == target_slot
        and source_size == target_size
        and _storage_types_equal(
            source_type,
            target_type,
            target_interface_markers=target_interface_markers,
        )
    )


def _storage_types_equal(
    source: str,
    target: str,
    *,
    target_interface_markers: frozenset[_StorageInterfaceMarker],
) -> bool:
    if source == target:
        return True
    source_profile = _storage_interface_marker_profile(source)
    target_profile = _storage_interface_marker_profile(target)
    if source_profile is None or target_profile is None:
        return False
    source_shape, source_markers = source_profile
    target_shape, target_markers = target_profile
    omitted_markers = source_markers - target_markers
    return (
        source_shape == target_shape
        and target_markers < source_markers
        and omitted_markers <= target_interface_markers
    )


def _storage_interface_marker_profile(
    type_name: str,
) -> tuple[object, frozenset[_StorageInterfaceMarker]] | None:
    type_name = type_name.strip()

    hashmap_args = _storage_generic_args(type_name, "HashMap")
    if hashmap_args is not None:
        return _storage_generic_marker_profile("HashMap", hashmap_args, expected_args=2)

    fixed_array = re.fullmatch(r"(?P<element>.+)\[(?P<length>[0-9]+)\]", type_name)
    if fixed_array is not None:
        element = _storage_interface_marker_profile(fixed_array.group("element"))
        if element is None:
            return None
        shape, markers = element
        return (
            ("array", shape, int(fixed_array.group("length"))),
            frozenset({((-1, *path), name) for path, name in markers}),
        )

    dynarray_args = _storage_generic_args(type_name, "DynArray")
    if dynarray_args is not None:
        return _storage_generic_marker_profile("DynArray", dynarray_args, expected_args=2)

    interface = re.fullmatch(r"interface\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)", type_name)
    if interface is not None:
        name = interface.group("name")
        return ("atom", name), frozenset({((), name)})
    return ("atom", type_name), frozenset()


def _storage_generic_marker_profile(
    generic: str,
    args: list[str],
    *,
    expected_args: int,
) -> tuple[object, frozenset[_StorageInterfaceMarker]] | None:
    if len(args) != expected_args or not all(args):
        return None
    shapes: list[object] = []
    markers: set[_StorageInterfaceMarker] = set()
    for index, arg in enumerate(args):
        profile = _storage_interface_marker_profile(arg)
        if profile is None:
            return None
        shape, arg_markers = profile
        shapes.append(shape)
        markers.update(((index, *path), name) for path, name in arg_markers)
    return ("generic", generic, tuple(shapes)), frozenset(markers)


def _changed_storage_line(
    label: str,
    key: str,
    source: object,
    target: object,
) -> str:
    line = f"changed {label}: {key} slot {_slot_type(source)} -> {_slot_type(target)}"
    if isinstance(source, tuple) and isinstance(target, tuple) and source[2] != target[2]:
        source_width = source[2] if source[2] is not None else "unknown"
        target_width = target[2] if target[2] is not None else "unknown"
        line += f" (n_slots {source_width} -> {target_width})"
    return line


def _slot_type(value: object) -> str:
    if not isinstance(value, tuple):
        return str(value)
    slot, type_name, _n_slots = value
    return f"{slot} {type_name}"
