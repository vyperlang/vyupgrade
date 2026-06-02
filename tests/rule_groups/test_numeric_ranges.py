from __future__ import annotations

from vyupgrade.rules import apply_rules


def test_array_loop_type_inference(config) -> None:
    source = """# @version 0.3.10
@external
def f(items: DynArray[address, 10]):
    for item in items:
        pass
"""

    result = apply_rules(source, config())

    assert "for item: address in items:" in result.source


def test_range_loop_uses_known_bound_integer_type(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 2

@external
def f():
    for i in range(N_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "for i: int128 in range(N_COINS):" in result.source


def test_negative_range_bound_converts_unsigned_constant_to_signed(config) -> None:
    source = """# @version 0.3.6
MAX_PRICES: constant(uint256) = 20

@external
def f():
    tick_spacing: int24 = 1
    for index in range((-1 * MAX_PRICES / 2), (MAX_PRICES / 2)):
        tick: int24 = index * tick_spacing
"""

    result = apply_rules(source, config())

    assert (
        "for index: int24 in range((-1 * convert(MAX_PRICES, int24) // 2), "
        "(convert(MAX_PRICES, int24) // 2), bound=20):" in result.source
    )


def test_unsigned_range_bound_converts_signed_constant(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 3

@external
def f() -> uint256:
    total: uint256 = 0
    for j: uint256 in range(2, N_COINS + 1):
        total += j
    for k: uint256 in range(N_COINS - 1):
        total += k
    return total
"""

    result = apply_rules(source, config())

    assert "range(2, convert(N_COINS, uint256) + 1, bound=2)" in result.source
    assert "range(convert(N_COINS, uint256) - 1, bound=2)" in result.source
    assert any(fix.rule == "VY056" for fix in result.fixes)


def test_unsigned_range_bound_converts_narrow_unsigned_constant(config) -> None:
    source = """# @version 0.3.7
MAX_PATHS: constant(uint8) = 10

@internal
def f() -> uint256:
    total: uint256 = 0
    for i in range(MAX_PATHS + 1):
        total += i
    return total
"""

    result = apply_rules(source, config())

    assert "for i: uint256 in range(convert(MAX_PATHS, uint256) + 1, bound=11):" in result.source
    assert any(fix.rule == "VY056" for fix in result.fixes)


def test_loop_type_uses_nearest_loop_after_same_name_local_decl(config) -> None:
    source = """# @version 0.2.16
MAX_COINS: constant(int128) = 4

@external
def f(base_n_coins: uint256) -> bool:
    x: uint256 = 0
    for x in range(MAX_COINS):
        if x == base_n_coins:
            return True
    return False
"""

    result = apply_rules(source, config())

    assert "if convert(x, uint256) == base_n_coins:" in result.source


def test_pr_3596_loop_variable_type_annotation_for_range_and_arrays(config) -> None:
    source = """# @version 0.3.10
@external
def f(items: DynArray[address, 10]):
    for i in range(3):
        pass
    for item in items:
        pass
"""

    result = apply_rules(source, config())

    assert "for i: uint256 in range(3):" in result.source
    assert "for item: address in items:" in result.source


def test_loop_variable_type_annotation_for_spaced_range_call(config) -> None:
    source = """# @version 0.3.10
@external
def f():
    for i in range (2, 9999999):
        pass
"""

    result = apply_rules(source, config())

    assert "for i: uint256 in range (2, 9999999):" in result.source


def test_literal_range_loop_uses_narrow_unsigned_body_peer(config) -> None:
    source = """# @version 0.3.7
mintingEpoch: uint64
pools: HashMap[bytes32, HashMap[uint64, uint64]]

@external
def f(schema: bytes32):
    for i in range (2, 9999999):
        if i > self.mintingEpoch:
            break
        if self.pools[schema][self.mintingEpoch - i] != 0:
            pass
"""

    result = apply_rules(source, config())

    assert "for i: uint64 in range (2, 9999999):" in result.source


def test_narrow_unsigned_range_loop_keeps_matching_bound_type(config) -> None:
    source = """# @version 0.3.7
mintingEpoch: uint64

@external
def f():
    unempty_start: uint64 = self.mintingEpoch
    for i in range (unempty_start, unempty_start + 99999999):
        if i > self.mintingEpoch - 1:
            break
"""

    result = apply_rules(source, config())

    assert "for i: uint64 in range (unempty_start, unempty_start + 99999999, bound=99999999):" in result.source
    assert "convert(unempty_start, uint256)" not in result.source


def test_loop_variable_type_annotation_for_self_storage_array(config) -> None:
    source = """# @version 0.3.10
queue: address[10]

@external
def f():
    for strategy in self.queue:
        pass
"""

    result = apply_rules(source, config())

    assert "for strategy: address in self.queue:" in result.source


def test_loop_variable_type_annotation_after_multiline_param_comment(config) -> None:
    source = """# @version 0.3.10
interface ERC20:
    def totalSupply() -> uint256: view

@external
def __init__(
    owner: address,  # admin
    accepted_tokens: DynArray[ERC20, 20],
):
    for token in accepted_tokens:
        pass
"""

    result = apply_rules(source, config())

    assert "for token: ERC20 in accepted_tokens:" in result.source


def test_loop_variable_type_annotation_for_literal_address_list(config) -> None:
    source = """# @version 0.3.10
@external
def f(_from: address, _to: address):
    for addr in [_from, _to]:
        pass
"""

    result = apply_rules(source, config())

    assert "for addr: address in [_from, _to]:" in result.source


def test_loop_variable_type_annotation_for_literal_interface_list(config) -> None:
    source = """# @version 0.3.10
interface Registry:
    def numTokens() -> uint256: view

@external
def f(registry_a: Registry, registry_b: Registry):
    for registry in [registry_a, registry_b]:
        n: uint256 = staticcall registry.numTokens()
"""

    result = apply_rules(source, config())

    assert "for registry: Registry in [registry_a, registry_b]:" in result.source


def test_loop_variable_type_annotation_for_literal_empty_address(config) -> None:
    source = """# @version 0.3.10
@external
def f(_gauge: address):
    for target in [_gauge, empty(address)]:
        pass
"""

    result = apply_rules(source, config())

    assert "for target: address in [_gauge, empty(address)]:" in result.source


def test_loop_variable_type_annotation_for_struct_array_attribute(config) -> None:
    source = """# @version 0.3.10
struct Action:
    target: address

struct Proposal:
    actions: DynArray[Action, 10]

proposals: HashMap[uint256, Proposal]

@external
def f(pid: uint256):
    for action in self.proposals[pid].actions:
        pass
"""

    result = apply_rules(source, config())

    assert "for action: Action in self.proposals[pid].actions:" in result.source


def test_pr_3679_range_runtime_stop_gets_bound_keyword(config) -> None:
    source = """# @version 0.3.10
@external
def f(start: uint256):
    for i in range(start, start + 101):
        pass
"""

    result = apply_rules(source, config())

    assert "range(start, start + 101, bound=101)" in result.source
    assert "for i: uint256 in range" in result.source


def test_range_runtime_stop_with_constant_delta_gets_bound_keyword(config) -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@external
def f():
    for i in range(MAX_COINS):
        for x in range(i, i + MAX_COINS):
            pass
"""

    result = apply_rules(source, config())

    assert "range(i, i + MAX_COINS, bound=8)" in result.source
    assert "for i: int128 in range" in result.source
    assert "for x: int128 in range" in result.source


def test_range_runtime_stop_with_public_constant_delta_gets_bound_keyword(config) -> None:
    source = """# @version 0.2.16
MAX_POOLS: public(constant(uint256)) = 2000

@external
def f(_offset: uint256):
    for pindex in range(_offset, _offset + MAX_POOLS):
        pass
"""

    result = apply_rules(source, config())

    assert "range(_offset, _offset + MAX_POOLS, bound=2000)" in result.source


def test_range_runtime_stop_with_max_value_delta_gets_bound_keyword(config) -> None:
    source = """# @version 0.3.10
@external
def f(_index: uint256):
    for i in range(_index, _index + max_value(uint8)):
        pass
"""

    result = apply_rules(source, config())

    assert "range(_index, _index + convert(max_value(uint8), uint256), bound=255)" in result.source


def test_unsigned_range_bound_does_not_convert_existing_bound_keyword(config) -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@external
def f(i2: uint256):
    for x: uint256 in range(i2, i2 + MAX_COINS, bound=MAX_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "range(i2, i2 + convert(MAX_COINS, uint256), bound=MAX_COINS)" in result.source
    assert "bound=convert(MAX_COINS, uint256)" not in result.source


def test_range_loop_type_ignores_bound_keyword(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(int128) = 2

@external
def f():
    for i in range(N_COINS, bound=N_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "for i: int128 in range(N_COINS, bound=N_COINS):" in result.source


def test_range_loop_type_uses_signed_stop_after_literal_start(config) -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@external
def f():
    for i in range(1, MAX_COINS):
        pass
"""

    result = apply_rules(source, config())

    assert "for i: int128 in range(1, MAX_COINS):" in result.source


def test_pr_3679_ambiguous_range_bound_is_diagnostic_only(config) -> None:
    source = """# @version 0.3.10
@external
def f(start: uint256, stop: uint256):
    for i in range(start, stop):
        pass
"""

    result = apply_rules(source, config())

    assert "range(start, stop, bound=" not in result.source
    assert any(diag.rule == "VYD011" for diag in result.diagnostics)


def test_pr_3679_literal_range_bounds_are_left_alone(config) -> None:
    source = """# @version 0.3.10
@external
def f():
    for i in range(1, 4):
        pass
"""

    result = apply_rules(source, config())

    assert "range(1, 4, bound=" not in result.source
    assert not [diag for diag in result.diagnostics if diag.rule == "VYD011"]


def test_dynamic_single_argument_range_gets_bound_diagnostic_at_0_3_10(config) -> None:
    source = """# @version 0.3.9
@external
def f(stop: uint256):
    for i in range(stop):
        pass
"""

    result = apply_rules(source, config(target_version="0.3.10"))

    assert "range(stop, bound=" not in result.source
    assert any(diag.rule == "VYD014" for diag in result.diagnostics)
