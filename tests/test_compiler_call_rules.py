from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import re

import pytest

from vyupgrade.models import Config
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


def _config(source, calls):
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
    return Config(
        paths=(),
        source_ast={"ast": {"ast_type": "Module", "src": f"0:{len(source)}:0", "body": functions}},
        source_external_calls={
            "source_sha256sum": sha256(source.encode()).hexdigest(),
            "calls": sites,
        },
    )


def branch_config():
    call = "oracle.probe()"
    return _config(
        SOURCE,
        [
            (SOURCE.index(call), "probe", call, "view"),
            (SOURCE.rindex(call), "probe", call, "nonpayable"),
        ],
    )


def _context(original, cfg, *, current=None):
    return RuleContext(
        current if current is not None else original,
        cfg,
        MigrationContext.from_specs("0.3.10", "0.4.3"),
        None,
        RULE_CHANGES,
        original,
    )


@pytest.mark.parametrize("strip", [False, True])
def test_proven_branch_bindings_survive_pragma_changes_and_keyword_passes(strip):
    cfg = replace(branch_config(), strip_pragma=strip)
    result = apply_rules(SOURCE, cfg)
    assert "return staticcall oracle.probe()" in result.source
    assert "return extcall oracle.probe()" in result.source
    assert [fix.rule for fix in result.fixes if fix.rule != "VY001"] == ["VY041", "VY040"]
    assert not result.diagnostics


@pytest.mark.parametrize("strip", [False, True])
def test_fallback_preserves_proven_branches_on_rerun_with_pinned_source(strip):
    cfg = replace(branch_config(), source_version="0.3.10", strip_pragma=strip)
    first = apply_rules(SOURCE, cfg)
    # The first run's compiler facts are stale. The lexical fallback should
    # preserve the explicit keyword in each scope.
    assert apply_rules(first.source, cfg).source == first.source
    assert (
        apply_rules(first.source, replace(cfg, source_external_calls=None)).source == first.source
    )


def test_proven_modes_respect_independent_rule_selection():
    cfg = replace(branch_config(), select=frozenset({"VY040"}))
    result = apply_rules(SOURCE, cfg)
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
    cfg = _config(source, [(source.index(text), "price", text, "view")])
    result = apply_rules(source, cfg)
    assert "return staticcall " + text in result.source
    assert not result.diagnostics
    assert apply_rules(result.source, cfg).source == result.source


@pytest.mark.parametrize(
    "invalid", ["digest", "span", "node_id", "method", "incomplete", "changed"]
)
def test_unproven_file_retains_lexical_fallback(invalid):
    cfg = branch_config()
    evidence = cfg.source_external_calls
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


def test_nested_receiver_is_delegated_to_the_existing_keyword_pipeline():
    inner = "oracle.probe()"
    outer = inner + ".probe()"
    source = SOURCE.replace(inner, outer, 1)
    offset = source.index(outer)
    cfg = _config(
        source,
        [
            (offset, "probe", inner, "view"),
            (offset, "probe", outer, "view"),
            (source.rindex(inner), "probe", inner, "nonpayable"),
        ],
    )
    assert proven_call_keywords(_context(source, cfg)) is None


@pytest.mark.parametrize(
    ("grouped", "select_parens"), [(False, True), (True, True), (False, False)]
)
def test_compiler_span_preserves_postfix_grouping(grouped, select_parens):
    call = "oracle.probe()"
    expression = f"({call})[0]" if grouped else f"{call}[0]"
    source = SOURCE.split("    if read_mode:")[0] + f"    return {expression}\n"
    offset = source.index(call)
    cfg = _config(source, [(offset, "probe", call, "view")])
    module = cfg.source_ast["ast"]["body"][0]
    module["body"][0] = {
        "ast_type": "Subscript",
        "node_id": 10,
        "src": f"{source.index(expression)}:{len(expression)}:0",
        "value": module["body"][0],
    }
    if not select_parens:
        cfg = replace(cfg, ignore=frozenset({"VY042"}))
    result = apply_rules(source, cfg)
    correct = (
        "(staticcall oracle.probe())[0]"
        if grouped or select_parens
        else "staticcall oracle.probe()[0]"
    )
    assert result.source.endswith(f"return {correct}\n")
    assert sum(f.rule == "VY042" for f in result.fixes) == (select_parens and not grouped)


def test_crlf_compiler_offsets_are_not_applied_to_captured_bytes():
    original = SOURCE.replace("\n", "\r\n")
    config = branch_config()
    config.source_external_calls["source_sha256sum"] = sha256(original.encode()).hexdigest()
    assert proven_call_keywords(_context(original, config)) is None


def test_exact_function_match_rejects_an_appended_statement():
    current = SOURCE + "    # an earlier pass extended the function\n    pass\n"
    assert proven_call_keywords(_context(SOURCE, branch_config(), current=current)) is None


def test_changed_sibling_uses_lexical_rules_alongside_unchanged_compiler_backed_function():
    other = """
@external
def other(a: address) -> uint256:
    return Read(a).probe()
"""
    original = SOURCE + other
    current = original.replace("return Read(a).probe()", "return Read(a).probe()  # changed")
    probe = "oracle.probe()"
    sibling = "Read(a).probe()"
    cfg = _config(
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
    assert "return staticcall Read(a).probe()  # changed" in migrated
    assert len(fixes) == 3
    assert all(diagnostic.line > SOURCE.count("\n") for diagnostic in diagnostics)
