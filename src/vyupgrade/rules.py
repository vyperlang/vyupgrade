from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .analysis import infer_expr_type, is_integer_type, iterable_element_type, parse_source_facts
from .models import Config, Diagnostic, Fix
from .source import (
    apply_edits,
    code_mask,
    find_matching,
    line_number,
    replace_identifier,
    split_top_level_args,
    span_is_code,
    TextEdit,
)
from .versions import infer_pragma


IMPORT_RENAMES = {"ERC20": "IERC20", "ERC20Detailed": "IERC20Detailed"}
REVIEW_RULES = {"VY080", "VY090", "VY100", "VY110"}


@dataclass
class RewriteResult:
    source: str
    fixes: list[Fix]
    diagnostics: list[Diagnostic]


def apply_rules(source: str, config: Config) -> RewriteResult:
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []

    current = source
    for rule in [
        _pragma,
        _constructor_deploy,
        _abi_builtins,
        _legacy_constants,
        _interface_imports,
        _enum_to_flag,
        _external_call_keywords,
        _integer_division,
        _redundant_integer_convert,
        _struct_kwargs,
        _range_bound,
        _typed_range_loops,
        _create_from_blueprint,
        _nonreentrant,
        _sqrt,
        _bitwise,
        _decimal_diagnostic,
        _prevrandao_diagnostic,
        _missing_pragma_diagnostic,
    ]:
        current, rule_fixes, rule_diagnostics = rule(current, config)
        fixes.extend(rule_fixes)
        diagnostics.extend(rule_diagnostics)

    fixes = [fix for fix in fixes if _enabled(fix.rule, config)]
    diagnostics = [diag for diag in diagnostics if _enabled(diag.rule, config)]
    return RewriteResult(current, fixes, diagnostics)


def _enabled(rule: str, config: Config) -> bool:
    if config.select and rule not in config.select:
        return False
    return rule not in config.ignore


def _any_enabled(rules: set[str], config: Config) -> bool:
    return any(_enabled(rule, config) for rule in rules)


def _pragma(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY001", config):
        return source, [], []
    fixes: list[Fix] = []
    pattern = re.compile(r"^(\s*)#\s*@version\s+(.+?)\s*$", re.MULTILINE)

    def repl(match: re.Match[str]) -> str:
        before = match.group(0)
        version = config.target_version if config.bump_pragma else match.group(2)
        after = f"{match.group(1)}#pragma version {version}"
        fixes.append(Fix("VY001", line_number(source, match.start()), "modernized version pragma", before, after))
        return after

    return pattern.sub(repl, source), fixes, []


def _constructor_deploy(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY002", config):
        return source, [], []
    lines = source.splitlines(keepends=True)
    fixes: list[Fix] = []
    out = list(lines)
    offset = 0
    for index, line in enumerate(lines):
        if not re.match(r"\s*def\s+__init__\s*\(", line):
            continue
        start = index
        while start > 0 and re.match(r"\s*@[A-Za-z_][A-Za-z0-9_]*(?:\(.*\))?\s*(?:#.*)?$", lines[start - 1]):
            start -= 1
        decorators = [decor.strip() for decor in lines[start:index]]
        insert_at = start + offset
        remove_indices: list[int] = []
        has_deploy = any(decor.startswith("@deploy") for decor in decorators)
        for rel, decor in enumerate(decorators):
            if decor.split("(", 1)[0] in {"@external", "@internal", "@public", "@private"}:
                remove_indices.append(start + rel)
        for original_index in sorted(remove_indices, reverse=True):
            before = out[original_index + offset].rstrip("\n")
            del out[original_index + offset]
            offset -= 1
            fixes.append(Fix("VY002", original_index + 1, "removed invalid constructor decorator", before, ""))
        if not has_deploy:
            indent = re.match(r"(\s*)", line).group(1)
            out.insert(insert_at, f"{indent}@deploy\n")
            offset += 1
            fixes.append(Fix("VY002", index + 1, "added @deploy to constructor", "", f"{indent}@deploy"))
    return "".join(out), fixes, []


def _abi_builtins(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    fixes: list[Fix] = []
    current = source
    for before, after, rule in [
        ("_abi_encode", "abi_encode", "VY010"),
        ("_abi_decode", "abi_decode", "VY011"),
    ]:
        if not _enabled(rule, config):
            continue
        next_source, edits = replace_identifier(current, before, after)
        for edit in edits:
            fixes.append(Fix(rule, line_number(current, edit.start), f"renamed {before} to {after}", before, after))
        current = next_source
    return current, fixes, []


def _legacy_constants(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY012", config):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    replacements = {
        "MAX_UINT256": "max_value(uint256)",
        "MIN_INT128": "min_value(int128)",
        "MAX_INT128": "max_value(int128)",
        "MIN_INT256": "min_value(int256)",
        "MAX_INT256": "max_value(int256)",
        "ZERO_ADDRESS": "empty(address)",
    }
    for before, after in replacements.items():
        current, edits = replace_identifier(current, before, after)
        for edit in edits:
            fixes.append(Fix("VY012", line_number(current, edit.start), f"replaced legacy constant {before}", before, after))
    return current, fixes, []


def _redundant_integer_convert(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY051", config):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = parse_source_facts(source)
    mask = code_mask(source)
    for match in re.finditer(r"\bconvert\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = source.find("(", match.start())
        close = find_matching(source, open_index)
        if close is None:
            continue
        args = split_top_level_args(source[open_index + 1 : close])
        if args is None or len(args) != 2:
            continue
        expr, target = args[0].strip(), args[1].strip()
        if target != "uint256" or not re.search(r"[-+*/%]", expr):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        if not _integerish_expression(expr, vars_for_line):
            continue
        replacement = f"({expr})"
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(Fix("VY051", line_number(source, match.start()), "removed redundant uint256 convert around integer expression", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes, []


def _interface_imports(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY020", "VYD003"}, config):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    lines = source.splitlines(keepends=True)
    changed = False
    requested_rewrites: dict[str, str] = {}

    for i, line in enumerate(lines):
        match = re.match(r"(\s*)from\s+vyper\.interfaces\s+import\s+(.+?)(\s*(?:#.*)?)(\n?)$", line)
        if not match:
            continue
        imports = [part.strip() for part in match.group(2).split(",")]
        mapped = [IMPORT_RENAMES.get(name, name) for name in imports]
        if mapped != imports and _enabled("VY020", config):
            requested_rewrites.update({old: new for old, new in zip(imports, mapped, strict=True) if old != new})
            lines[i] = f"{match.group(1)}from ethereum.ercs import {', '.join(mapped)}{match.group(3)}{match.group(4)}"
            fixes.append(Fix("VY020", i + 1, "updated built-in interface import path", line.rstrip("\n"), lines[i].rstrip("\n")))
            changed = True
        elif "vyper.interfaces" in line:
            if _enabled("VYD003", config):
                diagnostics.append(Diagnostic("VYD003", i + 1, "unknown built-in interface import; review manually"))

    current = "".join(lines) if changed else source
    for old, new in requested_rewrites.items():
        next_source, edits = replace_identifier(current, old, new)
        for edit in edits:
            fixes.append(Fix("VY020", line_number(current, edit.start), f"renamed interface type {old} to {new}", old, new))
        current = next_source
    return current, fixes, diagnostics


def _enum_to_flag(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY030"}, config):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    if re.search(r"\benum\s+\w+:", source) is None:
        return source, fixes, diagnostics

    pattern = re.compile(r"^(\s*)enum\s+([A-Za-z_][A-Za-z0-9_]*):", re.MULTILINE)
    for match in pattern.finditer(source):
        diagnostics.append(Diagnostic("VY030", line_number(source, match.start()), f"enum {match.group(2)} should be reviewed for flag compatibility"))
    if not config.aggressive:
        return source, fixes, diagnostics

    def repl(match: re.Match[str]) -> str:
        before = match.group(0)
        after = f"{match.group(1)}flag {match.group(2)}:"
        fixes.append(Fix("VY030", line_number(source, match.start()), "changed enum to flag", before, after))
        return after

    return pattern.sub(repl, source), fixes, diagnostics


def _external_call_keywords(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY040", "VY041", "VYD003"}, config):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    call_re = re.compile(
        r"(?<![\w.])((?P<cast>[A-Z][A-Za-z0-9_]*)\([^)\n]*\)|(?:self\.)?[A-Za-z_][A-Za-z0-9_]*)\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s*\("
    )

    for match in call_re.finditer(source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        prefix = source[max(0, match.start() - 16) : match.start()]
        if re.search(r"\b(?:extcall|staticcall)\s+$", prefix):
            continue
        target = match.group(1)
        cast_type = match.group("cast")
        method = match.group("method")
        if target == "self" or method in {"append", "pop"}:
            continue
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        if target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(target, vars_for_line)
        else:
            target_type = cast_type or infer_expr_type(target, vars_for_line)
        mutability = facts.interfaces.get(target_type or "", {}).get(method)
        if mutability is None:
            if _enabled("VYD003", config):
                diagnostics.append(Diagnostic("VYD003", line_number(source, match.start()), f"cannot infer mutability for external call {target}.{method}"))
            continue
        keyword = "staticcall" if mutability in {"view", "pure"} else "extcall"
        rule = "VY041" if keyword == "staticcall" else "VY040"
        if not _enabled(rule, config):
            continue
        edits.append(TextEdit(match.start(), match.start(), keyword + " "))
        fixes.append(Fix(rule, line_number(source, match.start()), f"added {keyword} to {mutability} external call", source[match.start() : match.end()].rstrip(), keyword + " " + source[match.start() : match.end()].rstrip()))

    return apply_edits(source, edits), fixes, diagnostics


def _integer_division(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY050", "VYD004"}, config):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"(?<!/)/(?!/)", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        line_start = source.rfind("\n", 0, match.start()) + 1
        line_end = source.find("\n", match.end())
        if line_end == -1:
            line_end = len(source)
        line = source[line_start:line_end]
        if re.match(r"\s*(?:from|import)\b", line):
            continue
        left = _read_left_operand(source, match.start())
        right = _read_right_operand(source, match.end())
        vars_for_line = facts.vars_at_line(line_number(source, match.start()))
        left_type = infer_expr_type(left, vars_for_line)
        right_type = infer_expr_type(right, vars_for_line)
        lhs_type = _lhs_declared_type(line)
        assigned_type = _lhs_assigned_type(line, vars_for_line)
        slash_col = match.start() - line_start
        if (
            (is_integer_type(left_type) and is_integer_type(right_type))
            or is_integer_type(lhs_type)
            or is_integer_type(assigned_type)
            or (
                _integerish_expression(line[:slash_col], vars_for_line)
                and _integerish_expression(line[slash_col + 1 :], vars_for_line)
            )
            or (
                _integerish_expression(line[slash_col + 1 :], vars_for_line)
                and _multiline_integer_division_context(source, line_start)
            )
            or (
                _integerish_expression(line[slash_col + 1 :], vars_for_line)
                and line.lstrip().startswith("assert ")
                and "decimal" not in line
            )
        ):
            if not _enabled("VY050", config):
                continue
            edits.append(TextEdit(match.start(), match.end(), "//"))
            fixes.append(Fix("VY050", line_number(source, match.start()), "changed integer division to //", "/", "//"))
        else:
            if _enabled("VYD004", config):
                diagnostics.append(Diagnostic("VYD004", line_number(source, match.start()), "cannot prove / operands are integer typed"))
    return apply_edits(source, edits), fixes, diagnostics


def _struct_kwargs(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY060", config):
        return source, [], []
    facts = parse_source_facts(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for struct_name in sorted(facts.structs):
        for match in re.finditer(rf"\b{re.escape(struct_name)}\s*\(\s*\{{", source):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            paren = source.find("(", match.start())
            close = find_matching(source, paren)
            if close is None:
                continue
            inner = source[paren + 1 : close].strip()
            if not (inner.startswith("{") and inner.endswith("}")):
                continue
            fields = split_top_level_args(inner[1:-1])
            if fields is None:
                continue
            pairs: list[str] = []
            ok = True
            for field in fields:
                if ":" not in field:
                    ok = False
                    break
                name, value = field.split(":", 1)
                pairs.append(f"{name.strip()}={value.strip()}")
            if not ok:
                continue
            replacement = f"{struct_name}({', '.join(pairs)})"
            edits.append(TextEdit(match.start(), close + 1, replacement))
            fixes.append(Fix("VY060", line_number(source, match.start()), "changed struct literal to keyword arguments", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes, []


def _typed_range_loops(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY070", config):
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    facts = parse_source_facts(source)
    pattern = re.compile(r"^(\s*)for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+?):", re.MULTILINE)

    for match in pattern.finditer(source):
        iterable = match.group(3).strip()
        if ":" in source[match.start() : match.end()].split(" in ", 1)[0]:
            continue
        if iterable.startswith("range("):
            var_type = "uint256"
        else:
            vars_for_line = facts.vars_at_line(line_number(source, match.start()))
            iterable_name = iterable.replace("self.", "")
            var_type = iterable_element_type(vars_for_line.get(iterable_name) or infer_expr_type(iterable_name, vars_for_line))
        if var_type is None:
            continue
        before = match.group(0)
        after = f"{match.group(1)}for {match.group(2)}: {var_type} in {iterable}:"
        edits.append(TextEdit(match.start(), match.end(), after))
        fixes.append(Fix("VY070", line_number(source, match.start()), f"added {var_type} loop variable type", before, after))

    return apply_edits(source, edits), fixes, []


def _range_bound(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY071", config):
        return source, [], []
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\brange\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        open_index = source.find("(", match.start())
        close = find_matching(source, open_index)
        if close is None:
            continue
        raw_args = source[open_index + 1 : close]
        if "bound" in raw_args:
            continue
        args = split_top_level_args(raw_args)
        if args is None or len(args) != 2:
            continue
        if _literal_integer(args[0]) and _literal_integer(args[1]):
            continue
        bound = _infer_range_bound(args[0], args[1])
        if bound is None:
            diagnostics.append(
                Diagnostic(
                    "VYD011",
                    line_number(source, match.start()),
                    "range(start, stop) has runtime bounds; add bound=... manually",
                )
            )
            continue
        edits.append(TextEdit(close, close, f", bound={bound}"))
        fixes.append(
            Fix(
                "VY071",
                line_number(source, match.start()),
                "added range bound keyword",
                f"range({raw_args})",
                f"range({raw_args}, bound={bound})",
            )
        )
    return apply_edits(source, edits), fixes, diagnostics


def _create_from_blueprint(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY080", config):
        return source, [], []
    diagnostics: list[Diagnostic] = []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = code_mask(source)
    for match in re.finditer(r"\bcreate_from_blueprint\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = source[match.end() : close]
        if "code_offset" in args:
            continue
        diagnostics.append(Diagnostic("VY080", line_number(source, match.start()), "create_from_blueprint default code_offset changed from 0 to 3"))
        if config.aggressive:
            edits.append(TextEdit(close, close, ", code_offset=0"))
            fixes.append(Fix("VY080", line_number(source, match.start()), "added code_offset=0 to preserve 0.3.x behavior", "", "code_offset=0"))
    return apply_edits(source, edits), fixes, diagnostics


def _nonreentrant(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _any_enabled({"VY090", "VYD002"}, config):
        return source, [], []
    pattern = re.compile(r"@nonreentrant\(\s*([\"'])(.+?)\1\s*\)")
    locks = [match.group(2) for match in pattern.finditer(source)]
    diagnostics: list[Diagnostic] = []
    fixes: list[Fix] = []
    if not locks:
        return source, fixes, diagnostics
    counts = Counter(locks)
    if len(counts) > 1:
        first = pattern.search(source)
        diagnostics.append(Diagnostic("VYD002", line_number(source, first.start() if first else 0), "multiple named reentrancy locks found; 0.4.x uses a global lock"))
        return source, fixes, diagnostics
    diagnostics.extend(Diagnostic("VY090", line_number(source, match.start()), "single named nonreentrant lock rewritten; review callback assumptions") for match in pattern.finditer(source))

    def repl(match: re.Match[str]) -> str:
        fixes.append(Fix("VY090", line_number(source, match.start()), "removed named nonreentrant lock", match.group(0), "@nonreentrant"))
        return "@nonreentrant"

    return pattern.sub(repl, source), fixes, diagnostics


def _sqrt(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY100", config):
        return source, [], []
    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for match in re.finditer(r"(?<!\.)\bsqrt\s*\(", source):
        if span_is_code(mask, match.start(), match.end()):
            edits.append(TextEdit(match.start(), match.start() + 4, "math.sqrt"))
            fixes.append(Fix("VY100", line_number(source, match.start()), "moved sqrt to math module", "sqrt", "math.sqrt"))
    next_source = apply_edits(source, edits)
    if edits and not re.search(r"^\s*import\s+math\s*$", next_source, re.MULTILINE):
        next_source = _insert_import(next_source, "import math\n")
        fixes.append(Fix("VY100", 1, "added math import", "", "import math"))
    return next_source, fixes, []


def _bitwise(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VY110", config):
        return source, [], []
    fixes: list[Fix] = []
    current = source
    for name, operator, unary in [
        ("bitwise_and", "&", False),
        ("bitwise_or", "|", False),
        ("bitwise_xor", "^", False),
        ("bitwise_not", "~", True),
    ]:
        current, new_fixes = _replace_builtin_call(current, name, operator, unary)
        fixes.extend(new_fixes)
    return current, fixes, []


def _decimal_diagnostic(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VYD001", config):
        return source, [], []
    if re.search(r"\bdecimal\b", source) and not config.enable_decimals:
        return source, [], [Diagnostic("VYD001", 1, "decimal type is used; target compile may require --enable-decimals")]
    return source, [], []


def _prevrandao_diagnostic(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VYD010", config):
        return source, [], []
    diagnostics = [
        Diagnostic("VYD010", line_number(source, match.start()), "block.prevrandao signature changed in 0.4.0; review manually")
        for match in re.finditer(r"\bblock\.prevrandao\b", source)
    ]
    return source, [], diagnostics


def _missing_pragma_diagnostic(source: str, config: Config) -> tuple[str, list[Fix], list[Diagnostic]]:
    if not _enabled("VYD005", config):
        return source, [], []
    if infer_pragma(source) is None and config.source_version is None:
        return source, [], [Diagnostic("VYD005", 1, "source has no version pragma and no --source-version")]
    return source, [], []


def _read_left_operand(source: str, index: int) -> str:
    i = index - 1
    while i >= 0 and source[i].isspace():
        i -= 1
    end = i + 1
    while i >= 0 and re.match(r"[A-Za-z0-9_.$\]]", source[i]):
        i -= 1
    return source[i + 1 : end].replace("self.", "")


def _read_right_operand(source: str, index: int) -> str:
    i = index
    while i < len(source) and source[i].isspace():
        i += 1
    start = i
    while i < len(source) and re.match(r"[A-Za-z0-9_.$\[]", source[i]):
        i += 1
    return source[start:i].replace("self.", "")


def _lhs_declared_type(line: str) -> str | None:
    match = re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*([^=]+)=", line)
    return match.group(1).strip() if match else None


def _lhs_assigned_type(line: str, vars_for_line: dict[str, str]) -> str | None:
    match = re.match(r"\s*(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    if not match:
        return None
    return vars_for_line.get(match.group(1))


def _integerish_expression(expr: str, vars_for_line: dict[str, str]) -> bool:
    expr = expr.split("#", 1)[0]
    expr = expr.replace("self.", "")
    expr = re.sub(r"^\s*(?:return|assert)\s+", "", expr)
    if "=" in expr:
        expr = expr.rsplit("=", 1)[-1]
    if re.search(r"\bdecimal\b|\d+\.\d+", expr):
        return False
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)
    if not tokens:
        return bool(re.search(r"\d", expr))
    typed = False
    for token in tokens:
        if token in {"convert", "max", "min", "unsafe_add", "unsafe_sub", "uint256", "uint128", "uint64", "uint8"}:
            typed = True
            continue
        token_type = vars_for_line.get(token)
        if token_type is None:
            if token.isupper():
                typed = True
                continue
            return False
        if not is_integer_type(token_type):
            return False
        typed = True
    return typed


def _multiline_integer_division_context(source: str, line_start: int) -> bool:
    prefix = source[:line_start].splitlines()[-8:]
    block = "\n".join(prefix)
    if re.search(r"\bdecimal\b|\d+\.\d+", block):
        return False
    return bool(re.search(r"return\s*\($|:\s*u?int(?:\d+)?\s*=\s*\($", block, re.MULTILINE))


def _infer_range_bound(start: str, stop: str) -> str | None:
    start = start.strip()
    stop = stop.strip()
    escaped = re.escape(start)
    plus_match = re.fullmatch(rf"{escaped}\s*\+\s*((?:\d|_)+)", stop)
    if plus_match:
        return plus_match.group(1)
    minus_match = re.fullmatch(rf"{escaped}\s*-\s*((?:\d|_)+)", stop)
    if minus_match:
        return minus_match.group(1)
    return None


def _literal_integer(value: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:\d|_)+\s*", value))


def _insert_import(source: str, line: str) -> str:
    lines = source.splitlines(keepends=True)
    insert_at = 0
    while insert_at < len(lines) and (
        lines[insert_at].startswith("#pragma")
        or lines[insert_at].startswith("# @version")
        or lines[insert_at].strip() == ""
        or lines[insert_at].startswith('"""')
    ):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].startswith("import "):
        insert_at += 1
    lines.insert(insert_at, line)
    return "".join(lines)


def _replace_builtin_call(source: str, name: str, operator: str, unary: bool) -> tuple[str, list[Fix]]:
    mask = code_mask(source)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for match in re.finditer(rf"\b{re.escape(name)}\s*\(", source):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        close = find_matching(source, source.find("(", match.start()))
        if close is None:
            continue
        args = split_top_level_args(source[match.end() : close])
        if args is None:
            continue
        if unary and len(args) == 1:
            replacement = f"(~{args[0]})"
        elif not unary and len(args) == 2:
            replacement = f"({args[0]} {operator} {args[1]})"
        else:
            continue
        edits.append(TextEdit(match.start(), close + 1, replacement))
        fixes.append(Fix("VY110", line_number(source, match.start()), f"replaced {name} builtin", source[match.start() : close + 1], replacement))
    return apply_edits(source, edits), fixes
