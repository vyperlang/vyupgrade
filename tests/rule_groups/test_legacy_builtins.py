from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_shifted_method_id_uses_bytes4_output_type(config) -> None:
    source = """# pragma version 0.3.1
@external
def f() -> uint256:
    return convert(method_id("callback(address)", output_type=bytes32), uint256) << 224
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert 'method_id("callback(address)", output_type=bytes4)' in result.source
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


def test_empty_bytes32_raw_call_data_rewrites_to_empty_bytes(config) -> None:
    source = """# @version 0.3.7
@internal
def _safe_send_ether(to: address, value: uint256):
    response: Bytes[32] = raw_call(
        to, empty(bytes32), value=value, max_outsize=32
    )
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert 'to, b"", value=value, max_outsize=32' in result.source
    assert "empty(bytes32)" not in result.source
    assert any(fix.rule == "VY208" for fix in result.fixes)


def test_raw_call_max_outsize_uint_bound_folds_to_literal(config) -> None:
    source = """# @version 0.3.7
@external
def f(target: address, data: Bytes[1024]) -> Bytes[255]:
    return raw_call(target, data, max_outsize=max_value(uint8))
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "max_outsize=255" in result.source
    assert "max_value(uint8)" not in result.source
    assert any(fix.rule == "VY208" for fix in result.fixes)


def test_legacy_method_id_bytes4_output_type_is_preserved(config) -> None:
    source = """# @version 0.2.1
SIG: constant(bytes4) = method_id("transfer(address,uint256)", output_type=bytes4)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert 'method_id("transfer(address,uint256)", output_type=bytes4)' in result.source


def test_method_id_bytes4_return_preserves_output_type(config) -> None:
    source = """# @version 0.3.10
@view
@external
def onERC721Received() -> bytes4:
    return method_id("onERC721Received(address,address,uint256,bytes)", output_type=bytes4)
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert (
        'return method_id("onERC721Received(address,address,uint256,bytes)", output_type=bytes4)'
        in result.source
    )


def test_legacy_method_id_bytes32_comparison_converts_to_bytes4(config) -> None:
    source = """# @version 0.2.1
@external
def f(return_value: bytes32):
    assert return_value == method_id("onERC721Received(address,address,uint256,bytes)", output_type=bytes32)
"""

    result = apply_rules(source, config(target_version="0.2.1"))

    assert (
        'assert convert(return_value, bytes4) == method_id("onERC721Received(address,address,uint256,bytes)", output_type=bytes4)'
        in result.source
    )
    assert "output_type=bytes32" not in result.source


def test_multiline_method_id_bytes32_comparison_converts_full_left_operand(config) -> None:
    source = """# @version 0.2.15
interface ERC721Receiver:
    def onERC721Received(_operator: address, _from: address, _tokenId: uint256, _data: Bytes[1024]) -> bytes32: view

@internal
def f(_to: address, _sender: address, _from: address, _token_id: uint256, _data: Bytes[1024]):
    assert ERC721Receiver(_to).onERC721Received(
        _sender, _from, _token_id, _data
    ) == method_id(
        "onERC721Received(address,address,uint256,bytes)", output_type=bytes32
    )
"""

    result = apply_rules(source, config(target_version="0.4.3"))

    assert "convert(), bytes4)" not in result.source
    assert (
        "convert(staticcall ERC721Receiver(_to).onERC721Received(\n"
        "        _sender, _from, _token_id, _data\n"
        "    ), bytes4) == method_id("
    ) in result.source
    assert "output_type=bytes32" not in result.source
