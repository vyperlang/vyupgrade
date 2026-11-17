from eth_abi import decode, encode
from eth_tester import EthereumTester
from eth_utils import keccak

from vyupgrade import engine
from vyupgrade.models import Config


ROUTER = """# @version 0.3.10
interface Reader:
    def sample() -> uint256: view
    def inspect() -> uint256: view

interface Writer:
    def sample() -> uint256: nonpayable
    def inspect() -> (uint256, uint256): view

interface Registry:
    def reader() -> Reader: view

struct Book:
    feeds: Reader[1]

@external
def route(book: Book, registry: Registry, dest: address, read_mode: bool) -> uint256:
    samples: DynArray[uint256, 1] = []
    samples.append(book.feeds[0].sample())
    base: uint256 = samples[0] + registry.reader().sample()
    if read_mode:
        oracle: Reader = Reader(dest)
        oracle.inspect()
        return base + oracle.sample()
    else:
        oracle: Writer = Writer(dest)
        oracle.inspect()
        return base + oracle.sample()
"""
FEED = """# @version 0.3.10
@external
@view
def reader() -> address:
    return self

@external
@pure
def sample() -> uint256:
    return 7

@external
@pure
def inspect() -> uint256:
    return 7
"""
WRITER = """# @version 0.3.10
calls: public(uint256)

@external
def sample() -> uint256:
    self.calls += 1
    return 100 + self.calls

@external
@pure
def inspect() -> (uint256, uint256):
    return 11, 13
"""


def test_nested_calls_keep_branch_bindings_and_discard_types(tmp_path):
    files = {
        tmp_path / "router.vy": ROUTER,
        tmp_path / "feed.vy": FEED,
        tmp_path / "writer.vy": WRITER,
    }
    config = Config(paths=tuple(files), target_version="0.4.3")
    requests = []
    for path, source in files.items():
        path.write_text(source)
        requests.append(engine.bounded_migration_request(path, source, config))
    batch = engine.prepare_migrations(requests, config)
    decision = engine.validate_migrations(batch, config)
    assert decision.status == "passed", [(m.path, m.report.target_error) for m in batch.files]
    router = next(m for m in batch.files if m.path.name == "router.vy")
    spelling = "book.feeds[0].sample()"
    span = f"{ROUTER.index(spelling)}:{len(spelling)}:0"
    calls = router.source_compile.artifacts["external_calls_v1"]["calls"]
    assert any(
        call["src"] == span and call["interface"] == "Reader" and call["mutability"] == "view"
        for call in calls
    )
    assert [fix.rule for fix in router.rewrite.fixes].count("VY057") == 2

    for target in (False, True):
        vm = EthereumTester()
        sender = vm.get_accounts()[0]
        addresses = {}
        for migration in batch.files:
            compiled = migration.target_compile if target else migration.source_compile
            assert compiled and compiled.artifacts
            tx = vm.send_transaction({
                "from": sender, "gas": 3_000_000, "data": compiled.artifacts["bytecode"],
            })
            receipt = vm.get_transaction_receipt(tx)
            assert receipt["status"] == 1
            addresses[migration.path.stem] = receipt["contract_address"]
        feed = addresses["feed"]
        writer = addresses["writer"]
        route = keccak(text="route((address[1]),address,address,bool)")[:4]
        cases = [
            (feed, True, 21, 0),
            # The Writer declaration requires two ABI words even when inspect() is discarded.
            (feed, False, None, 0),
            # The same selector on the stateful implementation must revert under STATICCALL.
            (writer, True, None, 0),
            (writer, False, 115, 1),
            (writer, False, 116, 2),
        ]
        for dest, read_mode, expected, count in cases:
            args = encode(
                ["(address[1])", "address", "address", "bool"], [([feed],), feed, dest, read_mode]
            )
            message = {
                "from": sender, "to": addresses["router"], "gas": 1_000_000,
                "data": "0x" + (route + args).hex(),
            }
            if expected is not None:
                result = bytes.fromhex(vm.call(message)[2:])
                assert decode(["uint256"], result) == (expected,)
            receipt = vm.get_transaction_receipt(vm.send_transaction(message))
            assert receipt["status"] == int(expected is not None)
            out = vm.call({
                "from": sender, "to": writer, "data": "0x" + keccak(text="calls()")[:4].hex(),
            })
            assert decode(["uint256"], bytes.fromhex(out[2:])) == (count,)
