from __future__ import annotations

import pytest

from vyupgrade.analysis import iterable_element_type


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("DynArray[DynArray[uint256, 2], 3]", "DynArray[uint256, 2]"),
        ("DynArray[uint256[2], 3]", "uint256[2]"),
        ("uint256[2][3]", "uint256[2]"),
        ("DynArray[uint256, 2][3]", "DynArray[uint256, 2]"),
        ("(uint256, address)[3]", "(uint256, address)"),
        ("Bytes[32]", None),
        ("String[32]", None),
        ("HashMap[address, uint256]", None),
    ],
)
def test_iterable_element_type_balances_nested_types(
    type_name: str, expected: str | None
) -> None:
    assert iterable_element_type(type_name) == expected
