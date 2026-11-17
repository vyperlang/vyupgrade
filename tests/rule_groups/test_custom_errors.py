import hashlib

import pytest

from vyupgrade.models import CompilerFacts
from vyupgrade.rules import apply_rules


def _declaration(*fields: str) -> dict:
    return {
        "ast_type": "ErrorDef",
        "name": "Problem",
        "node_id": 1,
        "body": [
            {"ast_type": "AnnAssign", "target": {"ast_type": "Name", "id": name}} for name in fields
        ],
    }


def _compiler_position(source: str, start: int, end: int) -> dict:
    positions = []
    for offset in (start, end):
        anchor = source.rfind("\n", 0, offset) + 1
        column = len(source[anchor:offset].encode())
        positions.append((source.count("\n", 0, offset) + 1, column, anchor + column))
    (line, col, raw_start), (end_line, end_col, raw_end) = positions
    return {
        "lineno": line,
        "col_offset": col,
        "end_lineno": end_line,
        "end_col_offset": end_col,
        "src": f"{raw_start}:{raw_end - raw_start}:0",
    }


def _evidence(source: str, call: str, expressions: tuple[str, ...]) -> dict:
    start = source.rindex(call)
    cursor = start
    args = []
    for expr in expressions:
        offset = source.index(expr, cursor)
        args.append({"ast_type": "Name", **_compiler_position(source, offset, offset + len(expr))})
        cursor = offset + len(expr)
    names = call.split("(", 1)[0].split(".")
    callee = {"ast_type": "Name", "id": names[0]}
    for name in names[1:]:
        callee = {"ast_type": "Attribute", "attr": name, "value": callee}
    call_node = {
        "ast_type": "Call",
        "args": args,
        "keywords": [],
        "func": callee,
        **_compiler_position(source, start, start + len(call)),
        "type": {"typeclass": "error", "type_decl_node": {"node_id": 1, "source_id": 0}},
    }
    return {
        "ast": {
            "ast_type": "Module",
            "source_sha256sum": hashlib.sha256(source.encode()).hexdigest(),
            "source_id": 0,
            "body": [
                _declaration("available", "required"),
                {"ast_type": "Raise", "exc": call_node},
            ],
        }
    }


def _migrate(config, source, evidence, *, target="0.5.0b2"):
    return apply_rules(
        source,
        config(
            select=frozenset({"VY127"}),
            target_version=target,
        ),
        compiler_facts=CompilerFacts(annotated_ast=evidence),
    )


def test_multiline_error_names_preserve_grouping_comments_and_literals(config):
    call = """Problem(
        # preserve comment and grouping
        ((x)),  # previous argument
        min(x, 3) + 4,
        # trailing separator
    )"""
    source = (
        '#pragma version 0.5.0b1\n"""Problem(x, x) 🐍"""\n'
        "error Problem:\n    available: uint256\n    required: uint256\n"
        "@external\ndef check(x: uint256):\n    # raise Problem(x, 42)\n    raise " + call + "\n"
    )
    output = _evidence(source, call, ("x", "min(x, 3) + 4"))
    result = _migrate(config, source, output)
    after = call.replace("((x))", "available=((x))").replace("min(x, 3)", "required=min(x, 3)")
    assert result.source == source.replace(call, after)
    assert [(fix.rule, fix.before, fix.after) for fix in result.fixes] == [("VY127", call, after)]
    assert _migrate(config, result.source, output).fixes == []


def test_same_line_multibyte_columns_before_and_inside_error(config):
    call = "Problem(\uff56, \uff56 + 1)"
    source = (
        "#pragma version 0.5.0b1\n# 🐍\n@external\ndef f(v: uint256):\n"
        "    assert \uff56 == 0, " + call + "\n"
    )
    evidence = _evidence(source, call, ("\uff56", "\uff56 + 1"))
    after = "Problem(available=\uff56, required=\uff56 + 1)"
    assert _migrate(config, source, evidence).source == source.replace(call, after)


def test_imported_error_is_resolved_by_module_and_node_id(config):
    call = "lib.Problem(n, n)"
    source = (
        "#pragma version 0.5.0b1\nimport lib\n"
        "error Problem:\n    available: uint256\n    required: uint256\n"
        "@external\ndef check(n: uint256):\n    assert n < 20, " + call + "\n"
    )
    output = _evidence(source, call, ("n", "n"))
    call_node = output["ast"]["body"][1]["exc"]
    output["ast"]["body"][1] = {"ast_type": "Assert", "msg": call_node}
    call_node["type"]["type_decl_node"]["source_id"] = 2
    output["imports"] = [
        {"ast_type": "Module", "source_id": 2, "body": [_declaration("low", "high")]}
    ]
    assert _migrate(config, source, output).source == source.replace(
        call, "lib.Problem(low=n, high=n)"
    )


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("type",), {}),
        (("func", "id"), "Another"),
        (("lineno",), None),
        (("type", "typeclass"), "struct"),
        (("type", "type_decl_node"), {"node_id": 1, "source_id": 1}),
        (("src",), "0:4:1"),
        (("args", 0, "src"), "2:4:0"),
        (("args", 0, "src"), "2:4:1"),
        (("args",), []),
        (("keywords",), [{"ast_type": "keyword"}]),
    ],
)
def test_insufficient_call_evidence_leaves_call_for_validation(config, keys, value):
    call = "Problem(x, y)"
    source = (
        "#pragma version 0.5.0b1\n@external\ndef check(x: uint256, y: uint256):\n    raise "
        + call
        + "\n"
    )
    output = _evidence(source, call, ("x", "y"))
    node = output["ast"]["body"][1]["exc"]
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = value
    result = _migrate(config, source, output)
    assert result.source == source
    assert result.fixes == []


def test_ambiguous_declarations_and_stale_evidence_leave_source_alone(config):
    call = "Problem(x, x)"
    source = "#pragma version 0.5.0b1\n@external\ndef check(x: uint256):\n    raise " + call + "\n"
    evidence = _evidence(source, call, ("x", "x"))
    assert _migrate(config, source, None).source == source
    stale = source + "# different snapshot\n"
    assert _migrate(config, stale, evidence).source == stale
    duplicate = _evidence(source, call, ("x", "x"))
    duplicate["ast"]["body"].append(duplicate["ast"]["body"][-1])
    assert _migrate(config, source, duplicate).source == source
    evidence["ast"]["body"].append(_declaration("wrong", "fields"))
    assert _migrate(config, source, evidence).source == source


def test_custom_error_rule_respects_version_and_selection(config):
    call = "Problem(x, x)"
    source = "#pragma version 0.5.0b1\n@external\ndef check(x: uint256):\n    raise " + call + "\n"
    evidence = _evidence(source, call, ("x", "x"))
    assert _migrate(config, source, evidence, target="0.5.0b1").source == source
    ignored = config(target_version="0.5.0b2", ignore=frozenset({"VY127"}))
    assert (
        call
        in apply_rules(source, ignored, compiler_facts=CompilerFacts(annotated_ast=evidence)).source
    )


@pytest.mark.parametrize("endings", [("\r\n",), ("\r",), ("\r", "\r\n", "\n")])
def test_named_arguments_preserve_exact_mixed_line_endings(config, endings):
    call = "Problem(\n        # π\n        (x),\n        x + 1,\n    )"
    normalized = (
        "#pragma version 0.5.0b1\n# β\n@external\ndef check(x: uint256):\n"
        "    assert x == 0, " + call + "\n"
    )
    output = _evidence(normalized, call, ("x", "x + 1"))
    lines = normalized.splitlines()
    original = "".join(line + endings[i % len(endings)] for i, line in enumerate(lines))
    result = _migrate(config, original, output)
    assert result.source == original.replace("(x),", "available=(x),").replace(
        "x + 1,", "required=x + 1,"
    )
    assert [(fix.rule, fix.line) for fix in result.fixes] == [("VY127", 5)]
