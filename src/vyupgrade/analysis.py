from __future__ import annotations

import re
from dataclasses import dataclass, field

from .source import code_mask, span_is_code
from .source import split_top_level_args


INT_TYPE_RE = re.compile(r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$")
TYPE_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
DEF_RE = re.compile(
    r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\((.*?)\)(?:\s*->\s*([^:]+?))?\s*:\s*(\w+)?\s*$"
)
INTERFACE_RE = re.compile(r"^interface\s+([A-Za-z_][A-Za-z0-9_]*)(?:\([^)]*\))?:\s*$")
STRUCT_RE = re.compile(r"^struct\s+([A-Za-z_][A-Za-z0-9_]*):\s*$")
FLAG_RE = re.compile(r"^(?:enum|flag)\s+([A-Za-z_][A-Za-z0-9_]*):\s*$")


BUILTIN_INTERFACES = {
    "ERC20": {
        "totalSupply": "view",
        "balanceOf": "view",
        "allowance": "view",
        "transfer": "nonpayable",
        "transferFrom": "nonpayable",
        "approve": "nonpayable",
    },
    "IERC20": {
        "totalSupply": "view",
        "balanceOf": "view",
        "allowance": "view",
        "transfer": "nonpayable",
        "transferFrom": "nonpayable",
        "approve": "nonpayable",
    },
    "ERC20Detailed": {"name": "view", "symbol": "view", "decimals": "view"},
    "IERC20Detailed": {"name": "view", "symbol": "view", "decimals": "view"},
    "ERC4626": {
        "asset": "view",
        "totalAssets": "view",
        "convertToShares": "view",
        "convertToAssets": "view",
        "maxDeposit": "view",
        "previewDeposit": "view",
        "deposit": "nonpayable",
        "maxMint": "view",
        "previewMint": "view",
        "mint": "nonpayable",
        "maxWithdraw": "view",
        "previewWithdraw": "view",
        "withdraw": "nonpayable",
        "maxRedeem": "view",
        "previewRedeem": "view",
        "redeem": "nonpayable",
    },
    "IERC4626": {
        "asset": "view",
        "totalAssets": "view",
        "convertToShares": "view",
        "convertToAssets": "view",
        "maxDeposit": "view",
        "previewDeposit": "view",
        "deposit": "nonpayable",
        "maxMint": "view",
        "previewMint": "view",
        "mint": "nonpayable",
        "maxWithdraw": "view",
        "previewWithdraw": "view",
        "withdraw": "nonpayable",
        "maxRedeem": "view",
        "previewRedeem": "view",
        "redeem": "nonpayable",
    },
}

BUILTIN_INTERFACE_RETURNS = {
    "ERC20": {
        "totalSupply": "uint256",
        "balanceOf": "uint256",
        "allowance": "uint256",
        "transfer": "bool",
        "transferFrom": "bool",
        "approve": "bool",
    },
    "IERC20": {
        "totalSupply": "uint256",
        "balanceOf": "uint256",
        "allowance": "uint256",
        "transfer": "bool",
        "transferFrom": "bool",
        "approve": "bool",
    },
    "ERC20Detailed": {"name": "String[64]", "symbol": "String[32]", "decimals": "uint8"},
    "IERC20Detailed": {"name": "String[64]", "symbol": "String[32]", "decimals": "uint8"},
    "ERC4626": {
        "asset": "address",
        "totalAssets": "uint256",
        "convertToShares": "uint256",
        "convertToAssets": "uint256",
        "maxDeposit": "uint256",
        "previewDeposit": "uint256",
        "deposit": "uint256",
        "maxMint": "uint256",
        "previewMint": "uint256",
        "mint": "uint256",
        "maxWithdraw": "uint256",
        "previewWithdraw": "uint256",
        "withdraw": "uint256",
        "maxRedeem": "uint256",
        "previewRedeem": "uint256",
        "redeem": "uint256",
    },
    "IERC4626": {
        "asset": "address",
        "totalAssets": "uint256",
        "convertToShares": "uint256",
        "convertToAssets": "uint256",
        "maxDeposit": "uint256",
        "previewDeposit": "uint256",
        "deposit": "uint256",
        "maxMint": "uint256",
        "previewMint": "uint256",
        "mint": "uint256",
        "maxWithdraw": "uint256",
        "previewWithdraw": "uint256",
        "withdraw": "uint256",
        "maxRedeem": "uint256",
        "previewRedeem": "uint256",
        "redeem": "uint256",
    },
}


@dataclass
class SourceFacts:
    interfaces: dict[str, dict[str, str]] = field(default_factory=lambda: dict(BUILTIN_INTERFACES))
    interface_returns: dict[str, dict[str, str]] = field(default_factory=lambda: dict(BUILTIN_INTERFACE_RETURNS))
    structs: set[str] = field(default_factory=set)
    struct_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    flags_or_enums: set[str] = field(default_factory=set)
    storage_vars: dict[str, str] = field(default_factory=dict)
    global_vars: dict[str, str] = field(default_factory=dict)
    function_vars: dict[int, dict[str, str]] = field(default_factory=dict)
    function_ends: dict[int, int] = field(default_factory=dict)
    function_returns: dict[int, str] = field(default_factory=dict)
    function_return_names: dict[str, str] = field(default_factory=dict)
    imported_interfaces: dict[str, str] = field(default_factory=dict)

    def vars_at_line(self, line_number: int) -> dict[str, str]:
        merged = dict(self.global_vars)
        merged.update(self.storage_vars)
        for start, vars_for_func in sorted(self.function_vars.items()):
            end = self.function_ends.get(start, 10**9)
            if start <= line_number <= end:
                merged.update(vars_for_func)
        return merged

    def return_type_at_line(self, line_number: int) -> str | None:
        for start, return_type in sorted(self.function_returns.items()):
            end = self.function_ends.get(start, 10**9)
            if start <= line_number <= end:
                return return_type
        return None


def parse_source_facts(source: str) -> SourceFacts:
    facts = SourceFacts()
    lines = source.splitlines()
    current_interface: str | None = None
    pending_interface_method: str | None = None
    current_struct: str | None = None
    current_struct_indent = 0
    pending_function_line: int | None = None
    pending_function_indent = 0
    pending_function_header: list[str] = []
    current_function_line: int | None = None
    current_function_indent = 0

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            continue

        if pending_function_line is not None:
            pending_function_header.append(stripped)
            if _balanced_parens(" ".join(pending_function_header)):
                def_match = DEF_RE.match(" ".join(pending_function_header))
                if def_match:
                    if current_function_line is not None:
                        facts.function_ends[current_function_line] = pending_function_line - 1
                    current_function_line = pending_function_line
                    current_function_indent = pending_function_indent
                    facts.function_vars[current_function_line] = _parse_params(def_match.group(2))
                    if def_match.group(3):
                        facts.function_returns[current_function_line] = def_match.group(3).strip()
                        facts.function_return_names[def_match.group(1)] = def_match.group(3).strip()
                pending_function_line = None
                pending_function_header = []
            continue

        import_match = re.match(r"from\s+vyper\.interfaces\s+import\s+(.+)$", stripped)
        if import_match:
            for name in [part.strip() for part in import_match.group(1).split(",")]:
                if name in {"ERC20", "ERC20Detailed"}:
                    facts.imported_interfaces[name] = "I" + name
            continue

        interface_match = INTERFACE_RE.match(stripped)
        if interface_match:
            current_interface = interface_match.group(1)
            facts.interfaces.setdefault(current_interface, {})
            facts.interface_returns.setdefault(current_interface, {})
            continue
        if current_interface and indent == 0:
            current_interface = None
            pending_interface_method = None

        struct_match = STRUCT_RE.match(stripped)
        if struct_match:
            current_struct = struct_match.group(1)
            current_struct_indent = indent
            facts.structs.add(current_struct)
            facts.struct_fields.setdefault(current_struct, {})
            continue
        if current_struct and indent <= current_struct_indent:
            current_struct = None

        if current_struct:
            decl = _parse_var_decl(stripped)
            if decl is not None:
                name, type_name = decl
                facts.struct_fields[current_struct][name] = type_name
            continue

        flag_match = FLAG_RE.match(stripped)
        if flag_match:
            facts.flags_or_enums.add(flag_match.group(1))

        if current_interface:
            def_match = DEF_RE.match(stripped)
            if def_match:
                mutability = def_match.group(4) or "nonpayable"
                facts.interfaces[current_interface][def_match.group(1)] = mutability
                if def_match.group(3):
                    facts.interface_returns[current_interface][def_match.group(1)] = def_match.group(3).strip()
                pending_interface_method = None
                continue
            multiline_def = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", stripped)
            if multiline_def:
                pending_interface_method = multiline_def.group(1)
                continue
            if pending_interface_method is not None:
                end_match = re.match(r"\)\s*(?:->\s*([^:]+?))?\s*:\s*(\w+)\s*$", stripped)
                if end_match:
                    facts.interfaces[current_interface][pending_interface_method] = end_match.group(2)
                    if end_match.group(1):
                        facts.interface_returns[current_interface][pending_interface_method] = end_match.group(1).strip()
                    pending_interface_method = None
            continue

        def_match = DEF_RE.match(stripped)
        if def_match:
            if current_function_line is not None:
                facts.function_ends[current_function_line] = line_no - 1
            current_function_line = line_no
            current_function_indent = indent
            facts.function_vars[current_function_line] = _parse_params(def_match.group(2))
            if def_match.group(3):
                facts.function_returns[current_function_line] = def_match.group(3).strip()
                facts.function_return_names[def_match.group(1)] = def_match.group(3).strip()
            continue
        if re.match(r"def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped):
            pending_function_line = line_no
            pending_function_indent = indent
            pending_function_header = [stripped]
            continue

        if current_function_line is not None and indent <= current_function_indent and stripped:
            facts.function_ends[current_function_line] = line_no - 1
            current_function_line = None

        decl = _parse_var_decl(stripped)
        if decl is None:
            continue
        name, type_name = decl
        if current_function_line is None:
            facts.global_vars[name] = type_name
            storage_type = _unwrap_public_or_constant(type_name)
            if storage_type:
                facts.storage_vars[name] = storage_type
        else:
            facts.function_vars[current_function_line][name] = type_name

    if current_function_line is not None:
        facts.function_ends[current_function_line] = len(lines)
    return facts


def is_integer_type(type_name: str | None) -> bool:
    if type_name is None:
        return False
    wrapper = re.match(r"(?:public|constant|immutable)\((.+)\)$", type_name.strip())
    if wrapper:
        type_name = wrapper.group(1)
    return bool(INT_TYPE_RE.match(type_name))


def normalize_type(type_name: str) -> str:
    type_name = type_name.split("[", 1)[0].strip()
    wrapper = re.match(r"(?:public|constant|immutable)\((.+)\)$", type_name)
    return wrapper.group(1).strip() if wrapper else type_name


def unwrap_type(type_name: str) -> str:
    type_name = type_name.strip()
    wrapper = re.match(r"(?:public|constant|immutable)\((.+)\)$", type_name)
    return wrapper.group(1).strip() if wrapper else type_name


def iterable_element_type(type_name: str | None) -> str | None:
    if type_name is None:
        return None
    type_name = type_name.strip()
    dyn_match = re.match(r"DynArray\[\s*([^,\]]+)", type_name)
    if dyn_match:
        return dyn_match.group(1).strip()
    static_match = re.match(r"(.+)\[[^\]]+\]$", type_name)
    if static_match and not type_name.startswith(("HashMap[", "Bytes[", "String[")):
        return static_match.group(1).strip()
    return None


def indexed_value_type(type_name: str | None) -> str | None:
    if type_name is None:
        return None
    type_name = unwrap_type(type_name)
    if type_name.startswith("HashMap[") and type_name.endswith("]"):
        parts = split_top_level_args(type_name.removeprefix("HashMap[").removesuffix("]"))
        if parts and len(parts) == 2:
            return parts[1].strip()
    return iterable_element_type(type_name)


def infer_expr_type(expr: str, vars_for_line: dict[str, str], facts: SourceFacts | None = None) -> str | None:
    expr = expr.strip()
    expr = _strip_outer_parens(expr)
    if re.fullmatch(r"\d(?:_?\d)*", expr):
        return "uint256"
    if expr in {"block.timestamp", "block.number", "block.difficulty", "block.basefee", "block.prevhash", "chain.id", "msg.value"}:
        return "uint256"
    convert_match = re.fullmatch(r"convert\s*\(.+,\s*([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\s*\)", expr)
    if convert_match:
        return convert_match.group(1)
    if facts is not None:
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
        return normalize_type(vars_for_line[expr])
    if expr.startswith("self.") and expr[5:] in vars_for_line:
        return normalize_type(vars_for_line[expr[5:]])
    indexed_type = _infer_indexed_type(expr, vars_for_line)
    if indexed_type is not None:
        return indexed_type
    return None


def _balanced_parens(value: str) -> bool:
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
    return depth == 0 and value.rstrip().endswith(":")


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


def _infer_external_call_type(expr: str, vars_for_line: dict[str, str], facts: SourceFacts) -> str | None:
    expr = re.sub(r"^(?:staticcall|extcall)\s+", "", expr.strip())
    match = re.fullmatch(
        r"(?:(?P<cast>[A-Za-z_][A-Za-z0-9_]*)\s*\(.+\)|(?P<target>(?:self\.)?[A-Za-z_][A-Za-z0-9_]*))\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\(.*\)",
        expr,
        re.DOTALL,
    )
    if not match:
        return None
    if match.group("cast"):
        target_type = match.group("cast")
    else:
        target_type = infer_expr_type(match.group("target"), vars_for_line, facts)
    return facts.interface_returns.get(normalize_type(target_type or ""), {}).get(match.group("method"))


def _infer_internal_call_type(expr: str, facts: SourceFacts) -> str | None:
    match = re.fullmatch(r"(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(.*\)", expr, re.DOTALL)
    if not match:
        return None
    return facts.function_return_names.get(match.group(1))


def _infer_attribute_type(expr: str, vars_for_line: dict[str, str], facts: SourceFacts) -> str | None:
    if "." not in expr:
        return None
    base, field_name = expr.rsplit(".", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
        return None
    base_type = infer_expr_type(base, vars_for_line, facts)
    if base_type is None:
        return None
    return facts.struct_fields.get(normalize_type(base_type), {}).get(field_name)


def _infer_indexed_type(expr: str, vars_for_line: dict[str, str]) -> str | None:
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
        type_name = indexed_value_type(type_name)
        if type_name is None:
            return None
        rest = rest[close + 1 :]
    return normalize_type(type_name) if rest == "" else None


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


def _parse_var_decl(line: str) -> tuple[str, str] | None:
    if line.startswith(("event ", "struct ", "interface ", "flag ", "enum ", "implements:")):
        return None
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=#]+)", line)
    if not match:
        return None
    return match.group(1), _strip_default(match.group(2).strip())


def _strip_default(type_part: str) -> str:
    return type_part.split("=", 1)[0].strip()


def _unwrap_public_or_constant(type_name: str) -> str | None:
    match = re.match(r"(?:public|constant|immutable)\((.+)\)$", type_name)
    if match:
        return match.group(1).strip()
    if TYPE_NAME_RE.fullmatch(type_name):
        return type_name
    return None
