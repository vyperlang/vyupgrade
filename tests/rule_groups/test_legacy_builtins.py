from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_shifted_method_id_uses_bytes4_output_type(config) -> None:
    source = """# pragma version 0.3.1
@external
def f() -> uint256:
    return convert(method_id("callback(address)", output_type=bytes32), uint256) << 224
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert 'method_id("callback(address)")' in result.source
    assert "output_type=bytes32" not in result.source


def test_legacy_create_with_code_of_renames_to_create_copy_of(config) -> None:
    source = """# @version 0.1.0b4
template: address

@public
def create() -> address:
    return create_with_code_of(self.template)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "create_copy_of(self.template)" in result.source
    assert any(fix.rule == "VY208" for fix in result.fixes)


def test_legacy_method_id_output_type_is_removed(config) -> None:
    source = """# @version 0.2.1
SIG: constant(bytes4) = method_id("transfer(address,uint256)", output_type=bytes4)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert 'method_id("transfer(address,uint256)")' in result.source
    assert "output_type=bytes4" not in result.source
    assert any(fix.rule == "VY209" for fix in result.fixes)


def test_legacy_method_id_bytes32_comparison_converts_to_bytes4(config) -> None:
    source = """# @version 0.2.1
@external
def f(return_value: bytes32):
    assert return_value == method_id("onERC721Received(address,address,uint256,bytes)", output_type=bytes32)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert (
        'assert convert(return_value, bytes4) == method_id("onERC721Received(address,address,uint256,bytes)")'
        in result.source
    )
    assert "output_type=" not in result.source
