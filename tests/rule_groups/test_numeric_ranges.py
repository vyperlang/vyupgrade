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


def test_signed_integer_declaration_casts_unsigned_loop_expression(config) -> None:
    source = """# @version 0.3.10
N_COINS: constant(uint256) = 3

@internal
def f():
    for i in range(N_COINS - 1):
        index: int128 = i + 1
"""

    result = apply_rules(source, config())

    assert "index: int128 = convert(i + 1, int128)" in result.source
    assert any(fix.rule == "VY052" for fix in result.fixes)


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


def test_loop_variable_type_annotation_for_storage_struct_dynarray(config) -> None:
    source = """# @version 0.3.10
struct PegKeeperInfo:
    peg_keeper: address
    debt_ceiling: uint256

peg_keepers: DynArray[PegKeeperInfo, 8]

@external
def f() -> DynArray[address, 8]:
    result: DynArray[address, 8] = []
    for info in self.peg_keepers:
        result.append(info.peg_keeper)
    return result
"""

    result = apply_rules(source, config())

    assert "for info: PegKeeperInfo in self.peg_keepers:" in result.source
    assert "for info: address in self.peg_keepers:" not in result.source


def test_loop_variable_type_annotation_for_literal_enum_member_list(config) -> None:
    source = """# @version 0.3.10
enum Epoch:
    SLEEP  # 1
    COLLECT  # 2
    EXCHANGE  # 4
    FORWARD  # 8

EPOCH_TIMESTAMPS: constant(uint256[9]) = [0, 10, 20, 30, 40, 50, 60, 70, 80]

@internal
def _epoch_ts(ts: uint256) -> Epoch:
    for epoch in [Epoch.SLEEP, Epoch.COLLECT, Epoch.EXCHANGE, Epoch.FORWARD]:
        if ts < EPOCH_TIMESTAMPS[2 * convert(epoch, uint256)]:
            return epoch
    raise "Bad Epoch"
"""

    result = apply_rules(source, config())

    assert "for epoch: uint256 in [1, 2, 4, 8]:" in result.source
    assert "EPOCH_TIMESTAMPS[2 * epoch]" in result.source
    assert "return convert(epoch, Epoch)" in result.source


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


def test_sentinel_break_range_uses_min_with_bound(config) -> None:
    source = """# @version 0.3.10
MAX_POOLS: constant(uint256) = 2000

@external
def f(pool_count: uint256) -> uint256:
    total: uint256 = 0
    for i in range(MAX_POOLS):
        if i == pool_count:
            break
        total += i
    return total
"""

    result = apply_rules(source, config())

    assert "for i: uint256 in range(min(pool_count, MAX_POOLS), bound=MAX_POOLS):" in result.source
    assert "if i == pool_count" not in result.source
    assert any(fix.rule == "VY071" for fix in result.fixes)


def test_sentinel_break_range_skips_min_when_constant_stop_is_within_bound(config) -> None:
    source = """# @version 0.3.10
MAX_POOLS: constant(uint256) = 2000
POOL_COUNT: constant(uint256) = 17

@external
def f() -> uint256:
    total: uint256 = 0
    for i in range(MAX_POOLS):
        if i == POOL_COUNT:
            break
        total += i
    return total
"""

    result = apply_rules(source, config())

    assert "for i: uint256 in range(POOL_COUNT, bound=MAX_POOLS):" in result.source
    assert "min(POOL_COUNT, MAX_POOLS)" not in result.source


def test_sentinel_break_range_handles_reversed_greater_equal(config) -> None:
    source = """# @version 0.3.10
@external
def f(_n_gauge_types: uint256) -> uint256:
    total: uint256 = 0
    for gauge_type in range(100):
        if _n_gauge_types <= gauge_type:
            break
        total += gauge_type
    return total
"""

    result = apply_rules(source, config())

    assert (
        "for gauge_type: uint256 in range(min(_n_gauge_types, 100), bound=100):"
        in result.source
    )
    assert "if _n_gauge_types <= gauge_type" not in result.source


def test_sentinel_break_range_skips_runtime_signed_stop(config) -> None:
    source = """# @version 0.3.10
MAX_COINS: constant(int128) = 8

@external
def f(n_coins: int128) -> int128:
    total: int128 = 0
    for i in range(MAX_COINS):
        if i >= n_coins:
            break
        total += i
    return total
"""

    result = apply_rules(source, config())

    assert "if i >= n_coins:" in result.source
    assert "min(n_coins, MAX_COINS)" not in result.source


def test_sentinel_break_range_is_idempotent(config) -> None:
    source = """# @version 0.3.10
MAX_POOLS: constant(uint256) = 2000

@external
def f(pool_count: uint256):
    for i in range(MAX_POOLS):
        if i == pool_count:
            break
        pass
"""

    first = apply_rules(source, config())
    second = apply_rules(first.source, config())

    assert second.source == first.source


def test_sentinel_break_range_preserves_commented_break_blocks(config) -> None:
    source = """# @version 0.3.10
MAX_POOLS: constant(uint256) = 2000

@external
def f(pool_count: uint256):
    for i in range(MAX_POOLS):
        if i == pool_count:
            # keep this review note
            break
        pass
"""

    result = apply_rules(source, config())

    assert "if i == pool_count:" in result.source
    assert "# keep this review note" in result.source


def test_sentinel_break_range_ignores_non_break_only_blocks(config) -> None:
    source = """# @version 0.3.10
MAX_POOLS: constant(uint256) = 2000

@external
def f(pool_count: uint256):
    for i in range(MAX_POOLS):
        if i == pool_count:
            pool_count = 0
            break
        pass
"""

    result = apply_rules(source, config())

    assert "if i == pool_count:" in result.source
    assert "pool_count = 0" in result.source


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
