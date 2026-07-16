from __future__ import annotations

from pathlib import Path

from vyupgrade.interfaces import split_interfaces_to_vyi


def test_split_interface_import_follows_module_docstring(tmp_path: Path) -> None:
    source = '''#pragma version 0.4.3
"""
@title Example
"""

interface Token:
    def balanceOf(owner: address) -> uint256: view

@external
def f(token: Token, owner: address) -> uint256:
    return staticcall token.balanceOf(owner)
'''

    result = split_interfaces_to_vyi(source, tmp_path / "Main.vy")

    assert result.source.startswith(
        '''#pragma version 0.4.3
"""
@title Example
"""

import Token
'''
    )
    assert result.generated[0].path == tmp_path / "Token.vyi"
