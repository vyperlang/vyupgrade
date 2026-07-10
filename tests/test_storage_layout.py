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
