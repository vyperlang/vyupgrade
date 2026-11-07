from __future__ import annotations

from hashlib import sha256
import re

import pytest

from vyupgrade.models import CompilerFacts, Config, Fix
from vyupgrade.rule_groups.compiler_calls import proven_call_keywords
from vyupgrade.rule_groups.external_calls import _external_call_syntax
from vyupgrade.rule_registry import RuleContext
from vyupgrade.rules import RULE_CHANGES, apply_rules
from vyupgrade.versions import MigrationContext


SOURCE = """# @version 0.3.10
interface Read:
    def probe() -> uint256: view

interface Write:
    def probe() -> uint256: nonpayable

@external
def route(a: address, read_mode: bool) -> uint256:
    if read_mode:
        oracle: Read = Read(a)
        return oracle.probe()
    else:
        oracle: Write = Write(a)
        return oracle.probe()
"""


def _evidence(source, calls):
    nodes = []
    sites = []
    for node_id, (start, name, text, mode) in enumerate(calls, 1):
        src = f"{start}:{len(text)}:0"
        nodes.append(
            {
                "ast_type": "Call",
                "node_id": node_id,
                "src": src,
                "func": {
                    "ast_type": "Attribute",
                    "attr": name,
                    "value": {"ast_type": "Name", "id": "oracle"},
                },
            }
        )
        interface = (
            "Read"
            if name == "probe" and mode == "view"
            else "Write"
            if name == "probe"
            else "Oracle"
        )
        sites.append(
            {
                "node_id": node_id,
                "src": src,
                "method": name,
                "mutability": mode,
                "interface": interface,
            }
        )
    functions = []
    for match in re.finditer(r"^def (\w+)\(", source, re.M):
        end = source.find("\n@", match.end())
        end = len(source.rstrip()) if end == -1 else len(source[:end].rstrip())
        subtree = [node for node in nodes if match.start() <= int(node["src"].split(":")[0]) < end]
        functions.append(
            {
                "ast_type": "FunctionDef",
                "name": match[1],
                "src": f"{match.start()}:{end - match.start()}:0",
                "body": subtree,
            }
        )
    functions.extend(
        {"ast_type": "InterfaceDef", "name": name}
        for name in re.findall(r"^interface (\w+):", source, re.M)
    )
    return CompilerFacts(
        ast={"ast": {"ast_type": "Module", "src": f"0:{len(source)}:0", "body": functions}},
        external_calls={
            "source_sha256sum": sha256(source.encode()).hexdigest(),
            "calls": sites,
        },
    )


def branch_evidence():
    call = "oracle.probe()"
    return _evidence(
        SOURCE,
        [
            (SOURCE.index(call), "probe", call, "view"),
            (SOURCE.rindex(call), "probe", call, "nonpayable"),
        ],
    )


def _context(original, evidence, *, current=None):
    return RuleContext(
        current if current is not None else original,
        Config(paths=()),
        MigrationContext.from_specs("0.3.10", "0.4.3"),
        None,
        RULE_CHANGES,
        original,
        evidence,
    )


@pytest.mark.parametrize("strip", [False, True])
def test_proven_branch_bindings_survive_pragma_changes_and_keyword_passes(strip):
    cfg = Config(paths=(), strip_pragma=strip)
    result = apply_rules(SOURCE, cfg, compiler_facts=branch_evidence())
    assert "return staticcall oracle.probe()" in result.source
    assert "return extcall oracle.probe()" in result.source
    assert [fix.rule for fix in result.fixes if fix.rule != "VY001"] == ["VY041", "VY040"]
    assert not result.diagnostics


@pytest.mark.parametrize("strip", [False, True])
def test_fallback_preserves_proven_branches_on_rerun_with_pinned_source(strip):
    cfg = Config(paths=(), source_version="0.3.10", strip_pragma=strip)
    first = apply_rules(SOURCE, cfg, compiler_facts=branch_evidence())
    # The first run's compiler facts are stale. The lexical fallback should
    # preserve the explicit keyword in each scope.
    assert apply_rules(first.source, cfg).source == first.source
    assert apply_rules(first.source, cfg, compiler_facts=branch_evidence()).source == first.source


def test_proven_modes_respect_independent_rule_selection():
    cfg = Config(paths=(), select=frozenset({"VY040"}))
    result = apply_rules(SOURCE, cfg, compiler_facts=branch_evidence())
    assert result.source.count("extcall ") == 1
    assert "return oracle.probe()" in result.source
    assert "staticcall " not in result.source


def test_proven_conditional_receiver_is_not_reported_as_unknown():
    source = """# @version 0.3.10
interface Oracle:
    def price() -> uint256: view

@external
@view
def quote(left: Oracle, right: Oracle, use_left: bool) -> uint256:
    return (left if use_left else right).price()
"""
    text = "(left if use_left else right).price()"
    cfg = _evidence(source, [(source.index(text), "price", text, "view")])
    result = apply_rules(source, Config(paths=()), compiler_facts=cfg)
    assert "return staticcall " + text in result.source
    assert not result.diagnostics
    assert apply_rules(result.source, Config(paths=()), compiler_facts=cfg).source == result.source


@pytest.mark.parametrize(
    "invalid", ["digest", "span", "node_id", "method", "incomplete", "changed"]
)
def test_unproven_file_retains_lexical_fallback(invalid):
    cfg = branch_evidence()
    evidence = cfg.external_calls
    source = SOURCE
    if invalid == "digest":
        evidence["source_sha256sum"] = "wrong"
    elif invalid == "incomplete":
        evidence["calls"].pop()
    elif invalid == "changed":
        source = SOURCE.replace("probe() -> uint256: view", "probe() -> uint256: nonpayable")
    else:
        evidence["calls"][0][invalid if invalid != "span" else "src"] = "mismatch"
    assert proven_call_keywords(_context(SOURCE, cfg, current=source)) is None


def test_nested_receiver_groups_inner_call_at_shared_start():
    inner = "oracle.probe()"
    outer = inner + ".probe()"
    source = SOURCE.replace(inner, outer, 1)
    offset = source.index(outer)
    cfg = _evidence(
        source,
        [
            (offset, "probe", inner, "view"),
            (offset, "probe", outer, "view"),
            (source.rindex(inner), "probe", inner, "nonpayable"),
        ],
    )
    function = cfg.ast["ast"]["body"][0]
    inner_node, outer_node, sibling_node = function["body"]
    outer_node["func"]["src"] = f"{offset}:{len(outer) - 2}:0"
    outer_node["func"]["value"] = inner_node
    function["body"] = [outer_node, sibling_node]
    proven = proven_call_keywords(_context(source, cfg))
    assert proven is not None
    assert "staticcall (staticcall oracle.probe()).probe()" in proven[0]


@pytest.mark.parametrize(
    ("grouped", "select_parens"), [(False, True), (True, True), (False, False)]
)
def test_compiler_span_preserves_postfix_grouping(grouped, select_parens):
    call = "oracle.probe()"
    expression = f"({call})[0]" if grouped else f"{call}[0]"
    source = SOURCE.split("    if read_mode:")[0] + f"    return {expression}\n"
    offset = source.index(call)
    cfg = _evidence(source, [(offset, "probe", call, "view")])
    module = cfg.ast["ast"]["body"][0]
    module["body"][0] = {
        "ast_type": "Subscript",
        "node_id": 10,
        "src": f"{source.index(expression)}:{len(expression)}:0",
        "value": module["body"][0],
    }
    config = Config(paths=(), ignore=frozenset() if select_parens else frozenset({"VY042"}))
    result = apply_rules(source, config, compiler_facts=cfg)
    correct = (
        "(staticcall oracle.probe())[0]"
        if grouped or select_parens
        else "staticcall oracle.probe()[0]"
    )
    assert result.source.endswith(f"return {correct}\n")
    assert sum(f.rule == "VY042" for f in result.fixes) == (select_parens and not grouped)


def test_normalized_compiler_offsets_preserve_captured_crlf():
    original = SOURCE.replace("\n", "\r\n")
    proven = proven_call_keywords(_context(original, branch_evidence()))
    assert proven is not None
    assert "return staticcall oracle.probe()\r\n" in proven[0]
    assert "return extcall oracle.probe()\r\n" in proven[0]


def test_exact_function_match_rejects_an_appended_statement():
    current = SOURCE + "    # an earlier pass extended the function\n    pass\n"
    assert proven_call_keywords(_context(SOURCE, branch_evidence(), current=current)) is None


def test_changed_sibling_uses_lexical_rules_alongside_unchanged_compiler_backed_function():
    other = """
@external
def other(a: address) -> uint256:
    return Read(a).probe()
"""
    original = SOURCE + other
    current = original.replace("return Read(a).probe()", "return Read(a).probe() + 0")
    probe = "oracle.probe()"
    sibling = "Read(a).probe()"
    cfg = _evidence(
        original,
        [
            (original.index(probe), "probe", probe, "view"),
            (original.rindex(probe), "probe", probe, "nonpayable"),
            (original.index(sibling), "probe", sibling, "view"),
        ],
    )
    context = _context(original, cfg, current=current)
    proven = proven_call_keywords(context)
    assert proven is not None and len(proven[1]) == 2
    migrated, fixes, diagnostics = _external_call_syntax(context)
    assert "return staticcall oracle.probe()" in migrated
    assert "return extcall oracle.probe()" in migrated
    assert "return staticcall Read(a).probe() + 0" in migrated
    assert len(fixes) == 3
    assert all(diagnostic.line > SOURCE.count("\n") for diagnostic in diagnostics)


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("interface", ["Oracle"]),
        ("mutability", {}),
        ("return_type", []),
        ("return_type", "uint256\n@external"),
        ("method", 12),
        ("node_id", []),
    ],
)
def test_malformed_optional_call_records_defer_to_fallback(key, bad):
    evidence = branch_evidence()
    evidence.external_calls["calls"][0][key] = bad
    assert proven_call_keywords(_context(SOURCE, evidence)) is None


@pytest.mark.parametrize(
    "change",
    [
        ("oracle: Read = Read(a)", "oracle: Write = Write(a)"),
        ("if read_mode:", "if not read_mode:"),
        ("        return oracle.probe()", "    return oracle.probe()"),
    ],
)
def test_branch_statements_cannot_trade_source_facts(change):
    before, after = change
    context = _context(SOURCE, branch_evidence(), current=SOURCE.replace(before, after, 1))
    counterfeit = Fix("VY020", 1, "", "Read", "Write")
    assert proven_call_keywords(context.with_source(context.source, (counterfeit,))) is None


def test_member_calls_require_the_same_compiler_site():
    source = SOURCE.replace("    if read_mode:", "    q.append(1)\n    if read_mode:")
    expr = "q.append(1)"
    scoped = "oracle.probe()"
    evidence = _evidence(
        source,
        [
            (source.index(expr), "append", expr, "nonpayable"),
            (source.index(scoped), "probe", scoped, "view"),
            (source.rindex(scoped), "probe", scoped, "nonpayable"),
        ],
    )
    row = evidence.external_calls["calls"].pop(0)
    context = _context(source, evidence)
    assert proven_call_keywords(context) is None
    evidence.external_calls["member_calls"] = [
        {key: row[key] for key in ("src", "method", "node_id")}
    ]
    result = proven_call_keywords(context)
    assert result is not None
    assert "    q.append(1)" in result[0]
    assert "return staticcall oracle.probe()" in result[0]


def test_rule_only_unknown_calls_report_once_per_site_and_preserve_distinct_sites():
    source = """# @version 0.3.10
@external
def f(x: address) -> uint256:
    return x.missing() + x.missing()
"""
    result = apply_rules(source, Config(paths=(), select=frozenset({"VYD003"})))
    assert [d.rule for d in result.diagnostics] == ["VYD003", "VYD003"]


def test_rule_only_cast_and_homogeneous_conditional_receivers_have_no_spurious_diagnostic():
    source = """# @version 0.3.10
interface Oracle:
    def price() -> uint256: view

@external
def f(x: address, left: Oracle, right: Oracle, pick: bool) -> uint256:
    return Oracle(x).price() + (left if pick else right).price()
"""
    result = apply_rules(source, Config(paths=(), select=frozenset({"VY041", "VYD003"})))
    assert result.source.count("staticcall ") == 2
    assert not result.diagnostics


def test_rule_only_distinguishes_interface_and_native_append_and_pop():
    source = """# @version 0.3.10
interface Api:
    def append(x: uint256): nonpayable
queue: DynArray[uint256, 2]
@external
def f(api: Api, x: uint256):
    self.queue.append(x)
    api.append(self.queue.pop())
"""
    result = apply_rules(source, Config(paths=(), select=frozenset({"VY040", "VYD003"})))
    assert "extcall api.append(self.queue.pop())" in result.source
    assert "    self.queue.append(x)" in result.source
    assert not result.diagnostics


def test_rule_only_multiline_discard_inserts_only_an_assignment_prefix():
    source = """# @version 0.3.10
interface Reader:
    def read(n: uint256) -> uint256: view

@external
def run(dest: address, n: uint256):
    Reader(dest).read(
        n,  # amount
    )  # validate
"""
    cfg = Config(paths=(), select=frozenset({"VY041", "VY057", "VYD003"}))
    result = apply_rules(source, cfg)
    assert result.source == source.replace(
        "    Reader(dest).read(",
        "    __vyupgrade_discard_7: uint256 = staticcall Reader(dest).read(",
    )
    assert not result.diagnostics


def test_unchanged_call_cannot_inherit_old_facts_for_a_new_storage_binding():
    source = (
        SOURCE[: SOURCE.index("@external")]
        + """oracle: Read

@external
def route() -> uint256:
    return self.oracle.probe()
"""
    )
    text = "self.oracle.probe()"
    facts = _evidence(source, [(source.index(text), "probe", text, "view")])
    current = source.replace("oracle: Read", "oracle: Write")
    assert proven_call_keywords(_context(source, facts, current=current)) is None


def test_builtin_cast_alignment_requires_the_actual_import_and_identifier_renames():
    source = """# @version 0.3.10
from vyper.interfaces import ERC20

@external
def supply(token: address) -> uint256:
    return ERC20(token).totalSupply()
"""
    call = "ERC20(token).totalSupply()"
    start = source.index(call)
    facts = _evidence(source, [(start, "totalSupply", call, "view")])
    facts.external_calls["calls"][0]["interface"] = "ERC20"
    module = facts.ast["ast"]
    module["body"].append({"ast_type": "ImportFrom", "module": "vyper.interfaces", "name": "ERC20"})
    # A cast contains a Name node for its type binding. Attribute method names
    # with the same spelling would not qualify as positions of this binding.
    module["body"][0]["body"][0]["func"]["value"] = {
        "ast_type": "Call",
        "func": {"ast_type": "Name", "id": "ERC20", "src": f"{start}:5:0"},
    }
    current = source.replace("vyper.interfaces", "ethereum.ercs").replace("ERC20", "IERC20")
    ctx = _context(source, facts, current=current)
    assert proven_call_keywords(ctx) is None
    receipts = (
        Fix(
            "VY020", 2, "", "from vyper.interfaces import ERC20", "from ethereum.ercs import IERC20"
        ),
        Fix("VY020", 6, "", "ERC20", "IERC20"),
    )
    result = proven_call_keywords(ctx.with_source(current, receipts))
    assert result is not None and "staticcall IERC20(token).totalSupply()" in result[0]
    duplicate = current.replace("from ethereum.ercs", "from thirdparty")
    assert proven_call_keywords(ctx.with_source(duplicate, receipts)) is None
