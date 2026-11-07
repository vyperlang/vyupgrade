from __future__ import annotations

import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property

from .constant_evaluation import integer_constant_values
from .source import (
    code_mask,
    find_matching,
    logical_statement_tokens,
    lone_cr_as_lf,
    span_is_code,
    split_top_level_args,
)
from .vyper_builtins import (
    BUILTIN_INTERFACE_PARAMS,
    BUILTIN_INTERFACE_RETURNS,
    BUILTIN_INTERFACES,
)


INT_TYPE_RE = re.compile(
    r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$"
)
DEF_RE = re.compile(
    r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)(?:\s*->\s*([^:]+?))?\s*:\s*(\w+)?\s*$"
)
INTERFACE_RE = re.compile(r"^interface\s+([A-Za-z_][A-Za-z0-9_]*)(?:\([^)]*\))?:\s*$")
STRUCT_RE = re.compile(r"^struct\s+([A-Za-z_][A-Za-z0-9_]*):\s*$")
FLAG_RE = re.compile(r"^(?:enum|flag)\s+([A-Za-z_][A-Za-z0-9_]*):\s*$")
TYPE_WRAPPER_RE = re.compile(r"(?:public|constant|immutable)\((.+)\)$")


@dataclass(frozen=True, slots=True)
class _Binding:
    """A persistent chain lets whole blocks and continuation lines share their locals."""

    name: str
    type_name: str | None
    outer: _Binding | None
    loop_line: int | None = None


@dataclass
class SourceFacts:
    interfaces: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            name: methods.copy() for name, methods in BUILTIN_INTERFACES.items()
        }
    )
    interface_returns: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            name: methods.copy() for name, methods in BUILTIN_INTERFACE_RETURNS.items()
        }
    )
    interface_params: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=lambda: {
            interface_name: {method_name: params.copy() for method_name, params in methods.items()}
            for interface_name, methods in BUILTIN_INTERFACE_PARAMS.items()
        }
    )
    structs: set[str] = field(default_factory=set)
    struct_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    flags_or_enums: set[str] = field(default_factory=set)
    storage_vars: dict[str, str] = field(default_factory=dict)
    global_vars: dict[str, str] = field(default_factory=dict)
    local_declarations: dict[int, tuple[str, str]] = field(default_factory=dict)
    assignment_rhs_columns: dict[int, int] = field(default_factory=dict)
    function_vars: dict[int, dict[str, str]] = field(default_factory=dict)
    _bindings_by_line: dict[int, _Binding] = field(default_factory=dict, repr=False)
    function_ends: dict[int, int] = field(default_factory=dict)
    function_names: dict[int, str] = field(default_factory=dict)
    function_decorators: dict[int, tuple[str, ...]] = field(default_factory=dict)
    function_decorator_lines: dict[int, dict[str, int]] = field(default_factory=dict)
    function_returns: dict[int, str] = field(default_factory=dict)
    function_return_names: dict[str, str] = field(default_factory=dict)
    function_params: dict[str, dict[str, str]] = field(default_factory=dict)
    imported_interfaces: dict[str, str] = field(default_factory=dict)

    _source: str = field(default="", repr=False, compare=False)

    @cached_property
    def tuple_index_constants(self) -> dict[str, int]:
        # In valid source, locals cannot shadow a module constant. Rule-only
        # clients can supply invalid source, so reject any ambiguous binding.
        values = {
            name: value for name, value in integer_constant_values(self._source).items()
            if is_integer_type(self.storage_vars.get(name))
        }
        seen = set()
        for head in self._bindings_by_line.values():
            binding = head
            while binding is not None and id(binding) not in seen:
                seen.add(id(binding))
                values.pop(binding.name, None)
                binding = binding.outer
        return values

    def _visible_bindings(self, line_number: int) -> Iterator[_Binding]:
        head = self._bindings_by_line.get(line_number)
        seen = set()
        while head is not None:
            if head.name not in seen:
                seen.add(head.name)
                yield head
            head = head.outer

    def vars_at_line(
        self, line_number: int, inferred_loop_types: dict[int, str] | None = None
    ) -> dict[str, str]:
        variables = {**self.global_vars, **self.storage_vars}
        for binding in self._visible_bindings(line_number):
            typ = binding.type_name
            if typ is None and binding.loop_line is not None and inferred_loop_types:
                typ = inferred_loop_types.get(binding.loop_line)
            if typ is None:
                # An unannotated legacy loop binds its name even before VY070 infers its type.
                variables.pop(binding.name, None)
            else:
                variables[binding.name] = typ
        return variables

    def return_type_at_line(self, line_number: int) -> str | None:
        for start, return_type in sorted(self.function_returns.items()):
            end = self.function_ends.get(start, 10**9)
            if start <= line_number <= end:
                return return_type
        return None

    def loop_vars_at_line(self, line_number: int) -> set[str]:
        return {
            binding.name
            for binding in self._visible_bindings(line_number)
            if binding.loop_line is not None
        }


def _statement_text(tokens: list[tokenize.TokenInfo]) -> str:
    """Join logical lines without comments or continuations, preserving same-line spelling."""
    first, last = tokens[0], tokens[-1]
    if first.start[0] == last.end[0]:
        return first.line[first.start[1] : last.end[1]]
    pieces = [first.string]
    previous = first
    for token in tokens[1:]:
        if previous.end[0] == token.start[0]:
            pieces.append(token.line[previous.end[1] : token.start[1]])
        else:
            pieces.append(" ")
        pieces.append(token.string)
        previous = token
    return "".join(pieces)


def parse_source_facts(source: str) -> SourceFacts:
    source = lone_cr_as_lf(source)
    facts = SourceFacts(_source=source)
    decorators: list[tuple[str, int]] = []
    function: int | None = None
    interface: str | None = None
    struct: str | None = None
    pending_method: str | None = None
    # The current stack head is the scope at this indentation. A block header
    # supplies pending bindings to its suite; dedenting restores its parent.
    scopes: list[tuple[int, _Binding | None]] = [(0, None)]
    pending: _Binding | None = None
    for tokens in logical_statement_tokens(source):
        first, last = tokens[0], tokens[-1]
        row, column = first.start
        indent = len(first.line[:column].expandtabs())
        while scopes[-1][0] > indent:
            scopes.pop()
        if indent > scopes[-1][0]:
            scopes.append((indent, pending))
        visible = scopes[-1][1]
        pending = visible

        if indent == 0:
            interface = struct = pending_method = None
            if first.string == "@":
                if len(tokens) > 1 and tokens[1].type == tokenize.NAME:
                    decorators.append((tokens[1].string, row))
                continue
            if function is not None:
                facts.function_ends[function] = row - 1
                function = None
            header = _statement_text(tokens)
            if first.string == "def" and (
                match := DEF_RE.match(_function_signature_header(header))
            ):
                function = row
                _record_function(facts, row, match, decorators)
                for name, typ in facts.function_params[match[1]].items():
                    visible = _Binding(name, typ, visible)
                pending = visible
            elif match := INTERFACE_RE.match(header):
                interface = match[1]
                facts.interfaces.setdefault(interface, {})
                facts.interface_returns.setdefault(interface, {})
                facts.interface_params.setdefault(interface, {})
            elif match := STRUCT_RE.match(header):
                struct = match[1]
                facts.structs.add(struct)
                facts.struct_fields.setdefault(struct, {})
            elif match := FLAG_RE.match(header):
                facts.flags_or_enums.add(match[1])
            elif match := re.match(
                r"from\s+(?:vyper\.interfaces|ethereum\.ercs)\s+import\s+(.+)$", header
            ):
                for name, alias in _parse_imported_names(match[1]):
                    if name in {"ERC20", "ERC20Detailed"}:
                        facts.imported_interfaces[alias] = "I" + name
                    _record_builtin_interface_import(facts, name, alias)
            elif decl := _parse_var_decl(header, _known_type_names(facts)):
                name, typ = decl
                facts.global_vars[name] = typ
                storage_type = _unwrap_public_or_constant(typ, _known_type_names(facts))
                if storage_type:
                    facts.storage_vars[name] = storage_type
            decorators.clear()
        elif interface is not None:
            if first.string == "def" and (match := DEF_RE.match(_statement_text(tokens))):
                method = _record_interface_method(facts, interface, match)
                pending_method = method if match[4] is None else None
            elif pending_method and first.string in {"view", "pure", "nonpayable", "payable"}:
                facts.interfaces[interface][pending_method] = first.string
                pending_method = None
        elif struct is not None:
            if decl := _parse_var_decl(_statement_text(tokens), _known_type_names(facts)):
                name, typ = decl
                facts.struct_fields[struct][name] = typ
        elif function is not None:
            _record_assignment_rhs(tokens, facts)
            if first.string == "for" and len(tokens) >= 4 and tokens[1].type == tokenize.NAME:
                name = tokens[1].string
                in_index = next((i for i, token in enumerate(tokens) if token.string == "in"), None)
                if in_index is not None:
                    typ = (
                        _statement_text(tokens[3:in_index])
                        if tokens[2].string == ":" and in_index > 3
                        else None
                    )
                    if typ is not None:
                        facts.local_declarations[row] = name, typ
                        facts.function_vars[function][name] = typ
                    pending = _Binding(name, typ, visible, loop_line=row)
                    if last.string != ":":
                        # A single-line suite shares its row with the header.
                        visible = pending
            elif len(tokens) > 1 and tokens[1].string == ":":
                if decl := _parse_var_decl(_statement_text(tokens), _known_type_names(facts)):
                    name, typ = decl
                    facts.local_declarations[row] = decl
                    facts.function_vars[function][name] = typ
                    visible = _Binding(name, typ, visible)
                    scopes[-1] = (indent, visible)
                    pending = visible
        if visible is not None:
            for line in range(row, last.end[0] + 1):
                facts._bindings_by_line[line] = visible
    if function is not None:
        facts.function_ends[function] = source.count("\n") + int(not source.endswith("\n"))
    return facts


def _record_assignment_rhs(tokens: list[tokenize.TokenInfo], facts: SourceFacts) -> None:
    """Record the real assignment operator, excluding comparisons and call keywords."""
    depth = 0
    for token in tokens:
        if token.type != tokenize.OP:
            continue
        if token.string in {"(", "[", "{"}:
            depth += 1
        elif token.string in {")", "]", "}"}:
            depth -= 1
        elif depth == 0 and token.string in {
            "=", "+=", "-=", "*=", "/=", "//=", "%=", "**=", "&=", "|=", "^=", ">>=", "<<=",
        }:
            row, column = token.end
            facts.assignment_rhs_columns[row] = column
            return


def _record_function(
    facts: SourceFacts,
    line_no: int,
    def_match: re.Match[str],
    decorators: list[tuple[str, int]],
) -> None:
    name = def_match.group(1)
    facts.function_names[line_no] = name
    facts.function_decorators[line_no] = tuple(name for name, _line in decorators)
    facts.function_decorator_lines[line_no] = {
        name: decorator_line for name, decorator_line in decorators
    }
    params = _parse_params(def_match.group(2))
    facts.function_vars[line_no] = params.copy()
    facts.function_params[name] = params
    if def_match.group(3):
        return_type = def_match.group(3).strip()
        facts.function_returns[line_no] = return_type
        facts.function_return_names[name] = return_type


def _record_interface_method(facts: SourceFacts, interface: str, def_match: re.Match[str]) -> str:
    method_name = def_match.group(1)
    facts.interfaces[interface][method_name] = def_match.group(4) or "nonpayable"
    facts.interface_params[interface][method_name] = _parse_params(def_match.group(2))
    if def_match.group(3):
        facts.interface_returns[interface][method_name] = def_match.group(3).strip()
    return method_name


def _parse_imported_names(imports: str) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    for part in imports.strip().removeprefix("(").removesuffix(")").split(","):
        item = part.strip()
        if not item:
            continue
        alias_match = re.match(
            r"([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", item
        )
        if alias_match:
            names.append((alias_match.group(1), alias_match.group(2)))
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
            names.append((item, item))
    return names


def _record_builtin_interface_import(facts: SourceFacts, name: str, alias: str) -> None:
    if name not in facts.interfaces:
        return
    if alias != name:
        facts.interfaces[alias] = facts.interfaces[name].copy()
        facts.interface_returns[alias] = facts.interface_returns.get(name, {}).copy()
        facts.interface_params[alias] = {
            method: params.copy()
            for method, params in facts.interface_params.get(name, {}).items()
        }


def _known_type_names(facts: SourceFacts) -> set[str]:
    return facts.interfaces.keys() | facts.structs | facts.flags_or_enums


def is_integer_type(type_name: str | None) -> bool:
    if type_name is None:
        return False
    type_name = unwrap_type(type_name)
    return bool(INT_TYPE_RE.match(type_name))


def normalize_type(type_name: str) -> str:
    type_name = type_name.split("[", 1)[0].strip()
    return unwrap_type(type_name)


def unwrap_type(type_name: str) -> str:
    type_name = type_name.strip()
    wrapper = TYPE_WRAPPER_RE.match(type_name)
    return wrapper.group(1).strip() if wrapper else type_name


def iterable_element_type(type_name: str | None) -> str | None:
    if type_name is None:
        return None
    type_name = type_name.strip()
    if type_name.startswith("DynArray["):
        open_index = type_name.find("[")
        close_index = find_matching(type_name, open_index, "[", "]")
        if close_index == len(type_name) - 1:
            parts = split_top_level_args(type_name[open_index + 1 : close_index])
            if parts is not None and len(parts) == 2:
                return parts[0].strip()
    if type_name.endswith("]"):
        depth = 0
        for index in range(len(type_name) - 1, -1, -1):
            char = type_name[index]
            if char == "]":
                depth += 1
            elif char == "[":
                depth -= 1
                if depth == 0:
                    element = type_name[:index].strip()
                    length = type_name[index + 1 : -1].strip()
                    if (
                        element
                        and length
                        and element not in {"Bytes", "DynArray", "HashMap", "String"}
                    ):
                        return element
                    break
    return None


def indexed_value_type(type_name: str | None) -> str | None:
    if type_name is None:
        return None
    type_name = unwrap_type(type_name)
    if type_name.startswith("(") and type_name.endswith(")"):
        parts = split_top_level_args(type_name[1:-1])
        if not parts:
            return None
        if len(parts) == 1 and not type_name[1:-1].rstrip().endswith(","):
            # Parentheses around a type leave its indexing behavior unchanged.
            return indexed_value_type(parts[0].strip())
        # Without a position a heterogeneous tuple has no single element type.
        element = parts[0].strip()
        return element if all(part.strip() == element for part in parts) else None
    if type_name.startswith("HashMap[") and type_name.endswith("]"):
        parts = split_top_level_args(type_name.removeprefix("HashMap[").removesuffix("]"))
        if parts and len(parts) == 2:
            return parts[1].strip()
    return iterable_element_type(type_name)


def indexed_key_type(type_name: str | None) -> str | None:
    if type_name is None:
        return None
    type_name = unwrap_type(type_name)
    if not (type_name.startswith("HashMap[") and type_name.endswith("]")):
        return None
    parts = split_top_level_args(type_name.removeprefix("HashMap[").removesuffix("]"))
    return parts[0].strip() if parts and len(parts) == 2 else None


def infer_expr_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts | None = None,
    *, preserve_shape: bool = False,
) -> str | None:
    """Infer an expression's type, optionally retaining container bounds and subscripts."""
    expr = expr.strip()
    expr = _strip_outer_parens(expr)
    if re.fullmatch(r"\d(?:_?\d)*", expr):
        return "uint256"
    minmax_type = _infer_minmax_type(expr, vars_for_line, facts)
    if minmax_type is not None:
        return minmax_type
    if expr in {
        "block.timestamp",
        "block.number",
        "block.difficulty",
        "block.basefee",
        "block.prevhash",
        "chain.id",
        "msg.value",
        "self.balance",
    }:
        return "uint256"
    if expr == "block.prevrandao":
        return "bytes32"
    convert_match = re.fullmatch(
        r"convert\s*\(.+,\s*([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\s*\)", expr
    )
    if convert_match:
        return convert_match.group(1)
    bounds_match = re.fullmatch(r"(?:max_value|min_value)\s*\(\s*(u?int(?:\d+)?)\s*\)", expr)
    if bounds_match:
        return bounds_match.group(1)
    if re.fullmatch(r"isqrt\s*\(.+\)", expr, re.DOTALL):
        return "uint256"
    if re.fullmatch(r"len\s*\(.+\)", expr, re.DOTALL):
        return "uint256"
    if re.fullmatch(r"as_wei_value\s*\(.+\)", expr, re.DOTALL):
        return "uint256"
    empty_match = re.fullmatch(r"empty\s*\(\s*(.+?)\s*\)", expr)
    if empty_match:
        return empty_match.group(1).strip()
    if facts is not None:
        storage_type = _infer_self_storage_type(expr, facts)
        if storage_type is not None:
            return unwrap_type(storage_type) if preserve_shape else normalize_type(storage_type)
        internal_call_type = _infer_internal_call_type(expr, facts)
        if internal_call_type is not None:
            return internal_call_type
        call_type = _infer_external_call_type(expr, vars_for_line, facts)
        if call_type is not None:
            return call_type
        attr_type = _infer_attribute_type(expr, vars_for_line, facts)
        if attr_type is not None:
            return attr_type
    if expr in vars_for_line:
        typ = vars_for_line[expr]
        return unwrap_type(typ) if preserve_shape else normalize_type(typ)
    indexed_type = _infer_indexed_type(expr, vars_for_line, facts)
    if indexed_type is not None:
        return unwrap_type(indexed_type) if preserve_shape else normalize_type(indexed_type)
    return None


def _infer_minmax_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts | None
) -> str | None:
    match = re.fullmatch(r"(?:min|max)\s*\((.*)\)", expr, re.DOTALL)
    if match is None:
        return None
    args = split_top_level_args(match.group(1))
    if args is None or len(args) < 2:
        return None
    arg_types: list[tuple[str, str]] = []
    for arg in args:
        arg_type = infer_expr_type(arg, vars_for_line, facts)
        if arg_type is None or not is_integer_type(arg_type):
            return None
        arg_types.append((arg.strip(), normalize_type(arg_type)))
    concrete_types = {
        type_name
        for arg, type_name in arg_types
        if not re.fullmatch(r"\d(?:_?\d)*", arg)
    }
    if len(concrete_types) == 1:
        return next(iter(concrete_types))
    all_types = {type_name for _arg, type_name in arg_types}
    return next(iter(all_types)) if len(all_types) == 1 else None


def _strip_outer_parens(expr: str) -> str:
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for index, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expr) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        expr = expr[1:-1].strip()
    return expr


def _infer_external_call_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
    expr = re.sub(r"^(?:staticcall|extcall)\s+", "", expr.strip())
    match = re.fullmatch(
        r"(?:(?P<cast>[A-Za-z_][A-Za-z0-9_]*)\s*\(.+\)|(?P<target>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])*(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])*)*))\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\(.*\)",
        expr,
        re.DOTALL,
    )
    if not match:
        return None
    if match.group("cast"):
        target_type = match.group("cast")
    else:
        target_type = infer_expr_type(match.group("target"), vars_for_line, facts)
    return facts.interface_returns.get(normalize_type(target_type or ""), {}).get(
        match.group("method")
    )


def _infer_internal_call_type(expr: str, facts: SourceFacts) -> str | None:
    match = re.fullmatch(r"(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(.*\)", expr, re.DOTALL)
    if not match:
        return None
    return facts.function_return_names.get(match.group(1))


def _infer_self_storage_type(expr: str, facts: SourceFacts) -> str | None:
    match = re.fullmatch(r"self\.([A-Za-z_][A-Za-z0-9_]*)", expr)
    if match is None:
        return None
    return facts.storage_vars.get(match.group(1))


def _infer_attribute_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts
) -> str | None:
    if "." not in expr:
        return None
    base, field_name = expr.rsplit(".", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
        return None
    base_type = infer_expr_type(base, vars_for_line, facts)
    if base_type is None:
        return None
    return facts.struct_fields.get(normalize_type(base_type), {}).get(field_name)


def _infer_indexed_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts | None = None
) -> str | None:
    open_index = _first_top_level_index(expr)
    if open_index is not None:
        base = expr[:open_index].strip()
        base_type = _raw_index_base_type(base, vars_for_line, facts) or infer_expr_type(
            base, vars_for_line, facts
        )
        if base_type is None:
            return None
        rest = expr[open_index:]
        while rest.startswith("["):
            close = _matching_local_bracket(rest, 0)
            if close is None:
                return None
            base_type = _indexed_value_type_at(base_type, rest[1:close].strip(), facts)
            if base_type is None:
                return None
            rest = rest[close + 1 :]
        return base_type if rest == "" else None

    if expr.startswith("self.") and facts is not None:
        # A local of the same name supplies no evidence about contract storage.
        return facts.storage_vars.get(expr[5:])
    expr = expr.removeprefix("self.")
    root_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", expr)
    if root_match is None:
        return None
    type_name = vars_for_line.get(root_match.group(1))
    if type_name is None:
        return None
    rest = expr[root_match.end() :]
    while rest.startswith("["):
        close = rest.find("]")
        if close == -1:
            return None
        type_name = _indexed_value_type_at(type_name, rest[1:close].strip(), facts)
        if type_name is None:
            return None
        rest = rest[close + 1 :]
    return type_name if rest == "" else None


def _indexed_value_type_at(
    type_name: str | None, index_expr: str, facts: SourceFacts | None
) -> str | None:
    if type_name is None:
        return None
    unwrapped = unwrap_type(type_name)
    if unwrapped.startswith("(") and unwrapped.endswith(")"):
        parts = split_top_level_args(unwrapped[1:-1])
        if not parts:
            return None
        if len(parts) == 1 and not unwrapped[1:-1].rstrip().endswith(","):
            return _indexed_value_type_at(parts[0].strip(), index_expr, facts)
        if re.fullmatch(r"\d(?:_?\d)*", index_expr):
            index = int(index_expr.replace("_", ""))
        elif facts is not None and index_expr.isidentifier():
            index = facts.tuple_index_constants.get(index_expr)
        else:
            index = None
        return parts[index].strip() if index is not None and 0 <= index < len(parts) else None
    return indexed_value_type(type_name)


def _raw_index_base_type(
    expr: str, vars_for_line: dict[str, str], facts: SourceFacts | None
) -> str | None:
    if expr.startswith("self.") and facts is not None:
        return facts.storage_vars.get(expr[5:])
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        return vars_for_line.get(expr)
    return None


def _first_top_level_index(expr: str) -> int | None:
    depth = 0
    for index, char in enumerate(expr):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "[" and depth == 0:
            return index
    return None


def _matching_local_bracket(expr: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(expr)):
        char = expr[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
    return None


def iter_code_matches(source: str, pattern: re.Pattern[str]):
    mask = code_mask(source)
    for match in pattern.finditer(source):
        if span_is_code(mask, match.start(), match.end()):
            yield match


def _parse_params(params: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in split_top_level_args(params) or []:
        part = raw.strip()
        if not part or ":" not in part:
            continue
        name, type_part = part.split(":", 1)
        parsed[name.strip()] = _strip_default(type_part.strip())
    return parsed


def _parse_var_decl(line: str, known_types: set[str] | None = None) -> tuple[str, str] | None:
    if line.startswith(("event ", "struct ", "interface ", "flag ", "enum ", "implements:")):
        return None
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=#]+)", line)
    if not match:
        return None
    type_name = _strip_default(match.group(2).strip().rstrip(","))
    if not _looks_like_type(type_name, known_types or set()):
        return None
    return match.group(1), type_name


def _strip_default(type_part: str) -> str:
    return type_part.split("=", 1)[0].strip()


def _function_signature_header(line: str) -> str:
    if not line.startswith("def "):
        return line
    colon = _top_level_colon(line)
    if colon is None:
        return line
    suffix = line[colon + 1 :].strip()
    if suffix in {"", "view", "pure", "nonpayable", "payable"}:
        return line
    return line[: colon + 1]


def _top_level_colon(line: str) -> int | None:
    depth = 0
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == "\\":
                continue
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ":" and depth == 0:
            return index
    return None


def _unwrap_public_or_constant(type_name: str, known_types: set[str] | None = None) -> str | None:
    match = TYPE_WRAPPER_RE.match(type_name)
    if match:
        return match.group(1).strip()
    if _looks_like_type(type_name, known_types or set()):
        return type_name
    return None


def _looks_like_type(type_name: str, known_types: set[str]) -> bool:
    if type_name.startswith("(") and type_name.endswith(")"):
        parts = split_top_level_args(type_name[1:-1])
        return bool(parts) and all(_looks_like_type(part.strip(), known_types) for part in parts)
    if TYPE_WRAPPER_RE.fullmatch(type_name):
        return True
    if type_name.startswith(("Bytes[", "String[", "DynArray[", "HashMap[")):
        return True
    if type_name in known_types:
        return True
    return bool(
        re.fullmatch(
            r"(?:u?int(?:\d+)?|bool|address|bytes\d*|decimal|[A-Z][A-Za-z0-9_]*)(?:\[.*\])?",
            type_name,
        )
    )
