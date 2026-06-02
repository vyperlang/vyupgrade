from __future__ import annotations

import re
from collections.abc import Callable

from ..analysis import SourceFacts, infer_expr_type, normalize_type
from ..models import Diagnostic, Fix
from ..rule_helpers import (
    line_match_starts_outside_string as _line_match_starts_outside_string,
    line_offsets as _line_offsets,
    nested_under_config_path as _nested_under_config_path,
)
from ..rule_registry import Rule, RuleContext, crossing, target_floor
from ..source import (
    TextEdit,
    apply_edits,
    code_identifiers,
    code_mask,
    find_matching,
    line_number,
    replace_identifier,
    split_top_level_arg_spans,
    span_is_code,
)
from .external_call_helpers import external_call_matches
from .legacy_interfaces import IMPORT_RENAMES


LEGACY_IMPLEMENTED_INTERFACE_STUBS = {
    "ERC165": """interface ERC165:
    def supportsInterface(interface_id: bytes4) -> bool: {mutability}
""",
    "ERC721": """interface ERC721:
    def balanceOf(_owner: address) -> uint256: {balanceOf}
    def ownerOf(_tokenId: uint256) -> address: {ownerOf}
    def getApproved(_tokenId: uint256) -> address: {getApproved}
    def isApprovedForAll(_owner: address, _operator: address) -> bool: {isApprovedForAll}
    def transferFrom(_from: address, _to: address, _tokenId: uint256): {transferFrom}
    def safeTransferFrom(_from: address, _to: address, _tokenId: uint256, _data: Bytes[1024] = b""): {safeTransferFrom}
    def approve(_approved: address, _tokenId: uint256): {approve}
    def setApprovalForAll(_operator: address, _approved: bool): {setApprovalForAll}
""",
    "ERC4626": """interface ERC4626:
    def totalAssets() -> uint256: {totalAssets}
    def convertToAssets(shareAmount: uint256) -> uint256: {convertToAssets}
    def convertToShares(assetAmount: uint256) -> uint256: {convertToShares}
    def maxDeposit(owner: address) -> uint256: {maxDeposit}
    def previewDeposit(assets: uint256) -> uint256: {previewDeposit}
    def deposit(assets: uint256, receiver: address = msg.sender) -> uint256: {deposit}
    def maxMint(owner: address) -> uint256: {maxMint}
    def previewMint(shares: uint256) -> uint256: {previewMint}
    def mint(shares: uint256, receiver: address = msg.sender) -> uint256: {mint}
    def maxWithdraw(owner: address) -> uint256: {maxWithdraw}
    def previewWithdraw(assets: uint256) -> uint256: {previewWithdraw}
    def withdraw(assets: uint256, receiver: address = msg.sender, owner: address = msg.sender) -> uint256: {withdraw}
    def maxRedeem(owner: address) -> uint256: {maxRedeem}
    def previewRedeem(shares: uint256) -> uint256: {previewRedeem}
    def redeem(shares: uint256, receiver: address = msg.sender, owner: address = msg.sender) -> uint256: {redeem}
""",
}


def _legacy_constants(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    current = source
    replacements = {
        "MAX_UINT256": "max_value(uint256)",
        "MIN_INT128": "min_value(int128)",
        "MAX_INT128": "max_value(int128)",
        "MIN_INT256": "min_value(int256)",
        "MAX_INT256": "max_value(int256)",
        "ZERO_ADDRESS": "empty(address)",
        "EMPTY_BYTES32": "empty(bytes32)",
    }
    for before, after in replacements.items():
        current, edits = replace_identifier(current, before, after)
        for edit in edits:
            fixes.append(
                Fix(
                    "VY012",
                    line_number(current, edit.start),
                    f"replaced legacy constant {before}",
                    before,
                    after,
                )
            )
    return current, fixes, []


def _immutable_accessor_collisions(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    current, fixes = _accessor_collision_rewrites(
        source,
        r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*immutable\s*\(",
        "VY013",
        "immutable",
        _is_immutable_declaration_name,
    )
    return current, fixes, []


def _constant_accessor_collisions(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    current, fixes = _accessor_collision_rewrites(
        source,
        r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*constant\s*\(",
        "VY016",
        "constant",
        _is_constant_declaration_name,
    )
    return current, fixes, []


def _interface_storage_assignment_casts(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    mask = rule_context.code_mask
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)self\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
        r"(?P<cast>[A-Za-z_][A-Za-z0-9_]*)\s*\("
    )
    for match in pattern.finditer(source):
        if not span_is_code(mask, match.start("name"), match.end("cast")):
            continue
        name = match.group("name")
        storage_type = normalize_type(facts.storage_vars.get(name, ""))
        cast_type = normalize_type(match.group("cast"))
        if (
            storage_type == cast_type
            or storage_type not in facts.interfaces
            or cast_type not in facts.interfaces
        ):
            continue
        edits.append(TextEdit(match.start("cast"), match.end("cast"), storage_type))
        fixes.append(
            Fix(
                "VY019",
                line_number(source, match.start()),
                "matched interface storage assignment cast to declared type",
                match.group("cast"),
                storage_type,
            )
        )
    return apply_edits(source, edits), fixes, []


def _accessor_collision_rewrites(
    source: str,
    declaration_pattern: str,
    rule: str,
    kind: str,
    is_allowed_declaration: Callable[[str, int], bool],
) -> tuple[str, list[Fix]]:
    declaration_names = {
        match.group(1)
        for match in re.finditer(
            declaration_pattern,
            source,
            re.MULTILINE,
        )
    }
    if not declaration_names:
        return source, []
    function_names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            source,
            re.MULTILINE,
        )
    }
    collisions = sorted(declaration_names & function_names)
    if not collisions:
        return source, []

    mask = code_mask(source)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    taken = code_identifiers(source)
    for name in collisions:
        replacement = _private_backing_name(name, taken)
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        name_edits: list[TextEdit] = []
        for match in pattern.finditer(source):
            if not span_is_code(mask, match.start(), match.end()):
                continue
            if _is_function_definition_name(source, match.start()):
                continue
            if _is_attribute_name(source, match.start()):
                continue
            if _is_type_declaration_name(
                source, match.start(), match.end()
            ) and not is_allowed_declaration(source, match.start()):
                continue
            if _is_keyword_argument_name(source, match.start(), match.end()):
                continue
            name_edits.append(TextEdit(match.start(), match.end(), replacement))
        edits.extend(name_edits)
        fixes.extend(
            Fix(
                rule,
                line_number(source, edit.start),
                f"renamed {kind} backing variable that collides with accessor",
                name,
                replacement,
            )
            for edit in name_edits
        )
    return apply_edits(source, edits), fixes


def _private_backing_name(name: str, taken: set[str]) -> str:
    candidate = f"_{name}"
    while candidate in taken:
        candidate = f"_{candidate}"
    taken.add(candidate)
    return candidate


def _is_function_definition_name(source: str, start: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    return bool(re.fullmatch(r"[ \t]*def\s+", source[line_start:start]))


def _is_attribute_name(source: str, start: int) -> bool:
    i = start - 1
    while i >= 0 and source[i].isspace() and source[i] != "\n":
        i -= 1
    return i >= 0 and source[i] == "."


def _is_keyword_argument_name(source: str, start: int, end: int) -> bool:
    i = end
    while i < len(source) and source[i].isspace() and source[i] != "\n":
        i += 1
    if i >= len(source) or source[i] != "=":
        return False
    j = start - 1
    while j >= 0 and source[j].isspace():
        j -= 1
    return j >= 0 and source[j] in "(,{"


def _is_type_declaration_name(source: str, start: int, end: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    prefix = source[line_start:start]
    if prefix.strip():
        return False
    i = end
    while i < len(source) and source[i].isspace() and source[i] != "\n":
        i += 1
    return i < len(source) and source[i] == ":"


def _is_immutable_declaration_name(source: str, start: int) -> bool:
    line_end = source.find("\n", start)
    if line_end == -1:
        line_end = len(source)
    return bool(re.search(r":\s*immutable\s*\(", source[start:line_end]))


def _is_constant_declaration_name(source: str, start: int) -> bool:
    line_end = source.find("\n", start)
    if line_end == -1:
        line_end = len(source)
    return bool(re.search(r":\s*constant\s*\(", source[start:line_end]))


def _interface_view_mutability(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    view_names = _view_implementation_names(source)
    if not view_names:
        return source, [], []
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    mask = rule_context.code_mask
    pattern = re.compile(
        r"^([ \t]*def[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\([^#\n]*\)[ \t]*(?:->[ \t]*[^:#\n]+)?[ \t]*:[ \t]*)(nonpayable)\b",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        if match.group(2) not in view_names or not span_is_code(mask, match.start(), match.end()):
            continue
        edits.append(TextEdit(match.start(3), match.end(3), "view"))
        fixes.append(
            Fix(
                "VY014",
                line_number(source, match.start()),
                "changed local interface mutability to match view implementation",
                match.group(0),
                f"{match.group(1)}view",
            )
        )
    return apply_edits(source, edits), fixes, []


def _side_effecting_view_interface_calls(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    methods = _side_effecting_view_interface_methods(source, facts)
    if not methods:
        return source, [], []

    lines = source.splitlines(keepends=True)
    offsets = _line_offsets(source)
    current_interface: str | None = None
    current_indent = 0
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    mask = rule_context.code_mask
    for index, line in enumerate(lines):
        offset = offsets[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not _line_match_starts_outside_string(source, mask, offset):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        header = line.split("#", 1)[0].strip()
        interface_match = re.match(
            r"interface\s+([A-Za-z_][A-Za-z0-9_]*)(?:\([^)]*\))?:\s*$", header
        )
        if interface_match is not None:
            current_interface = interface_match.group(1)
            current_indent = indent
            continue
        if current_interface is not None and indent <= current_indent:
            current_interface = None
        if current_interface is None:
            continue
        method_match = re.match(
            r"(?P<prefix>[ \t]*def[ \t]+(?P<method>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\([^#\n]*\)[ \t]*:[ \t]*)(?P<mutability>view|pure)\b",
            line,
        )
        if method_match is None:
            continue
        if (current_interface, method_match.group("method")) not in methods:
            continue
        start = offset + method_match.start("mutability")
        end = offset + method_match.end("mutability")
        edits.append(TextEdit(start, end, "nonpayable"))
        fixes.append(
            Fix(
                "VY014",
                index + 1,
                "changed no-return view interface method used as a statement to nonpayable",
                method_match.group("mutability"),
                "nonpayable",
            )
        )
    return apply_edits(source, edits), fixes, []


def _side_effecting_view_interface_methods(
    source: str, facts: SourceFacts
) -> set[tuple[str, str]]:
    methods: set[tuple[str, str]] = set()
    mask = code_mask(source)
    for start, _end, target, method, cast_type in external_call_matches(source, facts):
        if not span_is_code(mask, start, min(start + 1, len(source))):
            continue
        if not _is_standalone_call_statement(source, start):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, start))
        if target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(
                target, vars_for_line, facts
            )
        else:
            target_type = cast_type or infer_expr_type(target, vars_for_line, facts)
        type_name = normalize_type(target_type or "")
        mutability = facts.interfaces.get(type_name, {}).get(method)
        if mutability not in {"view", "pure"}:
            continue
        if method in facts.interface_returns.get(type_name, {}):
            continue
        methods.add((type_name, method))
    return methods


def _is_standalone_call_statement(source: str, start: int) -> bool:
    line_start = source.rfind("\n", 0, start) + 1
    return source[line_start:start].strip() in {"", "staticcall", "extcall"}


def _implemented_view_mutability(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    view_methods = _implemented_interface_methods(source, facts, "view")
    if not view_methods:
        return source, [], []
    lines = source.splitlines(keepends=True)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for function_line, decorators in facts.function_decorators.items():
        function_name = facts.function_names.get(function_line)
        if function_name not in view_methods or "pure" not in decorators or "view" in decorators:
            continue
        decorator_line = facts.function_decorator_lines.get(function_line, {}).get("pure")
        if decorator_line is None or decorator_line > len(lines):
            continue
        line = lines[decorator_line - 1]
        decorator_match = re.match(r"([ \t]*)@pure\b([^\n]*(?:\n|$))", line)
        if decorator_match is None:
            continue
        line_start = rule_context.line_offsets[decorator_line - 1]
        replacement = f"{decorator_match.group(1)}@view{decorator_match.group(2)}"
        edits.append(TextEdit(line_start, line_start + decorator_match.end(), replacement))
        fixes.append(
            Fix(
                "VY014",
                decorator_line,
                "changed implementation mutability to match view interface",
                line.rstrip("\n"),
                replacement.rstrip("\n"),
            )
        )
    return apply_edits(source, edits), fixes, []


def _implemented_payable_mutability(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    payable_methods = _implemented_interface_methods(source, facts, "payable")
    if not payable_methods:
        return source, [], []
    lines = source.splitlines(keepends=True)
    fixes: list[Fix] = []
    edits: list[TextEdit] = []
    for function_line, decorators in facts.function_decorators.items():
        function_name = facts.function_names.get(function_line)
        if (
            function_name not in payable_methods
            or "payable" in decorators
            or "external" not in decorators
            or "view" in decorators
            or "pure" in decorators
        ):
            continue
        external_line = facts.function_decorator_lines.get(function_line, {}).get("external")
        if external_line is None or external_line > len(lines):
            continue
        line = lines[external_line - 1]
        decorator_match = re.match(r"([ \t]*)@external\b", line)
        if decorator_match is None:
            continue
        insert_at = rule_context.line_offsets[external_line - 1] + len(line)
        replacement = f"{decorator_match.group(1)}@payable\n"
        edits.append(TextEdit(insert_at, insert_at, replacement))
        fixes.append(
            Fix(
                "VY014",
                external_line + 1,
                "changed implementation mutability to match payable interface",
                "",
                replacement.rstrip("\n"),
            )
        )
    return apply_edits(source, edits), fixes, []


def _implemented_interface_methods(
    source: str, facts: SourceFacts, mutability: str
) -> set[str]:
    names: set[str] = set()
    for interface_name in _implemented_interface_names(source):
        methods = facts.interfaces.get(interface_name, {})
        names.update(name for name, method_mutability in methods.items() if method_mutability == mutability)
    return names


def _implemented_interface_names(source: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"^[ \t]*implements:[ \t]*(.+?)[ \t]*(?:#.*)?$", source, re.MULTILINE):
        names.update(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", match.group(1)))
    return names


def _view_implementation_names(source: str) -> set[str]:
    names = {
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*public\s*\(",
            source,
            re.MULTILINE,
        )
    }
    decorators: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        decorator = re.fullmatch(r"@([A-Za-z_][A-Za-z0-9_]*)", stripped)
        if decorator is not None:
            decorators.add(decorator.group(1))
            continue
        def_match = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", stripped)
        if def_match is not None:
            if decorators & {"view", "pure"}:
                names.add(def_match.group(1))
            decorators = set()
            continue
        if stripped:
            decorators = set()
    return names


def _pure_immutable_reads(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    immutable_names = _immutable_names(facts)
    mask = rule_context.code_mask
    line_offsets = rule_context.line_offsets
    lines = source.splitlines(keepends=True)
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for function_line, decorators in facts.function_decorators.items():
        if "pure" not in decorators:
            continue
        read_name = _function_read_name(
            source, mask, line_offsets, facts, function_line, immutable_names
        )
        has_self_read = _function_contains(
            source, mask, line_offsets, facts, function_line, "self"
        )
        has_static_raw_call = _function_contains(
            source, mask, line_offsets, facts, function_line, "raw_call"
        )
        has_external_view_call = _function_contains_external_view_call(source, facts, function_line)
        if (
            read_name is None
            and not has_self_read
            and not has_static_raw_call
            and not has_external_view_call
        ):
            continue
        decorator_line = facts.function_decorator_lines.get(function_line, {}).get("pure")
        if decorator_line is None or decorator_line > len(lines):
            continue
        line_start = line_offsets[decorator_line - 1]
        decorator_match = re.search(r"@pure\b", lines[decorator_line - 1])
        if decorator_match is None:
            continue
        edits.append(
            TextEdit(
                line_start + decorator_match.start() + 1, line_start + decorator_match.end(), "view"
            )
        )
        message = (
            f"relaxed pure function that reads immutable {read_name}"
            if read_name is not None
            else (
                "relaxed pure function that queries self"
                if has_self_read
                else (
                    "relaxed pure function that performs static raw_call"
                    if has_static_raw_call
                    else "relaxed pure function that calls a view external function"
                )
            )
        )
        fixes.append(
            Fix(
                "VY015",
                decorator_line,
                message,
                "@pure",
                "@view",
            )
        )
    return apply_edits(source, edits), fixes, []


def _view_log_mutability(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    facts = rule_context.facts
    mask = rule_context.code_mask
    line_offsets = rule_context.line_offsets
    lines = source.splitlines(keepends=True)
    impure_lines = {
        function_line
        for function_line in facts.function_names
        if _function_contains(source, mask, line_offsets, facts, function_line, "log")
    }
    changed = True
    while changed:
        changed = False
        impure_names = {
            function_name
            for function_line, function_name in facts.function_names.items()
            if function_line in impure_lines
        }
        for function_line in facts.function_names:
            if function_line in impure_lines:
                continue
            if any(
                _function_calls_self_function(
                    source,
                    mask,
                    line_offsets,
                    facts,
                    function_line,
                    impure_name,
                )
                for impure_name in impure_names
            ):
                impure_lines.add(function_line)
                changed = True

    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    for function_line in sorted(impure_lines):
        decorators = facts.function_decorators.get(function_line, ())
        if "view" not in decorators:
            continue
        decorator_line = facts.function_decorator_lines.get(function_line, {}).get("view")
        if decorator_line is None or decorator_line > len(lines):
            continue
        line = lines[decorator_line - 1]
        decorator_match = re.match(r"[ \t]*@view\b[^\n]*(?:\n|$)", line)
        if decorator_match is None:
            continue
        line_start = line_offsets[decorator_line - 1]
        edits.append(TextEdit(line_start, line_start + decorator_match.end(), ""))
        fixes.append(
            Fix(
                "VY017",
                decorator_line,
                "removed view decorator from function that emits logs",
                line.rstrip("\n"),
                "",
            )
        )
    return apply_edits(source, edits), fixes, []


def _function_calls_self_function(
    source: str,
    mask: list[bool],
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
    name: str,
) -> bool:
    body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
    pattern = re.compile(rf"\bself\s*\.\s*{re.escape(name)}\s*\(")
    return any(
        span_is_code(mask, match.start(), match.end())
        for match in pattern.finditer(source, body_start, body_end)
    )


def _function_contains_external_view_call(
    source: str, facts: SourceFacts, function_line: int
) -> bool:
    line_offsets = _line_offsets(source)
    body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
    for start, _end, target, method, cast_type in external_call_matches(source, facts):
        if not (body_start <= start < body_end):
            continue
        vars_for_line = facts.vars_at_line(line_number(source, start))
        if target.startswith("self."):
            target_type = facts.storage_vars.get(target[5:]) or infer_expr_type(
                target, vars_for_line, facts
            )
        else:
            target_type = cast_type or infer_expr_type(target, vars_for_line, facts)
        mutability = facts.interfaces.get(normalize_type(target_type or ""), {}).get(method)
        if mutability in {"view", "pure"}:
            return True
    return False


def _immutable_names(facts: SourceFacts) -> set[str]:
    return {name for name, type_name in facts.global_vars.items() if _is_immutable_type(type_name)}


def _is_immutable_type(type_name: str) -> bool:
    type_name = type_name.strip()
    if type_name.startswith("immutable("):
        return True
    return bool(re.fullmatch(r"public\s*\(\s*immutable\s*\(.+\)\s*\)", type_name))


def _function_read_name(
    source: str,
    mask: list[bool],
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
    names: set[str],
) -> str | None:
    body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
    local_names = set(facts.function_params.get(facts.function_names.get(function_line, ""), {}))
    for name in sorted(names):
        if name in local_names:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        for match in pattern.finditer(source, body_start, body_end):
            if span_is_code(mask, match.start(), match.end()) and not _is_attribute_name(
                source, match.start()
            ):
                return name
    return None


def _function_contains(
    source: str,
    mask: list[bool],
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
    name: str,
) -> bool:
    body_start, body_end = _function_body_span(source, line_offsets, facts, function_line)
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    return any(
        span_is_code(mask, match.start(), match.end())
        for match in pattern.finditer(source, body_start, body_end)
    )


def _function_body_span(
    source: str,
    line_offsets: list[int],
    facts: SourceFacts,
    function_line: int,
) -> tuple[int, int]:
    def_line_start = line_offsets[function_line - 1]
    next_line_start = line_offsets[function_line] if function_line < len(line_offsets) else len(source)
    def_line = source[def_line_start:next_line_start]
    colon = _function_header_colon(def_line)
    start = def_line_start + colon + 1 if colon is not None else next_line_start
    end_line = facts.function_ends.get(function_line, len(line_offsets))
    end = line_offsets[end_line] if end_line < len(line_offsets) else len(source)
    return start, end


def _function_header_colon(line: str) -> int | None:
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
        elif char == "#":
            return None
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ":" and depth == 0:
            return index
    return None


def _interface_imports(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    fixes: list[Fix] = []
    diagnostics: list[Diagnostic] = []
    lines = source.splitlines(keepends=True)
    changed = False
    requested_rewrites: dict[str, str] = {}
    taken = code_identifiers(source)
    mask = rule_context.code_mask
    implemented_names = _implemented_interface_names(source)
    offset = 0

    for i, line in enumerate(lines):
        match = re.match(r"(\s*)from\s+vyper\.interfaces\s+import\s+(.+?)(\s*(?:#.*)?)(\n?)$", line)
        if not match or not _line_match_starts_outside_string(source, mask, offset):
            offset += len(line)
            continue
        imports = [part.strip() for part in match.group(2).split(",")]
        parsed_imports = [_split_import_alias(entry) for entry in imports]
        legacy_stubs = [
            name
            for name, alias in parsed_imports
            if alias in {None, name}
            and name in implemented_names
            and name in LEGACY_IMPLEMENTED_INTERFACE_STUBS
        ]
        mapped = [
            name if name in legacy_stubs else IMPORT_RENAMES.get(name, name)
            for name, _alias in parsed_imports
        ]
        names = [name for name, _alias in parsed_imports]
        if (mapped != names or legacy_stubs) and rule_context.is_enabled("VY020"):
            import_entries: list[str] = []
            for entry, (old, alias), new in zip(imports, parsed_imports, mapped, strict=True):
                if old in legacy_stubs:
                    continue
                if old == new:
                    import_entries.append(entry)
                elif alias is not None:
                    import_entries.append(f"{new} as {alias}")
                elif new in taken:
                    import_entries.append(f"{new} as {old}")
                else:
                    import_entries.append(new)
                    requested_rewrites[old] = new
            imports_line = (
                f"{match.group(1)}from ethereum.ercs import {', '.join(import_entries)}{match.group(3)}{match.group(4)}"
                if import_entries
                else ""
            )
            stub_source = "\n".join(
                _legacy_implemented_interface_stub(name, rule_context.facts).rstrip("\n")
                for name in legacy_stubs
            )
            lines[i] = "\n\n".join(part for part in (imports_line.rstrip("\n"), stub_source) if part)
            if lines[i] and line.endswith("\n") and not lines[i].endswith("\n"):
                lines[i] += "\n"
            fixes.append(
                Fix(
                    "VY020",
                    i + 1,
                    (
                        "preserved legacy implemented interface"
                        if legacy_stubs
                        else "updated built-in interface import path"
                    ),
                    line.rstrip("\n"),
                    lines[i].rstrip("\n"),
                )
            )
            changed = True
        elif "vyper.interfaces" in line:
            if rule_context.is_enabled("VYD003"):
                diagnostics.append(
                    Diagnostic(
                        "VYD003", i + 1, "unknown built-in interface import; review manually"
                    )
                )
        offset += len(line)

    current = "".join(lines) if changed else source
    for old, new in requested_rewrites.items():
        next_source, edits = replace_identifier(current, old, new)
        for edit in edits:
            fixes.append(
                Fix(
                    "VY020",
                    line_number(current, edit.start),
                    f"renamed interface type {old} to {new}",
                    old,
                    new,
                )
            )
        current = next_source
    return current, fixes, diagnostics


def _legacy_implemented_interface_stub(name: str, facts: SourceFacts) -> str:
    stub = LEGACY_IMPLEMENTED_INTERFACE_STUBS[name]
    mutability = "view"
    if name == "ERC165":
        mutability = _implementation_mutability(facts, "supportsInterface", "view")
        return stub.replace("{mutability}", mutability)
    if name == "ERC721":
        replacements = {
            "balanceOf": _implementation_mutability(facts, "balanceOf", "view"),
            "ownerOf": _implementation_mutability(facts, "ownerOf", "view"),
            "getApproved": _implementation_mutability(facts, "getApproved", "view"),
            "isApprovedForAll": _implementation_mutability(facts, "isApprovedForAll", "view"),
            "transferFrom": _implementation_mutability(facts, "transferFrom", "nonpayable"),
            "safeTransferFrom": _implementation_mutability(
                facts, "safeTransferFrom", "nonpayable"
            ),
            "approve": _implementation_mutability(facts, "approve", "nonpayable"),
            "setApprovalForAll": _implementation_mutability(
                facts, "setApprovalForAll", "nonpayable"
            ),
        }
        for key, value in replacements.items():
            stub = stub.replace("{" + key + "}", value)
        return stub
    if name == "ERC4626":
        replacements = {
            method: _implementation_mutability(facts, method, default)
            for method, default in {
                "totalAssets": "view",
                "convertToAssets": "view",
                "convertToShares": "view",
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
            }.items()
        }
        for key, value in replacements.items():
            stub = stub.replace("{" + key + "}", value)
        return stub
    return stub


def _implementation_mutability(facts: SourceFacts, name: str, default: str) -> str:
    for function_line, function_name in facts.function_names.items():
        if function_name != name:
            continue
        decorators = facts.function_decorators.get(function_line, ())
        if "payable" in decorators:
            return "payable"
        if "pure" in decorators:
            return "pure"
        if "view" in decorators:
            return "view"
        return "nonpayable"
    return default


def _dependency_imports(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    if "create2_address" not in source:
        return source, [], []
    current, edits = replace_identifier(source, "create2_address", "create2")
    fixes = [
        Fix(
            "VY018",
            line_number(source, edit.start),
            "renamed snekmate create2 helper module",
            "create2_address",
            "create2",
        )
        for edit in edits
    ]
    edits = []
    mask = code_mask(current)
    pattern = re.compile(r"(?<![\w.])create2\._compute_address\b")
    for match in pattern.finditer(current):
        if not span_is_code(mask, match.start(), match.end()):
            continue
        replacement = "create2._compute_create2_address"
        edits.append(TextEdit(match.start(), match.end(), replacement))
        fixes.append(
            Fix(
                "VY018",
                line_number(current, match.start()),
                "renamed snekmate create2 address helper",
                match.group(0),
                replacement,
            )
        )
    return apply_edits(current, edits), fixes, []


def _split_import_alias(entry: str) -> tuple[str, str | None]:
    match = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_]*)(?:\s+as\s+([A-Za-z_][A-Za-z0-9_]*))?", entry
    )
    if match is None:
        return entry, None
    return match.group(1), match.group(2)


def _absolute_relative_imports(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    path = rule_context.path
    config = rule_context.config
    if path is None or not _nested_under_config_path(path, config):
        return source, [], []
    diagnostics: list[Diagnostic] = []
    for match in re.finditer(
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?\s*(?:#.*)?$",
        source,
        re.MULTILINE,
    ):
        module = match.group(1)
        if module in {"math"}:
            continue
        diagnostics.append(
            Diagnostic(
                "VYD015",
                line_number(source, match.start()),
                "nested module uses bare import; 0.4.1 disallows implicit relative imports, review as 'from . import ...'",
            )
        )
    return source, [], diagnostics


def _implements_tuple(rule_context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    lines = source.splitlines(keepends=True)
    offsets = _line_offsets(source)
    mask = rule_context.code_mask
    declarations: list[tuple[int, str, str]] = []

    for index, line in enumerate(lines):
        match = re.match(r"(?P<indent>[ \t]*)implements:\s*(?P<value>.+?)(?P<newline>\n?)$", line)
        if not match or not _line_match_starts_outside_string(source, mask, offsets[index]):
            continue
        if "#" in match.group("value"):
            return source, [], []
        names = _implements_names(match.group("value").strip())
        if names is None:
            return source, [], []
        for name in names:
            declarations.append((index, match.group("indent"), name))

    if len(declarations) <= 1:
        return source, [], []

    first_indent = declarations[0][1]
    if any(indent != first_indent for _index, indent, _name in declarations):
        return source, [], []

    unique_names = list(dict.fromkeys(name for _index, _indent, name in declarations))
    after_value = unique_names[0] if len(unique_names) == 1 else f"({', '.join(unique_names)})"
    first_index = declarations[0][0]
    first_line = lines[first_index]
    newline = "\n" if first_line.endswith("\n") else ""
    replacement = f"{first_indent}implements: {after_value}{newline}"
    edits = [
        TextEdit(offsets[first_index], offsets[first_index] + len(first_line), replacement),
        *(
            TextEdit(offsets[index], offsets[index] + len(lines[index]), "")
            for index, _indent, _name in declarations[1:]
        ),
    ]
    return apply_edits(source, edits), [
        Fix(
            "VY121",
            first_index + 1,
            "merged implements declarations",
            "implements",
            replacement.rstrip("\n"),
        )
    ], []


def _implements_names(value: str) -> list[str] | None:
    if value.startswith("(") and value.endswith(")"):
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", value):
        return None
    return [value]


def _interface_default_ellipsis(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    lines = source.splitlines(keepends=True)
    offsets = _line_offsets(source)
    mask = rule_context.code_mask
    interface_indent: int | None = None
    edits: list[TextEdit] = []
    fixes: list[Fix] = []

    for index, line in enumerate(lines):
        offset = offsets[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not _line_match_starts_outside_string(source, mask, offset):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        header = line.split("#", 1)[0].strip()
        if re.match(r"interface\s+[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?:\s*$", header):
            interface_indent = indent
            continue
        if interface_indent is not None and indent <= interface_indent:
            interface_indent = None
        if interface_indent is None or not re.search(r"\bdef\s+[A-Za-z_]", line):
            continue
        open_index = source.find("(", offset, offset + len(line))
        if open_index == -1:
            continue
        close_index = find_matching(source, open_index)
        if close_index is None or close_index > offset + len(line):
            continue
        args_start = open_index + 1
        args = source[args_start:close_index]
        spans = split_top_level_arg_spans(args)
        if spans is None:
            continue
        for start, end, raw_arg in spans:
            eq = raw_arg.find("=")
            if eq == -1 or raw_arg[eq + 1 :].strip() == "...":
                continue
            value_start = args_start + start + eq + 1
            value_end = args_start + end
            while value_end > value_start and source[value_end - 1].isspace():
                value_end -= 1
            edits.append(TextEdit(value_start, value_end, " ..."))
            fixes.append(
                Fix(
                    "VY122",
                    line_number(source, value_start),
                    "replaced interface default value with ellipsis",
                    source[value_start:value_end].strip(),
                    "...",
                )
            )
    return apply_edits(source, edits), fixes, []


def _interface_default_function(
    rule_context: RuleContext,
) -> tuple[str, list[Fix], list[Diagnostic]]:
    source = rule_context.source
    lines = source.splitlines(keepends=True)
    offsets = _line_offsets(source)
    interface_indent: int | None = None
    edits: list[TextEdit] = []
    fixes: list[Fix] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        header = line.split("#", 1)[0].strip()
        if re.match(r"interface\s+[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?:\s*$", header):
            interface_indent = indent
            index += 1
            continue
        if interface_indent is not None and indent <= interface_indent:
            interface_indent = None
        if interface_indent is None or not re.match(r"def\s+__default__\s*\(", stripped):
            index += 1
            continue

        start_index = index
        header_lines = [line]
        index += 1
        while index < len(lines) and not _interface_header_complete("".join(header_lines)):
            header_lines.append(lines[index])
            index += 1
        start = offsets[start_index]
        end = offsets[index] if index < len(offsets) else len(source)
        before = source[start:end].rstrip()
        edits.append(TextEdit(start, end, ""))
        fixes.append(
            Fix(
                "VY123",
                start_index + 1,
                "removed interface default function",
                before,
                "",
            )
        )
    return apply_edits(source, edits), fixes, []


def _interface_header_complete(header: str) -> bool:
    depth = 0
    for char in header:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
    return depth == 0 and bool(re.search(r":[ \t]*(?:\w+\s*)?(?:#.*)?$", header.rstrip()))


RULES = (
    Rule("legacy_constants", runner=_legacy_constants, changes=(crossing("VY012", (0, 4, 0)),)),
    Rule("immutable_accessor_collisions", runner=_immutable_accessor_collisions, changes=(crossing("VY013", (0, 4, 0)),)),
    Rule("constant_accessor_collisions", runner=_constant_accessor_collisions, changes=(crossing("VY016", (0, 4, 0)),)),
    Rule("interface_storage_assignment_casts", runner=_interface_storage_assignment_casts, changes=(crossing("VY019", (0, 4, 0)),)),
    Rule("interface_view_mutability", runner=_interface_view_mutability, changes=(crossing("VY014", (0, 4, 0)),)),
    Rule(
        "side_effecting_view_interface_calls",
        runner=_side_effecting_view_interface_calls,
        changes=(crossing("VY014", (0, 4, 0)),),
    ),
    Rule("pure_immutable_reads", runner=_pure_immutable_reads, changes=(crossing("VY015", (0, 4, 0)),)),
    Rule("view_log_mutability", runner=_view_log_mutability, changes=(crossing("VY017", (0, 4, 0)),)),
    Rule(
        "interface_imports",
        runner=_interface_imports,
        changes=(
            crossing("VY020", (0, 4, 0)),
            crossing("VYD003", (0, 4, 0)),
        ),
    ),
    Rule(
        "implemented_view_mutability",
        runner=_implemented_view_mutability,
        changes=(crossing("VY014", (0, 4, 0)),),
    ),
    Rule(
        "implemented_payable_mutability",
        runner=_implemented_payable_mutability,
        changes=(crossing("VY014", (0, 4, 0)),),
    ),
    Rule(
        "dependency_imports",
        runner=_dependency_imports,
        changes=(target_floor("VY018", (0, 4, 0)),),
    ),
    Rule(
        "absolute_relative_imports",
        runner=_absolute_relative_imports,
        changes=(crossing("VYD015", (0, 4, 1)),),
    ),
    Rule("implements_tuple", runner=_implements_tuple, changes=(crossing("VY121", "0.5.0a1"),)),
    Rule(
        "interface_default_ellipsis",
        runner=_interface_default_ellipsis,
        changes=(crossing("VY122", "0.5.0a1"),),
    ),
    Rule(
        "interface_default_function",
        runner=_interface_default_function,
        changes=(crossing("VY123", (0, 4, 0)),),
    ),
)
