from __future__ import annotations

import re
from dataclasses import dataclass, field

from .source import code_mask, span_is_code
from .source import split_top_level_args


INT_TYPE_RE = re.compile(r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?$")
TYPE_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
DEF_RE = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\((.*?)\)(?:\s*->.*?)?:\s*(\w+)?\s*$")
INTERFACE_RE = re.compile(r"^interface\s+([A-Za-z_][A-Za-z0-9_]*):\s*$")
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
}


@dataclass
class SourceFacts:
    interfaces: dict[str, dict[str, str]] = field(default_factory=lambda: dict(BUILTIN_INTERFACES))
    structs: set[str] = field(default_factory=set)
    flags_or_enums: set[str] = field(default_factory=set)
    storage_vars: dict[str, str] = field(default_factory=dict)
    global_vars: dict[str, str] = field(default_factory=dict)
    function_vars: dict[int, dict[str, str]] = field(default_factory=dict)
    function_ends: dict[int, int] = field(default_factory=dict)
    imported_interfaces: dict[str, str] = field(default_factory=dict)

    def vars_at_line(self, line_number: int) -> dict[str, str]:
        merged = dict(self.global_vars)
        merged.update(self.storage_vars)
        for start, vars_for_func in sorted(self.function_vars.items()):
            end = self.function_ends.get(start, 10**9)
            if start <= line_number <= end:
                merged.update(vars_for_func)
        return merged


def parse_source_facts(source: str) -> SourceFacts:
    facts = SourceFacts()
    lines = source.splitlines()
    current_interface: str | None = None
    current_function_line: int | None = None
    current_function_indent = 0

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
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
            continue
        if current_interface and indent == 0:
            current_interface = None

        struct_match = STRUCT_RE.match(stripped)
        if struct_match:
            facts.structs.add(struct_match.group(1))
            continue

        flag_match = FLAG_RE.match(stripped)
        if flag_match:
            facts.flags_or_enums.add(flag_match.group(1))

        if current_interface:
            def_match = DEF_RE.match(stripped)
            if def_match:
                mutability = def_match.group(3) or "nonpayable"
                facts.interfaces[current_interface][def_match.group(1)] = mutability
            continue

        def_match = DEF_RE.match(stripped)
        if def_match:
            if current_function_line is not None:
                facts.function_ends[current_function_line] = line_no - 1
            current_function_line = line_no
            current_function_indent = indent
            facts.function_vars[current_function_line] = _parse_params(def_match.group(2))
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
    return type_name.split("[", 1)[0].strip()


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


def infer_expr_type(expr: str, vars_for_line: dict[str, str]) -> str | None:
    expr = expr.strip()
    if re.fullmatch(r"\d(?:_?\d)*", expr):
        return "uint256"
    if expr in vars_for_line:
        return normalize_type(vars_for_line[expr])
    if expr.startswith("self.") and expr[5:] in vars_for_line:
        return normalize_type(vars_for_line[expr[5:]])
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
