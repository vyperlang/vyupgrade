from __future__ import annotations

import pytest

from vyupgrade.storage_layout import (
    StorageLayout,
    StorageLayoutComparison,
    StorageValue,
    compare_storage_layouts,
    parse_storage_layout,
)


def test_parse_storage_layout_returns_typed_namespaces() -> None:
    layout = parse_storage_layout(
        {
            "storage_layout": {"owner": {"slot": "0x0", "type": "address", "n_slots": 1}},
            "transient_storage_layout": {"scratch": {"slot": 0, "type": "uint256", "n_slots": 1}},
        }
    )

    assert layout == StorageLayout(
        persistent={"owner": StorageValue(0, "address", 1)},
        transient={"scratch": StorageValue(0, "uint256", 1)},
    )


@pytest.mark.parametrize(
    "artifact",
    [
        None,
        {"storage_layout": []},
        {"storage_layout": {"owner": {"slot": 0}}},
        {"storage_layout": {"owner": {"slot": 0, "type": "address", "n_slots": 2}}},
    ],
)
def test_parse_storage_layout_rejects_malformed_artifacts(artifact: object) -> None:
    assert parse_storage_layout(artifact) is None


def test_compare_storage_layouts_returns_typed_full_diff() -> None:
    source = parse_storage_layout({"owner": {"location": "storage", "slot": 0, "type": "address"}})
    target = parse_storage_layout(
        {"storage_layout": {"owner": {"slot": 1, "type": "address", "n_slots": 1}}}
    )
    assert source is not None
    assert target is not None

    assert compare_storage_layouts(source, target) == StorageLayoutComparison(
        equal=False,
        differences=("changed storage: owner slot 0 address -> 1 address",),
    )


def test_compare_storage_layouts_uses_target_ast_interface_evidence() -> None:
    source = parse_storage_layout({"pool": {"slot": 0, "type": "interface Pool"}})
    target = parse_storage_layout(
        {"storage_layout": {"pool": {"slot": 0, "type": "Pool", "n_slots": 1}}}
    )
    assert source is not None
    assert target is not None

    assert not compare_storage_layouts(source, target).equal

    comparison = compare_storage_layouts(
        source,
        target,
        target_ast={
            "ast_type": "Module",
            "body": [
                {"ast_type": "InterfaceDef", "name": "Pool"},
                {
                    "ast_type": "VariableDecl",
                    "target": {"ast_type": "Name", "id": "pool"},
                    "annotation": {"ast_type": "Name", "id": "Pool"},
                },
            ],
        },
    )

    assert comparison == StorageLayoutComparison(equal=True, differences=())


def test_transient_lock_move_is_equal_but_remains_visible() -> None:
    source = parse_storage_layout(
        {
            "lock": {
                "location": "storage",
                "slot": 0,
                "type": "nonreentrant lock",
            }
        }
    )
    target = parse_storage_layout(
        {
            "storage_layout": {},
            "transient_storage_layout": {
                "$nonreentrant:0": {
                    "slot": 0,
                    "type": "nonreentrant lock",
                    "n_slots": 1,
                }
            },
        }
    )
    assert source is not None
    assert target is not None

    comparison = compare_storage_layouts(source, target)

    assert comparison.equal
    assert comparison.differences == (
        "moved storage to transient: $nonreentrant:0 slot 0 nonreentrant lock "
        "-> $nonreentrant:0 slot 0 nonreentrant lock",
    )


def test_parse_storage_layout_accepts_zero_bound_dynarray() -> None:
    layout = parse_storage_layout(
        {"storage_layout": {"empty": {"slot": 0, "type": "DynArray[uint256, 0]", "n_slots": 1}}}
    )

    assert layout == StorageLayout(
        persistent={"empty": StorageValue(0, "DynArray[uint256, 0]", 1)}, transient={}
    )


def _name(name: str) -> dict[str, object]:
    return {"ast_type": "Name", "id": name}


def _subscript(value: str | dict[str, object], *args: dict[str, object]) -> dict[str, object]:
    return {
        "ast_type": "Subscript",
        "value": _name(value) if isinstance(value, str) else value,
        "slice": args[0] if len(args) == 1 else {"ast_type": "Tuple", "elements": list(args)},
    }


def _member(name: str, annotation: dict[str, object]) -> dict[str, object]:
    return {"ast_type": "AnnAssign", "target": _name(name), "annotation": annotation}


def _struct_module(
    members: list[dict[str, object]],
    annotation: dict[str, object] | None = None,
    *,
    extra: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "ast_type": "Module",
        "body": [
            *extra,
            {"ast_type": "StructDef", "name": "Record", "body": members},
            {
                "ast_type": "VariableDecl",
                "target": _name("records"),
                "annotation": annotation
                or _subscript("HashMap", _name("address"), _name("Record")),
            },
        ],
    }


def _struct_comparison(
    source_ast: object,
    target_ast: object,
    type_name: str = "HashMap[address, Record]",
    *,
    location: str = "storage",
    name: str = "records",
) -> StorageLayoutComparison:
    # Exercise the compiler entry point so the regression also checks AST wiring.
    from vyupgrade.compiler import CompileResult, compare_validation_artifacts

    layout = {name: {"slot": 0, "type": type_name, "location": location}}
    source = CompileResult("passed", artifacts={"layout": layout, "ast": source_ast})
    target = CompileResult("passed", artifacts={"layout": layout, "ast": target_ast})
    result = compare_validation_artifacts(source, target).storage_layout
    assert result.equal is not None
    return StorageLayoutComparison(result.equal, result.differences)


def _max_value(type_name: str) -> dict[str, object]:
    return {
        "ast_type": "Call",
        "func": _name("max_value"),
        "args": [_name(type_name)],
        "keywords": [],
    }


def test_mapped_struct_bound_change_is_not_equal() -> None:
    source = _struct_module(
        [
            _member("items", _subscript("DynArray", _name("uint256"), _max_value("int128"))),
            _member("tail", _name("uint256")),
        ]
    )
    target = _struct_module(
        [
            _member("items", _subscript("DynArray", _name("uint256"), _max_value("uint32"))),
            _member("tail", _name("uint256")),
        ]
    )

    comparison = _struct_comparison(source, target)

    assert not comparison.equal
    assert any("records" in line and "definition" in line for line in comparison.differences)


@pytest.mark.parametrize(
    ("type_name", "annotation"),
    [
        ("HashMap[address, Record]", _subscript("HashMap", _name("address"), _name("Record"))),
        (
            "HashMap[address, HashMap[uint256, Record]]",
            _subscript(
                "HashMap",
                _name("address"),
                _subscript("HashMap", _name("uint256"), _name("Record")),
            ),
        ),
        ("Record[2]", _subscript("Record", {"ast_type": "Int", "value": 2})),
        (
            "DynArray[Record, 3][2]",
            _subscript(
                _subscript("DynArray", _name("Record"), {"ast_type": "Int", "value": 3}),
                {"ast_type": "Int", "value": 2},
            ),
        ),
        (
            "DynArray[Record, 3]",
            _subscript("DynArray", _name("Record"), {"ast_type": "Int", "value": 3}),
        ),
        (
            "HashMap[address, DynArray[Record, 3]]",
            _subscript(
                "HashMap",
                _name("address"),
                _subscript("DynArray", _name("Record"), {"ast_type": "Int", "value": 3}),
            ),
        ),
    ],
)
@pytest.mark.parametrize("location", ["storage", "transient"])
def test_nested_struct_members_are_compared(
    type_name: str,
    annotation: dict[str, object],
    location: str,
) -> None:
    source = _struct_module(
        [_member("first", _name("uint256")), _member("tail", _name("uint256"))], annotation
    )
    target = _struct_module(
        [_member("first", _name("bytes32")), _member("tail", _name("uint256"))], annotation
    )

    assert not _struct_comparison(source, target, type_name, location=location).equal
    assert _struct_comparison(source, source, type_name, location=location).equal


@pytest.mark.parametrize(
    "members",
    [
        [_member("tail", _name("uint256")), _member("first", _name("uint256"))],
        [_member("first", _name("uint256"))],
        [
            _member("first", _name("uint256")),
            _member("tail", _name("uint256")),
            _member("extra", _name("uint256")),
        ],
    ],
)
def test_mapped_struct_member_order_and_membership_are_compared(
    members: list[dict[str, object]],
) -> None:
    source = _struct_module([_member("first", _name("uint256")), _member("tail", _name("uint256"))])
    assert not _struct_comparison(source, _struct_module(members)).equal


def test_mapped_struct_recursively_compares_member_structs() -> None:
    source = _struct_module(
        [_member("inner", _name("Inner"))],
        extra=(
            {
                "ast_type": "StructDef",
                "name": "Inner",
                "body": [_member("value", _name("uint256"))],
            },
        ),
    )
    target = _struct_module(
        [_member("inner", _name("Inner"))],
        extra=(
            {
                "ast_type": "StructDef",
                "name": "Inner",
                "body": [_member("value", _name("bytes32"))],
            },
        ),
    )
    assert not _struct_comparison(source, target).equal
    assert _struct_comparison(source, source).equal


def test_struct_bounds_resolve_constant_values_and_legacy_ast_wrappers() -> None:
    source_annotation = _subscript("DynArray", _name("uint256"), _name("BOUND"))
    source_annotation["slice"] = {"ast_type": "Index", "value": source_annotation["slice"]}
    source = _struct_module(
        [_member("items", source_annotation)],
        extra=(
            {
                "ast_type": "AnnAssign",
                "target": _name("BOUND"),
                "annotation": {
                    "ast_type": "Call",
                    "func": _name("constant"),
                    "args": [_name("uint256")],
                },
                "value": _max_value("uint32"),
            },
        ),
    )
    target = _struct_module(
        [
            _member(
                "items",
                _subscript("DynArray", _name("uint256"), {"ast_type": "Int", "value": 2**32 - 1}),
            )
        ]
    )
    assert _struct_comparison({"ast": source}, target).equal
    changed = _struct_module(
        [_member("items", _subscript("DynArray", _name("uint256"), _name("BOUND")))],
        extra=(
            {
                "ast_type": "VariableDecl",
                "target": _name("BOUND"),
                "is_constant": True,
                "value": {"ast_type": "Int", "value": 5},
            },
        ),
    )
    assert not _struct_comparison(source, changed).equal


@pytest.mark.parametrize("missing", [None, {}, {"ast_type": "Module", "body": []}])
def test_mapped_struct_requires_definitions_on_both_sides(missing: object) -> None:
    ast = _struct_module([_member("value", _name("uint256"))])
    assert not _struct_comparison(missing, ast).equal
    assert not _struct_comparison(ast, missing).equal
    assert not _struct_comparison(missing, missing).equal


@pytest.mark.parametrize(
    "members",
    [
        [_member("self", _name("Record"))],
        [_member("unknown", _name("Missing"))],
        [_member("unknown", {"ast_type": "Attribute", "value": _name("lib"), "attr": "Record"})],
    ],
)
def test_mapped_struct_unresolved_or_recursive_members_fail_closed(
    members: list[dict[str, object]],
) -> None:
    ast = _struct_module(members)
    assert not _struct_comparison(ast, ast).equal


def test_mapped_struct_evidence_must_bind_the_layout_variable() -> None:
    ast = _struct_module([_member("value", _name("uint256"))])
    assert not _struct_comparison(ast, ast, name="child.records").equal
    imported = _struct_module(
        [_member("value", _name("uint256"))],
        _subscript(
            "HashMap",
            _name("address"),
            {"ast_type": "Attribute", "value": _name("lib"), "attr": "Record"},
        ),
    )
    assert not _struct_comparison(imported, imported).equal


def test_mapped_struct_ignores_unrelated_ast_changes() -> None:
    members = [_member("value", _name("uint256"))]
    source = _struct_module(
        members,
        extra=(
            {
                "ast_type": "StructDef",
                "name": "Unused",
                "body": [_member("value", _name("uint256"))],
            },
        ),
    )
    target = _struct_module(
        members,
        extra=(
            {
                "ast_type": "StructDef",
                "name": "Unused",
                "body": [_member("other", _name("bytes32"))],
            },
        ),
    )
    assert _struct_comparison(source, target).equal


def test_mapped_struct_cyclic_bound_constants_fail_closed() -> None:
    ast = _struct_module(
        [_member("items", _subscript("DynArray", _name("uint256"), _name("BOUND")))],
        extra=(
            {
                "ast_type": "VariableDecl",
                "target": _name("BOUND"),
                "is_constant": True,
                "value": _name("OTHER"),
            },
            {
                "ast_type": "VariableDecl",
                "target": _name("OTHER"),
                "is_constant": True,
                "value": _name("BOUND"),
            },
        ),
    )
    assert not _struct_comparison(ast, ast).equal


def test_mapped_struct_unsupported_bound_expression_fails_closed() -> None:
    bound = {
        "ast_type": "BinOp",
        "op": {"ast_type": "Add"},
        "left": {"ast_type": "Int", "value": 2},
        "right": {"ast_type": "Int", "value": 3},
    }
    ast = _struct_module([_member("items", _subscript("DynArray", _name("uint256"), bound))])
    assert not _struct_comparison(ast, ast).equal
