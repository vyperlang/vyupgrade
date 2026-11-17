from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from eth_abi import decode, encode
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
from eth_utils import keccak

from vyupgrade import engine
from vyupgrade.compiler import CompileResult
from vyupgrade.models import Config
from vyupgrade.rule_groups.interface_wildcards import _compiled_before_interfaces


INTERFACE = """#pragma version 0.4.3
WIDTH: constant(uint256) = 1

@external
@view
def data(large: bool) -> Bytes[WIDTH]:
    ...
"""

IMPLEMENTATION = """#pragma version 0.4.3
import Narrow

implements: Narrow

@external
@view
def data(large: bool) -> Bytes[5]:
    if large:
        return b"abcde"
    return b"a"
"""


def _read_function(return_type: str, body: str) -> str:
    return (
        "\n@external\n@view\n"
        f"def read(other: address, large: bool) -> {return_type}:\n"
        f"{body}\n"
    )


def _prepare(
    tmp_path: Path,
    *,
    body: str,
    return_type: str,
    extra_consumer: bool = False,
    select: frozenset[str] = frozenset(),
):
    entry = tmp_path / "entry.vy"
    interface = tmp_path / "Narrow.vyi"
    source = IMPLEMENTATION + _read_function(return_type, body)
    entry.write_text(source)
    interface.write_text(INTERFACE)
    roots = (entry, tmp_path / "reader.vy") if extra_consumer else (entry,)
    config = Config(
        paths=roots, target_version="0.5.0b2", include_dependencies=True, select=select,
    )
    requests = [engine.bounded_migration_request(entry, source, config)]
    if extra_consumer:
        reader = "#pragma version 0.4.3\nimport Narrow\n" + _read_function(return_type, body)
        roots[1].write_text(reader)
        requests.append(engine.bounded_migration_request(roots[1], reader, config))
    requests.append(
        engine.bounded_migration_request(
            interface, INTERFACE, config, role="dependency", consumer_roots=roots
        )
    )
    return engine.prepare_migrations(requests, config), config


def _deploy(bytecode: str) -> tuple[EthereumTester, str, str]:
    vm = EthereumTester()
    sender = vm.get_accounts()[0]
    tx = vm.send_transaction({"from": sender, "gas": 3_000_000, "data": bytecode})
    receipt = vm.get_transaction_receipt(tx)
    assert receipt["status"] == 1
    return vm, sender, receipt["contract_address"]


def _read(deployment: tuple[EthereumTester, str, str], *, large: bool) -> bytes:
    vm, sender, address = deployment
    selector = keccak(text="read(address,bool)")[:4]
    data = selector + encode(["address", "bool"], [address, large])
    output = vm.call({"from": sender, "to": address, "data": "0x" + data.hex()})
    return decode(["bytes"], bytes.fromhex(output[2:]))[0]


@pytest.mark.parametrize(
    "body",
    [
        "    return staticcall Narrow(other).data(large)",
        "    return staticcall (Narrow(other).data)(large)",
        "    result: Bytes[1] = staticcall Narrow(other).data(large)\n    return result",
    ],
)
def test_wildcard_keeps_named_dependency_bound_in_exact_context(tmp_path: Path, body: str) -> None:
    batch, config = _prepare(tmp_path, body=body, return_type="Bytes[1]")
    entry, interface = batch.files
    assert entry.source_compile.status == "passed", entry.source_compile.stderr
    assert "def data(large: bool) -> Bytes[...]:" in interface.rewrite.source
    assert [(fix.before, fix.after) for fix in interface.rewrite.fixes if fix.rule == "VY125"] == [
        ("Bytes[WIDTH]", "Bytes[...]")
    ]
    assert body in entry.rewrite.source
    decision = engine.validate_migrations(batch, config)
    assert decision.status == "passed", batch.reports
    assert entry.target_compile is not None
    for compile_result in (entry.source_compile, entry.target_compile):
        deployment = _deploy(compile_result.artifacts["bytecode"])
        assert _read(deployment, large=False) == b"a"
        with pytest.raises(TransactionFailed):
            _read(deployment, large=True)


@pytest.mark.parametrize(
    ("return_type", "body"),
    [
        ("Bytes[10]", "    return staticcall (Narrow(other).data)(large)"),
        ("uint256", "    return len(staticcall Narrow(other).data(large))"),
    ],
)
def test_wildcard_keeps_bound_when_caller_does_not_prove_it(
    tmp_path: Path, return_type: str, body: str,
) -> None:
    batch, config = _prepare(tmp_path, body=body, return_type=return_type)
    entry, interface = batch.files
    assert entry.source_compile.status == "passed", entry.source_compile.stderr
    assert body in entry.rewrite.source
    assert "def data(large: bool) -> Bytes[WIDTH]:" in interface.rewrite.source
    assert any(
        diagnostic.rule == "VYD022"
        for report in batch.reports for diagnostic in report.diagnostics
    )
    decision = engine.validate_migrations(batch, config)
    assert not decision.write_allowed, batch.reports
    if return_type == "Bytes[10]":
        original = _deploy(entry.source_compile.artifacts["bytecode"])
        assert _read(original, large=False) == b"a"
        with pytest.raises(TransactionFailed):
            _read(original, large=True)


def test_wildcard_keeps_bound_when_a_consumer_lacks_evidence(tmp_path: Path, monkeypatch) -> None:
    compile_source_file = engine.compile_source_file

    def compile_without_reader_evidence(path, config, source_version):
        result = compile_source_file(path, config, source_version)
        if path.name != "reader.vy" or result.artifacts is None:
            return result
        return replace(result, artifacts={
            name: value for name, value in result.artifacts.items() if name != "annotated_ast"
        })

    monkeypatch.setattr(engine, "compile_source_file", compile_without_reader_evidence)
    batch, _ = _prepare(
        tmp_path,
        body="    return staticcall Narrow(other).data(large)",
        return_type="Bytes[1]",
        extra_consumer=True,
    )
    entry, reader, interface = batch.files
    assert entry.source_compile.status == reader.source_compile.status == "passed"
    assert "annotated_ast" in entry.source_compile.artifacts
    assert "annotated_ast" not in reader.source_compile.artifacts
    assert "def data(large: bool) -> Bytes[WIDTH]:" in interface.rewrite.source
    assert any(
        diagnostic.rule == "VYD022"
        for report in batch.reports for diagnostic in report.diagnostics
    )


def test_wildcard_diagnostic_can_be_selected_on_its_own(tmp_path: Path) -> None:
    batch, _ = _prepare(
        tmp_path,
        body="    return len(staticcall Narrow(other).data(large))",
        return_type="uint256",
        select=frozenset({"VYD022"}),
    )
    interface = batch.files[1]
    assert [diagnostic.rule for diagnostic in interface.rewrite.diagnostics] == ["VYD022"]
    assert interface.rewrite.source == INTERFACE
    assert not interface.rewrite.fixes


def test_wildcard_preserves_narrow_call_through_a_public_getter_interface(tmp_path: Path) -> None:
    entry = tmp_path / "entry.vy"
    interface = tmp_path / "Narrow.vyi"
    source = """#pragma version 0.4.3
import Narrow

implements: Narrow
symbol: public(String[34])

@external
@view
def read_symbol(other: address) -> String[32]:
    return staticcall Narrow(other).symbol()
"""
    declaration = """#pragma version 0.4.3
SYMBOL_MAX_LEN: constant(uint256) = 32

@external
@view
def symbol() -> String[SYMBOL_MAX_LEN]:
    ...
"""
    entry.write_text(source)
    interface.write_text(declaration)
    config = Config(paths=(entry,), target_version="0.5.0b2", include_dependencies=True)
    requests = [
        engine.bounded_migration_request(entry, source, config),
        engine.bounded_migration_request(
            interface, declaration, config, role="dependency", consumer_roots=(entry,),
        ),
    ]
    batch = engine.prepare_migrations(requests, config)
    assert "def symbol() -> String[...]" in batch.files[1].rewrite.source
    assert engine.validate_migrations(batch, config).status == "passed", batch.reports


def test_wildcard_distinguishes_identical_interfaces_in_different_packages(tmp_path: Path) -> None:
    exact_root, wide_root = (tmp_path / package for package in ("exact", "wide"))
    roots = (exact_root / "Exact.vy", wide_root / "Wide.vy")
    config = Config(
        paths=roots, target_version="0.5.0b2", include_dependencies=True,
        compiler_search_paths=(tmp_path,),
    )
    requests = []
    for entry, return_type in zip(roots, ("Bytes[1]", "Bytes[10]"), strict=True):
        entry.parent.mkdir()
        interface = entry.with_name("Narrow.vyi")
        source = IMPLEMENTATION.replace("import Narrow", "from . import Narrow") + _read_function(
            return_type, "    return staticcall (Narrow(other).data)(large)"
        )
        entry.write_text(source)
        interface.write_text(INTERFACE)
        requests.extend((
            engine.bounded_migration_request(entry, source, config),
            engine.bounded_migration_request(
                interface, INTERFACE, config, role="dependency", consumer_roots=(entry,),
            ),
        ))

    batch = engine.prepare_migrations(requests, config)
    exact, exact_interface, wide, wide_interface = batch.files
    for module in (exact, wide):
        assert module.source_compile.status == "passed", module.source_compile.stderr
    assert "def data(large: bool) -> Bytes[...]:" in exact_interface.rewrite.source
    assert "def data(large: bool) -> Bytes[WIDTH]:" in wide_interface.rewrite.source
    assert any(
        diagnostic.rule == "VYD022"
        for module in (wide, wide_interface) for diagnostic in module.rewrite.diagnostics
    )


def test_wildcard_groups_one_declaration_across_compiler_source_ids(tmp_path: Path) -> None:
    roots = (tmp_path / "First.vy", tmp_path / "Second.vy")
    interface = tmp_path / "Narrow.vyi"
    interface.write_text(INTERFACE)
    extra = tmp_path / "Extra.vy"
    extra_source = "#pragma version 0.4.3\nEXTRA: constant(uint256) = 2\n"
    extra.write_text(extra_source)
    config = Config(paths=roots, target_version="0.5.0b2", include_dependencies=True)
    exact = _read_function("Bytes[1]", "    return staticcall Narrow(other).data(large)")
    implementations = (
        IMPLEMENTATION + exact,
        IMPLEMENTATION.replace("import Narrow", "import Extra\nimport Narrow") + exact,
    )
    requests = []
    for path, source in zip(roots, implementations, strict=True):
        path.write_text(source)
        requests.append(engine.bounded_migration_request(path, source, config))
    requests.append(engine.bounded_migration_request(
        extra, extra_source, config, role="dependency", consumer_roots=(roots[1],),
    ))
    requests.append(engine.bounded_migration_request(
        interface, INTERFACE, config, role="dependency", consumer_roots=roots,
    ))
    batch = engine.prepare_migrations(requests, config)
    source_ids = {
        module["source_id"]
        for entry in batch.files[:2]
        for module in entry.source_compile.artifacts["annotated_ast"]["imports"]
        if Path(module["path"]).name == "Narrow.vyi"
    }
    assert len(source_ids) == 2
    declaration = batch.files[-1].rewrite
    assert [fix.after for fix in declaration.fixes if fix.rule == "VY125"] == ["Bytes[...]"]
    assert engine.validate_migrations(batch, config).status == "passed", batch.reports


@pytest.mark.parametrize("digest", [None, "0" * 64])
def test_wildcard_keeps_bound_when_compiler_resolves_an_uncaptured_import(
    tmp_path: Path, monkeypatch, digest: str | None,
) -> None:
    compile_source_file = engine.compile_source_file

    def compile_with_uncaptured_import(path, config, source_version):
        result = compile_source_file(path, config, source_version)
        annotated = result.artifacts.get("annotated_ast")
        if annotated is None:
            return result
        external_module = {
            "ast_type": "Module",
            "source_id": 20,
            "resolved_path": str(tmp_path / "installed" / "Caller.vy"),
            "is_interface": False,
            "body": [],
        }
        if digest is not None:
            external_module["source_sha256sum"] = digest
        return replace(result, artifacts={
            **result.artifacts,
            "annotated_ast": {**annotated, "imports": [*annotated["imports"], external_module]},
        })

    monkeypatch.setattr(engine, "compile_source_file", compile_with_uncaptured_import)
    batch, _ = _prepare(
        tmp_path, body="    return staticcall Narrow(other).data(large)", return_type="Bytes[1]",
    )
    entry, interface = batch.files
    assert entry.source_compile.status == "passed", entry.source_compile.stderr
    assert "def data(large: bool) -> Bytes[WIDTH]:" in interface.rewrite.source
    diagnostic = next(d for d in interface.rewrite.diagnostics if d.rule == "VYD022")
    assert "a module imported by entry.vy lacks verified caller evidence" in diagnostic.message


def test_wildcard_keeps_evidence_with_repeated_and_builtin_imports(tmp_path: Path) -> None:
    # The compiler lists helper once per import request, and gives both builtin
    # interfaces the same source id.
    library = tmp_path / "lib"
    library.mkdir()
    dependencies = {
        library / "helper.vy": "#pragma version 0.4.3\nX: constant(uint256) = 1\n",
        library / "user.vy": "#pragma version 0.4.3\nfrom . import helper\nY: constant(uint256) = helper.X\n",
        tmp_path / "Narrow.vyi": INTERFACE,
    }
    entry = tmp_path / "entry.vy"
    source = IMPLEMENTATION.replace(
        "import Narrow\n",
        "import Narrow\nfrom ethereum.ercs import IERC20\nfrom ethereum.ercs import IERC20Detailed\n"
        "from lib import helper\nfrom lib import user\n",
    ) + _read_function(
        "Bytes[1]",
        "    supply: uint256 = staticcall IERC20(other).totalSupply() + helper.X + user.Y\n"
        "    return staticcall Narrow(other).data(large)",
    )
    entry.write_text(source)
    config = Config(paths=(entry,), target_version="0.5.0b2", include_dependencies=True)
    requests = [engine.bounded_migration_request(entry, source, config)]
    for path, text in dependencies.items():
        path.write_text(text)
        requests.append(engine.bounded_migration_request(
            path, text, config, role="dependency", consumer_roots=(entry,),
        ))
    batch = engine.prepare_migrations(requests, config)
    assert batch.files[0].source_compile.status == "passed", batch.files[0].source_compile.stderr
    declaration = batch.files[-1].rewrite
    assert [fix.after for fix in declaration.fixes if fix.rule == "VY125"] == ["Bytes[...]"], batch.reports
    assert engine.validate_migrations(batch, config).status == "passed", batch.reports


def _prepare_with_unrelated_root(tmp_path: Path, unrelated: str):
    entry, interface, other = tmp_path / "entry.vy", tmp_path / "Narrow.vyi", tmp_path / "other.vy"
    source = IMPLEMENTATION + _read_function("Bytes[1]", "    return staticcall Narrow(other).data(large)")
    for path, text in ((entry, source), (interface, INTERFACE), (other, unrelated)):
        path.write_text(text)
    config = Config(paths=(entry, other), target_version="0.5.0b2", include_dependencies=True)
    requests = [
        engine.bounded_migration_request(entry, source, config),
        engine.bounded_migration_request(other, unrelated, config),
        engine.bounded_migration_request(
            interface, INTERFACE, config, role="dependency", consumer_roots=(entry,),
        ),
    ]
    return engine.prepare_migrations(requests, config)


def _wildcards(batch) -> list[str]:
    return [fix.after for fix in batch.files[-1].rewrite.fixes if fix.rule == "VY125"]


def test_wildcard_ignores_an_unrelated_root_compiled_before_04(tmp_path: Path) -> None:
    batch = _prepare_with_unrelated_root(
        tmp_path, "# pragma version 0.3.10\n\n@external\n@view\ndef answer() -> uint256:\n    return 42\n",
    )
    assert batch.files[1].source_compile.resolved_compiler == "0.3.10"
    assert _wildcards(batch) == ["Bytes[...]"], batch.reports


def test_wildcard_ignores_an_unrelated_compile_with_an_uncaptured_import(
    tmp_path: Path, monkeypatch,
) -> None:
    compile_source_file = engine.compile_source_file

    def compile_other_with_uncaptured_import(path, config, source_version):
        result = compile_source_file(path, config, source_version)
        if path.name != "other.vy":
            return result
        annotated = result.artifacts["annotated_ast"]
        external_module = {
            "ast_type": "Module",
            "source_id": 20,
            "source_sha256sum": "0" * 64,
            "resolved_path": str(tmp_path / "installed" / "Caller.vy"),
            "is_interface": False,
            "body": [],
        }
        return replace(result, artifacts={
            **result.artifacts,
            "annotated_ast": {**annotated, "imports": [*annotated["imports"], external_module]},
        })

    monkeypatch.setattr(engine, "compile_source_file", compile_other_with_uncaptured_import)
    batch = _prepare_with_unrelated_root(tmp_path, "#pragma version 0.4.3\nANSWER: constant(uint256) = 42\n")
    assert _wildcards(batch) == ["Bytes[...]"], batch.reports


def _without_declarations(node):
    if isinstance(node, list):
        return [_without_declarations(item) for item in node]
    if isinstance(node, dict):
        return {
            key: {} if key == "type_decl_node" else _without_declarations(value)
            for key, value in node.items()
        }
    return node


UNRELATED_CALLER = """#pragma version 0.4.3
interface Answer:
    def answer() -> uint256: view

@external
@view
def read(other: address) -> uint256:
    return staticcall Answer(other).answer()
"""


@pytest.mark.parametrize(("stripped", "wildcards"), [("entry.vy", []), ("other.vy", ["Bytes[...]"])])
def test_unresolved_call_blocks_only_its_own_compilation(
    tmp_path: Path, monkeypatch, stripped: str, wildcards: list[str],
) -> None:
    compile_source_file = engine.compile_source_file

    def compile_without_call_declarations(path, config, source_version):
        result = compile_source_file(path, config, source_version)
        if path.name != stripped:
            return result
        annotated = result.artifacts["annotated_ast"]
        return replace(result, artifacts={
            **result.artifacts,
            "annotated_ast": {**annotated, "ast": _without_declarations(annotated["ast"])},
        })

    monkeypatch.setattr(engine, "compile_source_file", compile_without_call_declarations)
    batch = _prepare_with_unrelated_root(tmp_path, UNRELATED_CALLER)
    assert _wildcards(batch) == wildcards, batch.reports
    if not wildcards:
        diagnostic = next(d for d in batch.files[-1].rewrite.diagnostics if d.rule == "VYD022")
        assert "the compiler declaration of a call in entry.vy on line" in diagnostic.message
        assert diagnostic.message.endswith("is unresolved")


def test_wildcard_keeps_bound_when_a_same_project_root_fails_to_compile(tmp_path: Path) -> None:
    # Without compiler evidence the failed root could import the interface in a
    # way the import scanner misses.
    batch = _prepare_with_unrelated_root(tmp_path, "#pragma version 0.4.3\nANSWER: constant(uint256) = \n")
    assert batch.files[1].source_compile.status == "failed"
    assert _wildcards(batch) == []
    diagnostic = next(d for d in batch.files[-1].rewrite.diagnostics if d.rule == "VYD022")
    assert "no matching compiler evidence is available for other.vy" in diagnostic.message


@pytest.mark.parametrize(
    ("compiler", "artifacts", "exempt"),
    [
        ("0.3.10", {"bytecode": "0x00", "ast": {}}, True),
        ("0.3.10", {"ast": {}}, False),
        ("0.4.0b6", {"bytecode": "0x00", "ast": {}}, False),
        ("0.4.3", {"bytecode": "0x00", "ast": {}}, False),
    ],
)
def test_only_a_full_pre_04_compile_is_exempt(compiler, artifacts, exempt) -> None:
    result = CompileResult("passed", artifacts=artifacts, resolved_compiler=compiler)
    assert _compiled_before_interfaces(result) is exempt
