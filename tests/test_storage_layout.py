from __future__ import annotations

import pytest

from vyupgrade.storage_layout import (
    StorageLayout,
    StorageLayoutComparison,
    StorageValue,
    compare_storage_layouts,
    parse_storage_layout,
    storage_layout_needs_module_types,
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


@pytest.mark.parametrize(("literal", "equal"), [(5, True), (6, False)])
def test_mapped_struct_resolves_bounded_constant_arithmetic(literal: int, equal: bool) -> None:
    base = {
        "ast_type": "VariableDecl",
        "target": _name("BASE"),
        "is_constant": True,
        "value": {"ast_type": "Int", "value": 2},
    }
    bound = {
        "ast_type": "VariableDecl",
        "target": _name("BOUND"),
        "is_constant": True,
        "value": {
            "ast_type": "BinOp",
            "op": {"ast_type": "Add"},
            "left": _name("BASE"),
            "right": {"ast_type": "Int", "value": 3},
        },
    }
    source = _struct_module(
        [_member("items", _subscript("DynArray", _name("uint256"), _name("BOUND")))],
        extra=(base, bound),
    )
    target = _struct_module(
        [
            _member(
                "items",
                _subscript("DynArray", _name("uint256"), {"ast_type": "Int", "value": literal}),
            )
        ]
    )

    assert _struct_comparison(source, target).equal is equal


@pytest.mark.parametrize(("operator", "extent"), [("BitOr", 3), ("Pow", 1025)])
def test_mapped_struct_unresolved_bound_expression_fails_closed(operator: str, extent: int) -> None:
    bound = {
        "ast_type": "BinOp",
        "op": {"ast_type": operator},
        "left": {"ast_type": "Int", "value": 2},
        "right": {"ast_type": "Int", "value": extent},
    }
    ast = _struct_module([_member("items", _subscript("DynArray", _name("uint256"), bound))])
    assert not _struct_comparison(ast, ast).equal


def _flag_module(kind: str, *members: str) -> dict[str, object]:
    return {
        "ast_type": "Module",
        "body": [
            {
                "ast_type": kind,
                "name": "Permission",
                "body": [{"ast_type": "Expr", "value": _name(member)} for member in members],
            },
            {
                "ast_type": "VariableDecl",
                "target": _name("perms"),
                "annotation": _subscript("HashMap", _name("address"), _name("Permission")),
            },
        ],
    }


@pytest.mark.parametrize(
    ("target_members", "equal"),
    [(("READ", "WRITE"), True), (("WRITE", "READ"), False), (("READ",), False)],
)
def test_legacy_enum_matches_flag_only_with_same_member_order(
    target_members: tuple[str, ...], equal: bool
) -> None:
    from vyupgrade.compiler import CompileResult, compare_validation_artifacts

    # Layout shapes as 0.3.10 and 0.4.3 emit them for HashMap[address, Permission].
    source = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "perms": {
                        "slot": 0,
                        "type": "HashMap[address, enum Permission('READ','WRITE')]",
                    }
                },
                "code_layout": {},
            },
            "ast": _flag_module("EnumDef", "READ", "WRITE"),
        },
    )
    target = CompileResult(
        "passed",
        artifacts={
            "layout": {
                "storage_layout": {
                    "perms": {"slot": 0, "type": "HashMap[address, Permission]", "n_slots": 1}
                }
            },
            "ast": _flag_module("FlagDef", *target_members),
        },
    )

    assert compare_validation_artifacts(source, target).storage_layout.equal is equal


def _import(alias: str, path: str) -> dict[str, object]:
    return {
        "ast_type": "Import",
        "name": alias,
        "alias": None,
        "import_info": {"alias": alias, "resolved_path": path},
    }


def _initializes(annotation: dict[str, object]) -> dict[str, object]:
    return {"ast_type": "InitializesDecl", "annotation": annotation}


def _ledger(first: str, second: str, *, type_name: str = "Record") -> dict[str, object]:
    annotation = (
        _name(type_name)
        if type_name == "Record"
        else _subscript("HashMap", _name("address"), _name("Record"))
    )
    return {
        "ast_type": "Module",
        "resolved_path": "/project/ledger.vy",
        "source_sha256sum": f"{first}-{second}",
        "body": [
            {
                "ast_type": "StructDef",
                "name": "Record",
                "body": [_member(first, _name("uint256")), _member(second, _name("uint256"))],
            },
            {
                "ast_type": "VariableDecl",
                "target": _name("record"),
                "annotation": {"ast_type": "Call", "func": _name("public"), "args": [annotation]},
            },
        ],
    }


def _module_comparison(
    source_ast: object,
    target_ast: object,
    name: str = "ledger.record",
    type_name: str = "Record",
) -> StorageLayoutComparison:
    *namespace, leaf = name.split(".")
    width = 2 if type_name == "Record" else 1
    node: dict[str, object] = {leaf: {"slot": 0, "type": type_name, "n_slots": width}}
    for key in reversed(namespace):
        node = {key: node}
    layout = parse_storage_layout({"storage_layout": node})
    assert layout is not None
    return compare_storage_layouts(layout, layout, source_ast=source_ast, target_ast=target_ast)


def _entry(*imports: dict[str, object], body: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "ast": {"ast_type": "Module", "body": [_import("ledger", "/project/ledger.vy"), *body]},
        "imports": list(imports),
    }


@pytest.mark.parametrize("type_name", ["Record", "HashMap[address, Record]"])
def test_initialized_module_struct_member_order_is_compared(type_name: str) -> None:
    body = (_initializes(_name("ledger")),)
    source = _entry(_ledger("left", "right", type_name=type_name), body=body)
    target = _entry(_ledger("right", "left", type_name=type_name), body=body)

    assert _module_comparison(source, source, type_name=type_name).equal
    assert _module_comparison(source, target, type_name=type_name).differences == (
        f"changed storage type definition: ledger.record ({type_name})",
    )


def test_module_initialized_through_a_package_is_keyed_by_its_own_alias() -> None:
    def project(ledger: dict[str, object]) -> dict[str, object]:
        package = {
            "ast_type": "Module",
            "resolved_path": "/project/pkg.vy",
            "body": [_import("ledger", "/project/ledger.vy")],
        }
        attribute = {"ast_type": "Attribute", "value": _name("pkg"), "attr": "ledger"}
        return {
            "ast": {
                "ast_type": "Module",
                "body": [
                    _import("pkg", "/project/pkg.vy"),
                    _initializes({"ast_type": "Subscript", "value": attribute, "slice": {}}),
                ],
            },
            # The compiler repeats a module once per import request that reached it.
            "imports": [package, ledger, ledger],
        }

    source = project(_ledger("left", "right"))
    target = project(_ledger("right", "left"))

    assert _module_comparison(source, source).equal
    assert not _module_comparison(source, target).equal


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {"ast": {"ast_type": "Module", "body": []}},
        _entry(body=(_initializes(_name("ledger")),)),
        _entry(_ledger("left", "right"), body=()),
        _entry(_ledger("left", "right"), _ledger("right", "left"), body=(
            _initializes(_name("ledger")),
        )),
    ],
)
def test_unresolved_module_struct_fails_closed(evidence: object) -> None:
    assert _module_comparison(evidence, evidence).differences == (
        "unverified storage type definition: ledger.record (Record)",
    )


def test_known_width_module_storage_needs_no_module_evidence() -> None:
    assert _module_comparison(None, None, "ledger.owner", "address").equal


@pytest.mark.parametrize(
    ("layout", "needed"),
    [
        ({"record": {"slot": 0, "type": "Record", "n_slots": 2}}, False),
        ({"ledger": {"owner": {"slot": 0, "type": "address", "n_slots": 1}}}, False),
        ({"ledger": {"record": {"slot": 0, "type": "Record", "n_slots": 2}}}, True),
        ({"ledger": {"records": {"slot": 0, "type": "HashMap[address, Record]"}}}, True),
    ],
)
def test_module_type_evidence_is_needed_only_for_module_owned_unknown_types(
    layout: dict[str, object], needed: bool
) -> None:
    parsed = parse_storage_layout({"storage_layout": layout})
    assert storage_layout_needs_module_types(parsed) is needed


def _ledger_type(name: str = "Record") -> dict[str, object]:
    return {"ast_type": "Attribute", "value": _name("ledger"), "attr": name}


def _root_storing(
    annotation: dict[str, object], *imports: dict[str, object], extra: tuple = ()
) -> dict[str, object]:
    variable = {"ast_type": "VariableDecl", "target": _name("x"), "annotation": annotation}
    return _entry(*imports, body=(*extra, variable))


def _flag_ledger(*members: str) -> dict[str, object]:
    return {
        "ast_type": "Module",
        "resolved_path": "/project/ledger.vy",
        "source_sha256sum": "-".join(members),
        "body": [
            {
                "ast_type": "FlagDef",
                "name": "Permission",
                "body": [{"ast_type": "Expr", "value": _name(member)} for member in members],
            }
        ],
    }


@pytest.mark.parametrize(
    ("annotation", "type_name"),
    [
        (_ledger_type(), "Record"),
        ({"ast_type": "Call", "func": _name("public"), "args": [_ledger_type()]}, "Record"),
        (_subscript("HashMap", _name("address"), _ledger_type()), "HashMap[address, Record]"),
        (_subscript(_ledger_type(), {"ast_type": "Int", "value": 3}), "Record[3]"),
    ],
)
def test_root_storage_of_an_imported_struct_compares_its_members(
    annotation: dict[str, object], type_name: str
) -> None:
    source = _root_storing(annotation, _ledger("left", "right"))
    target = _root_storing(annotation, _ledger("right", "left"))

    assert _module_comparison(source, source, "x", type_name).equal
    assert _module_comparison(source, target, "x", type_name).differences == (
        f"changed storage type definition: x ({type_name})",
    )


def test_root_storage_of_an_imported_flag_compares_its_members() -> None:
    annotation = _ledger_type("Permission")
    source = _root_storing(annotation, _flag_ledger("READ", "WRITE"))
    target = _root_storing(annotation, _flag_ledger("WRITE", "READ"))

    assert _module_comparison(source, source, "x", "Permission").equal
    assert not _module_comparison(source, target, "x", "Permission").equal


def test_struct_member_of_an_imported_namesake_type_is_compared() -> None:
    def project(ledger: dict[str, object]) -> dict[str, object]:
        local = {
            "ast_type": "StructDef",
            "name": "Record",
            "body": [_member("inner", _ledger_type())],
        }
        return _root_storing(_name("Record"), ledger, extra=(local,))

    source = project(_ledger("left", "right"))
    target = project(_ledger("right", "left"))

    assert _module_comparison(source, source, "x").equal
    assert not _module_comparison(source, target, "x").equal


@pytest.mark.parametrize(
    "imports",
    [(), (_ledger("left", "right"), _ledger("right", "left"))],
)
def test_root_storage_of_an_unresolved_imported_type_fails_closed(
    imports: tuple[dict[str, object], ...],
) -> None:
    evidence = _root_storing(_ledger_type(), *imports)
    assert _module_comparison(evidence, evidence, "x").differences == (
        "unverified storage type definition: x (Record)",
    )


@pytest.mark.parametrize(
    ("annotation", "needed"),
    [(_ledger_type(), True), (_name("Record"), False), (_name("uint256"), False)],
)
def test_root_storage_of_an_imported_type_needs_module_evidence(
    annotation: dict[str, object], needed: bool
) -> None:
    raw_ast = {"ast": {"ast_type": "Module", "body": [
        {"ast_type": "VariableDecl", "target": _name("x"), "annotation": annotation},
    ]}}
    layout = parse_storage_layout({"storage_layout": {"x": {"slot": 0, "type": "Record"}}})
    assert storage_layout_needs_module_types(layout, raw_ast) is needed


@pytest.mark.parametrize(
    ("field", "needed"),
    [(_ledger_type(), True), (_ledger_type("__interface__"), False), (_name("uint256"), False)],
)
def test_local_struct_with_an_imported_field_needs_module_evidence(
    field: dict[str, object], needed: bool
) -> None:
    raw_ast = {"ast": {"ast_type": "Module", "body": [
        {"ast_type": "StructDef", "name": "Outer", "body": [_member("inner", field)]},
        {"ast_type": "VariableDecl", "target": _name("x"), "annotation": _name("Outer")},
    ]}}
    layout = parse_storage_layout({"storage_layout": {"x": {"slot": 0, "type": "Outer"}}})
    assert storage_layout_needs_module_types(layout, raw_ast) is needed


def test_stored_module_interface_keeps_its_layout_type_comparison() -> None:
    evidence = _root_storing(_ledger_type("__interface__"))
    assert _module_comparison(evidence, evidence, "x", "/project/ledger.vy").equal


def test_beta_import_infos_resolve_imported_types() -> None:
    # The 0.5 betas list an import's facts under ``import_infos``.
    def project(ledger: dict[str, object]) -> dict[str, object]:
        evidence = _root_storing(_ledger_type(), ledger)
        body = evidence["ast"]["body"]
        info = body[0].pop("import_info")
        body[0]["import_infos"] = [info]
        return evidence

    source = project(_ledger("left", "right"))
    target = project(_ledger("right", "left"))

    assert _module_comparison(source, source, "x").equal
    assert not _module_comparison(source, target, "x").equal


def test_imported_flag_spelling_a_scalar_name_compares_its_members() -> None:
    def project(*members: str) -> dict[str, object]:
        ledger = _flag_ledger(*members)
        ledger["body"][0]["name"] = "int"
        return _root_storing(_ledger_type("int"), ledger)

    source = project("READ", "WRITE")
    target = project("WRITE", "READ")

    assert _module_comparison(source, source, "x", "int").equal
    assert not _module_comparison(source, target, "x", "int").equal
