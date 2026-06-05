#!/usr/bin/env python
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import json
import re
import time
import traceback
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from vyupgrade.compiler import (
    compare_artifact_details,
    compare_artifacts,
    compile_source_file,
    compile_target_source,
    target_overlay,
)
from vyupgrade.models import Config
from vyupgrade.rules import apply_rules
from vyupgrade.versions import (
    KNOWN_VERSIONS,
    compiler_version_for_source,
    compiler_version_for_spec,
    infer_pragma,
    is_supported_source_version,
    known_versions_satisfying,
    parse_version,
)


DEFAULT_ROOTS = (Path("~/dev").expanduser(), Path("~/yearn").expanduser())
DEFAULT_OUTPUT = Path("corpus/vyper")
CODESLAW_CHAINS = (
    "ethereum",
    "arbitrum",
    "optimism",
    "base",
    "polygon",
    "bnbchain",
    "scroll",
    "blast",
    "fraxtal",
)
EXCLUDED_PARTS = {
    ".git",
    "corpus",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "site-packages",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "artifacts",
    "cache",
    ".tox",
    ".eggs",
    "out",
    "coverage",
    "target",
}
FIXTURE_MARKERS = (
    ("viperproject", "2vyper", "tests", "resources"),
    ("vyperlang", "vyper", "tests"),
    ("vyperlang", "titanoboa", "tests"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and smoke a local Vyper corpus")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="copy applicable Vyper sources into corpus/")
    build.add_argument("--root", action="append", type=Path, dest="roots")
    build.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    build.add_argument("--max-per-repo", type=int, default=0)

    codeslaw = subparsers.add_parser("codeslaw", help="fetch a Codeslaw Vyper corpus")
    codeslaw.add_argument("--chain", default="ethereum")
    codeslaw.add_argument("--query", default="lang:vyper")
    codeslaw.add_argument("--sort", default="score-desc")
    codeslaw.add_argument("--limit", type=int, default=100)
    codeslaw.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    codeslaw_buckets = subparsers.add_parser(
        "codeslaw-buckets", help="fetch Codeslaw by chain/version buckets and record capped buckets"
    )
    codeslaw_buckets.add_argument("--chain", action="append", choices=CODESLAW_CHAINS)
    codeslaw_buckets.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    old_vyper_bug = subparsers.add_parser(
        "old-vyper-bug", help="import the 2023 Etherscan Vyper reentrancy corpus"
    )
    old_vyper_bug.add_argument(
        "--source", type=Path, default=Path("~/yearn/old-vyper-bug").expanduser()
    )
    old_vyper_bug.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    fiesta = subparsers.add_parser(
        "smart-contract-fiesta", help="import the Smart Contract Fiesta Vyper corpus"
    )
    fiesta.add_argument(
        "--source",
        type=Path,
        default=Path("~/dev/zellic/smart-contract-fiesta").expanduser(),
    )
    fiesta.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    vyper_2026 = subparsers.add_parser("vyper-2026", help="import local vyper-2026 source metadata")
    vyper_2026.add_argument(
        "--source",
        type=Path,
        default=Path("~/dev/banteg/vyper-2026").expanduser(),
    )
    vyper_2026.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    chainsecurity = subparsers.add_parser(
        "chainsecurity", help="import the local ChainSecurity Vyper contract export"
    )
    chainsecurity.add_argument(
        "--source",
        type=Path,
        default=Path("~/dev/chainsecurity/vyper-contracts").expanduser(),
    )
    chainsecurity.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    dedupe = subparsers.add_parser(
        "dedupe", help="merge manifests and dedupe source items by sha256"
    )
    dedupe.add_argument("--manifest", action="append", type=Path, dest="manifests")
    dedupe.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "deduped-manifest.json")

    smoke = subparsers.add_parser(
        "smoke", help="run compiler-backed migration smoke over a corpus manifest"
    )
    smoke.add_argument("--manifest", type=Path, default=DEFAULT_OUTPUT / "manifest.json")
    smoke.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "smoke-results.json")
    smoke.add_argument("--target-version", default="0.4.3")
    smoke.add_argument("--workers", type=int, default=4)
    smoke.add_argument("--limit", type=int, default=0)
    smoke.add_argument("--path", action="append", type=Path, dest="paths")

    args = parser.parse_args()
    if args.command == "build":
        manifest = build_corpus(tuple(args.roots or DEFAULT_ROOTS), args.output, args.max_per_repo)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "codeslaw":
        manifest = fetch_codeslaw(args.chain, args.query, args.sort, args.limit, args.output)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "codeslaw-buckets":
        manifest = fetch_codeslaw_buckets(tuple(args.chain or CODESLAW_CHAINS), args.output)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "old-vyper-bug":
        manifest = import_old_vyper_bug(args.source, args.output)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "smart-contract-fiesta":
        manifest = import_smart_contract_fiesta(args.source, args.output)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "vyper-2026":
        manifest = import_vyper_2026(args.source, args.output)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "chainsecurity":
        manifest = import_chainsecurity(args.source, args.output)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "dedupe":
        manifest = dedupe_manifests(args.manifests, args.output)
        print(json.dumps(_build_summary(manifest), indent=2))
        return 0
    if args.command == "smoke":
        summary = smoke_corpus(
            args.manifest, args.output, args.target_version, args.workers, args.limit, args.paths
        )
        print(json.dumps(summary, indent=2))
        return 0
    raise AssertionError(args.command)


def build_corpus(roots: tuple[Path, ...], output: Path, max_per_repo: int = 0) -> dict[str, Any]:
    contracts_dir = output / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    repo_seen: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    for root in roots:
        for path in root.expanduser().rglob("*.vy"):
            counts["seen"] += 1
            try:
                is_file = path.is_file()
            except OSError:
                counts["stat_error"] += 1
                continue
            if not is_file:
                counts["non_file"] += 1
                continue
            if _is_excluded(path):
                counts["excluded"] += 1
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                counts["read_error"] += 1
                continue
            pragma = infer_pragma(source)
            if pragma is None:
                counts["missing_pragma"] += 1
                continue
            if not is_supported_source_version(pragma):
                counts["unsupported_pragma"] += 1
                continue

            repo_root = _git_root(path)
            if max_per_repo and repo_seen[str(repo_root)] >= max_per_repo:
                counts["over_repo_limit"] += 1
                continue
            repo_seen[str(repo_root)] += 1
            relpath = path.relative_to(repo_root)
            digest = _source_hash(source)
            corpus_repo = f"{repo_root.parent.name}__{repo_root.name}"
            corpus_path = contracts_dir / corpus_repo / relpath
            corpus_path = _write_corpus_source(source, corpus_path, digest, counts)

            compiler = compiler_version_for_spec(pragma)
            item = {
                "source_path": str(path),
                "repo_root": str(repo_root),
                "repo": corpus_repo,
                "relpath": str(relpath),
                "corpus_path": str(corpus_path),
                "corpus_repo_root": str(contracts_dir / corpus_repo),
                "pragma": pragma,
                "source_compiler": compiler,
                "sha256": digest,
            }
            items.append(item)
            counts["applicable"] += 1
            by_repo[corpus_repo] += 1
            if compiler is not None:
                by_compiler[compiler] += 1

    manifest_path = output / "manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": [str(root.expanduser()) for root in roots],
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def import_old_vyper_bug(source: Path, output: Path) -> dict[str, Any]:
    root = source.expanduser()
    source_dir = root / "contracts"
    csv_dir = root / "etherscan-export"
    contracts_dir = output / "contracts"
    archive_path = root / "vyper-contracts-2023.zip"
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    seen_sources: set[Path] = set()

    for csv_path in sorted(csv_dir.glob("*.csv")):
        chain = csv_path.stem
        chain_dir = source_dir / chain
        source_by_address = {path.stem.lower(): path for path in chain_dir.glob("*.vy")}
        with csv_path.open(newline="", encoding="utf-8") as csv_file:
            for row in csv.reader(csv_file):
                if len(row) < 2:
                    counts["invalid_rows"] += 1
                    continue
                address, csv_version = (part.strip() for part in row[:2])
                counts["rows_seen"] += 1
                source_path = source_by_address.get(address.lower())
                if source_path is None:
                    counts["missing_source"] += 1
                    continue
                try:
                    source_text = source_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    counts["read_error"] += 1
                    continue
                pragma = infer_pragma(source_text)
                source_spec = pragma or csv_version
                if not is_supported_source_version(source_spec):
                    counts["unsupported_pragma"] += 1
                    continue

                seen_sources.add(source_path)
                compiler = compiler_version_for_spec(csv_version) or compiler_version_for_spec(
                    source_spec
                )
                corpus_repo = "old_vyper_bug"
                relpath = Path(chain) / source_path.name
                digest = _source_hash(source_text)
                corpus_path = contracts_dir / corpus_repo / relpath
                corpus_path = _write_corpus_source(source_text, corpus_path, digest, counts)
                item = {
                    "source_path": str(source_path),
                    "repo_root": str(root),
                    "repo": corpus_repo,
                    "relpath": str(relpath),
                    "corpus_path": str(corpus_path),
                    "corpus_repo_root": str(contracts_dir / corpus_repo),
                    "pragma": source_spec,
                    "source_pragma": pragma,
                    "source_compiler": compiler,
                    "csv_version": csv_version,
                    "sha256": digest,
                    "chain": chain,
                    "address": address,
                    "archive": str(archive_path) if archive_path.exists() else None,
                }
                items.append(item)
                counts["applicable"] += 1
                by_repo[corpus_repo] += 1
                if compiler is not None:
                    by_compiler[compiler] += 1

        for source_path in chain_dir.glob("*.vy"):
            if source_path not in seen_sources:
                counts["unreferenced_source"] += 1

    manifest_path = output / "old-vyper-bug-manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": [str(root)],
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "archive": str(archive_path) if archive_path.exists() else None,
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def import_smart_contract_fiesta(source: Path, output: Path) -> dict[str, Any]:
    root = source.expanduser()
    organized = root / "organized_contracts"
    contracts_dir = output / "contracts"
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    items: list[dict[str, Any]] = []

    for metadata_path in sorted(organized.rglob("metadata.json")):
        counts["metadata_seen"] += 1
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            counts["metadata_read_error"] += 1
            continue

        compiler_raw = metadata.get("CompilerVersion")
        if not isinstance(compiler_raw, str) or not compiler_raw.startswith("vyper:"):
            counts["non_vyper_metadata"] += 1
            continue
        counts["vyper_metadata"] += 1
        compiler = compiler_version_for_spec(compiler_raw)
        if compiler is None or not is_supported_source_version(compiler):
            counts["unsupported_compiler"] += 1
            continue

        source_files = _fiesta_source_files(metadata_path.parent)
        if not source_files:
            counts["missing_source"] += 1
            continue

        bytecode_hash = str(metadata.get("BytecodeHash") or metadata_path.parent.name)
        for filename, source_text, source_path in source_files:
            pragma = infer_pragma(source_text)
            digest = _source_hash(source_text)
            corpus_repo = "smart_contract_fiesta"
            relpath = Path(bytecode_hash[:2]) / bytecode_hash / _safe_filename(filename)
            corpus_path = contracts_dir / corpus_repo / relpath
            corpus_path = _write_corpus_source(source_text, corpus_path, digest, counts)

            item = {
                "source_path": str(source_path),
                "repo_root": str(root),
                "repo": corpus_repo,
                "relpath": str(relpath),
                "corpus_path": str(corpus_path),
                "corpus_repo_root": str(contracts_dir / corpus_repo),
                "pragma": compiler,
                "source_pragma": pragma,
                "source_compiler": compiler,
                "compiler_version": compiler_raw,
                "contract_name": metadata.get("ContractName"),
                "bytecode_hash": bytecode_hash,
                "sha256": digest,
            }
            items.append(item)
            counts["applicable"] += 1
            by_repo[corpus_repo] += 1
            by_compiler[compiler] += 1

    manifest_path = output / "smart-contract-fiesta-manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": [str(root)],
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def import_vyper_2026(source: Path, output: Path) -> dict[str, Any]:
    import polars as pl

    root = source.expanduser()
    inventory_dir = _vyper_2026_inventory_dir(root)
    if inventory_dir is not None:
        return _import_vyper_2026_inventory(root, inventory_dir, output)

    data_dir = root / "data"
    contracts_dir = output / "contracts"
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    by_metadata_compiler: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    metadata_items: list[dict[str, Any]] = []
    seen_metadata: set[tuple[str | None, str | None, str | None, str | None]] = set()

    for parquet_name in (
        "dormant_bug_source_features.parquet",
        "vyper_source_pattern_flags.parquet",
        "non_tvl_source_signals.parquet",
        "etherscan_source_metadata.parquet",
    ):
        parquet_path = data_dir / parquet_name
        if not parquet_path.exists():
            counts["missing_parquet"] += 1
            continue
        schema = pl.scan_parquet(parquet_path).collect_schema()
        columns = [
            column
            for column in (
                "chain",
                "address",
                "source_available",
                "source_origin",
                "source_path",
                "contract_name",
                "compiler_version",
                "normalized_vyper_version",
                "source_len",
                "source_sha256",
            )
            if column in schema
        ]
        for row in pl.read_parquet(parquet_path, columns=columns).iter_rows(named=True):
            counts[f"{parquet_name}:rows_seen"] += 1
            compiler_raw = row.get("normalized_vyper_version") or row.get("compiler_version")
            compiler = compiler_version_for_spec(compiler_raw)
            if compiler is None or not is_supported_source_version(compiler):
                counts[f"{parquet_name}:unsupported_compiler"] += 1
                continue

            key = (row.get("chain"), row.get("address"), row.get("source_sha256"), compiler)
            if key in seen_metadata:
                counts[f"{parquet_name}:duplicate_metadata"] += 1
                continue
            seen_metadata.add(key)
            by_metadata_compiler[compiler] += 1
            record = {
                "source_path": row.get("source_path"),
                "repo_root": str(root),
                "repo": "vyper_2026",
                "pragma": compiler,
                "source_compiler": compiler,
                "compiler_version": row.get("compiler_version"),
                "contract_name": row.get("contract_name"),
                "sha256": row.get("source_sha256"),
                "chain": row.get("chain"),
                "address": row.get("address"),
                "source_available": row.get("source_available"),
                "source_origin": row.get("source_origin"),
                "source_len": row.get("source_len"),
                "parquet": str(parquet_path),
            }
            metadata_items.append(record)

            local_source = row.get("source_path")
            if not local_source:
                counts[f"{parquet_name}:missing_source_path"] += 1
                continue
            local_source_path = Path(local_source).expanduser()
            if not local_source_path.is_absolute():
                local_source_path = root / local_source_path
            if not local_source_path.exists():
                counts[f"{parquet_name}:missing_source_file"] += 1
                continue
            try:
                source_text = local_source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                counts[f"{parquet_name}:read_error"] += 1
                continue

            digest = _source_hash(source_text)
            corpus_repo = "vyper_2026"
            relpath = Path(row.get("chain") or "unknown") / f"{row.get('address') or digest}.vy"
            corpus_path = contracts_dir / corpus_repo / relpath
            corpus_path = _write_corpus_source(source_text, corpus_path, digest, counts)
            item = {
                **record,
                "source_path": str(local_source_path),
                "relpath": str(relpath),
                "corpus_path": str(corpus_path),
                "corpus_repo_root": str(contracts_dir / corpus_repo),
                "source_pragma": infer_pragma(source_text),
                "sha256": digest,
            }
            items.append(item)
            counts["applicable"] += 1
            by_repo[corpus_repo] += 1
            by_compiler[compiler] += 1

    manifest_path = output / "vyper-2026-manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": [str(root)],
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "by_metadata_compiler": by_metadata_compiler.most_common(),
        "metadata_items": metadata_items,
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def _vyper_2026_inventory_dir(root: Path) -> Path | None:
    if (root / "source_catalog.parquet").exists():
        return root
    candidates: list[Path] = []
    if root.is_dir():
        candidates.extend(path for path in root.iterdir() if path.is_dir())
    source_enrichment = root / "data" / "source_enrichment"
    if source_enrichment.exists():
        candidates.extend(path for path in source_enrichment.iterdir() if path.is_dir())
    for candidate in sorted(candidates, reverse=True):
        if (candidate / "source_catalog.parquet").exists():
            return candidate
    return None


def _import_vyper_2026_inventory(source: Path, inventory_dir: Path, output: Path) -> dict[str, Any]:
    import polars as pl

    repo_root = _vyper_2026_inventory_repo_root(source, inventory_dir)
    contracts_dir = output / "contracts"
    corpus_repo = "vyper_2026"
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    by_metadata_compiler: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    metadata_items: list[dict[str, Any]] = []
    metadata_by_source = _vyper_2026_inventory_metadata(
        inventory_dir, metadata_items, by_metadata_compiler, counts
    )

    catalog_path = inventory_dir / "source_catalog.parquet"
    catalog = pl.read_parquet(catalog_path)
    for row in catalog.iter_rows(named=True):
        counts["source_catalog.parquet:rows_seen"] += 1
        source_id = row.get("source_id")
        digest = str(row.get("source_sha256") or "").strip()
        source_format = row.get("source_format")
        if not source_id or not digest:
            counts["source_catalog.parquet:missing_source_id"] += 1
            continue
        source_path = _resolve_vyper_2026_inventory_path(
            row.get("catalog_path"), repo_root, inventory_dir
        )
        if source_path is None or not source_path.exists():
            counts["source_catalog.parquet:missing_source_file"] += 1
            continue
        metadata = metadata_by_source.get(str(source_id), {})

        if source_format == "vy":
            item = _import_vyper_2026_inventory_source(
                row,
                metadata,
                source_path,
                contracts_dir / corpus_repo / "vy",
                repo_root,
                counts,
            )
            if item is None:
                continue
            items.append(item)
            counts["applicable"] += 1
            by_repo[corpus_repo] += 1
            if item["source_compiler"] is not None:
                by_compiler[item["source_compiler"]] += 1
            continue

        if source_format == "standard_json":
            new_items = _import_vyper_2026_inventory_standard_json(
                row,
                metadata,
                source_path,
                contracts_dir / corpus_repo / "standard_json" / digest,
                repo_root,
                counts,
            )
            items.extend(new_items)
            counts["applicable"] += len(new_items)
            by_repo[corpus_repo] += len(new_items)
            for item in new_items:
                if item["source_compiler"] is not None:
                    by_compiler[item["source_compiler"]] += 1
            continue

        counts[f"source_catalog.parquet:unsupported_source_format:{source_format}"] += 1

    manifest_path = output / "vyper-2026-manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": [str(source)],
        "inventory": str(inventory_dir),
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "by_metadata_compiler": by_metadata_compiler.most_common(),
        "metadata_items": metadata_items,
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def _vyper_2026_inventory_repo_root(source: Path, inventory_dir: Path) -> Path:
    source = source.resolve()
    inventory_dir = inventory_dir.resolve()
    for candidate in (source, inventory_dir, *inventory_dir.parents):
        if (candidate / "data" / "source_enrichment").exists():
            return candidate
    return source


def _vyper_2026_inventory_metadata(
    inventory_dir: Path,
    metadata_items: list[dict[str, Any]],
    by_metadata_compiler: Counter[str],
    counts: Counter[str],
) -> dict[str, dict[str, Any]]:
    import polars as pl

    metadata_by_source: dict[str, dict[str, Any]] = {}
    artifacts_path = inventory_dir / "source_artifacts.parquet"
    if artifacts_path.exists():
        for row in pl.read_parquet(artifacts_path).iter_rows(named=True):
            counts["source_artifacts.parquet:rows_seen"] += 1
            record = _vyper_2026_artifact_record(row, artifacts_path)
            metadata_items.append(record)
            _record_vyper_2026_source_metadata(record, metadata_by_source, by_metadata_compiler)

    coverage_path = inventory_dir / "source_coverage.parquet"
    if coverage_path.exists():
        coverage = pl.read_parquet(coverage_path)
        for row in coverage.iter_rows(named=True):
            counts["source_coverage.parquet:rows_seen"] += 1
            for scope in ("address", "implementation", "metadata"):
                record = _vyper_2026_coverage_record(row, scope, coverage_path)
                if record is not None:
                    _record_vyper_2026_source_metadata(
                        record, metadata_by_source, by_metadata_compiler
                    )
    return metadata_by_source


def _vyper_2026_artifact_record(row: dict[str, Any], parquet_path: Path) -> dict[str, Any]:
    compiler_raw = row.get("source_compiler") or row.get("compiler_version")
    compiler = compiler_version_for_spec(compiler_raw)
    return {
        "source_id": row.get("source_id"),
        "source_path": row.get("source_path"),
        "repo_root": str(parquet_path.parent),
        "repo": "vyper_2026",
        "pragma": compiler,
        "source_compiler": compiler,
        "compiler_version": row.get("compiler_version"),
        "contract_name": row.get("contract_name"),
        "sha256": row.get("source_sha256"),
        "chain": None,
        "address": row.get("address"),
        "source_available": row.get("has_source_file"),
        "source_origin": row.get("provider"),
        "source_len": row.get("source_len"),
        "source_format": row.get("source_format"),
        "match_kind": row.get("match_kind"),
        "parquet": str(parquet_path),
    }


def _vyper_2026_coverage_record(
    row: dict[str, Any], scope: str, parquet_path: Path
) -> dict[str, Any] | None:
    source_id = row.get(f"{scope}_source_id")
    if not source_id:
        return None
    compiler_raw = row.get(f"{scope}_source_compiler") or row.get(f"{scope}_compiler_version")
    compiler = compiler_version_for_spec(compiler_raw)
    return {
        "source_id": source_id,
        "source_path": row.get(f"{scope}_source_path"),
        "repo_root": str(parquet_path.parent),
        "repo": "vyper_2026",
        "pragma": compiler,
        "source_compiler": compiler,
        "compiler_version": row.get(f"{scope}_compiler_version"),
        "contract_name": row.get(f"{scope}_contract_name"),
        "sha256": row.get(f"{scope}_source_sha256"),
        "chain": row.get("chain"),
        "address": row.get("address"),
        "source_available": row.get(f"{scope}_source_available"),
        "source_origin": row.get(f"{scope}_source_provider"),
        "source_len": row.get(f"{scope}_source_len"),
        "source_format": row.get(f"{scope}_source_format"),
        "match_kind": row.get(f"{scope}_source_match_kind"),
        "parquet": str(parquet_path),
    }


def _record_vyper_2026_source_metadata(
    record: dict[str, Any],
    metadata_by_source: dict[str, dict[str, Any]],
    by_metadata_compiler: Counter[str],
) -> None:
    compiler = record.get("source_compiler")
    if compiler is not None:
        by_metadata_compiler[compiler] += 1
    if compiler is None or not is_supported_source_version(compiler):
        return
    source_id = record.get("source_id")
    if source_id and source_id not in metadata_by_source:
        metadata_by_source[str(source_id)] = record


def _import_vyper_2026_inventory_source(
    row: dict[str, Any],
    metadata: dict[str, Any],
    source_path: Path,
    output_dir: Path,
    repo_root: Path,
    counts: Counter[str],
) -> dict[str, Any] | None:
    try:
        source_text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        counts["source_catalog.parquet:read_error"] += 1
        return None
    if _looks_like_non_vyper_source(source_text):
        counts["source_catalog.parquet:non_vyper_source"] += 1
        return None
    source_spec = _vyper_2026_source_spec(metadata, source_text, None)
    if source_spec is None or not is_supported_source_version(source_spec):
        counts["source_catalog.parquet:unsupported_compiler"] += 1
        return None
    digest = _source_hash(source_text)
    target = output_dir / f"{digest}.vy"
    corpus_path = _write_corpus_source(source_text, target, digest, counts)
    item = _vyper_2026_inventory_item(row, metadata, repo_root, source_path)
    item.update(
        {
            "relpath": str(Path("vy") / corpus_path.name),
            "corpus_path": str(corpus_path),
            "corpus_repo_root": str(output_dir),
            "pragma": source_spec,
            "source_pragma": infer_pragma(source_text),
            "source_compiler": compiler_version_for_spec(source_spec),
            "sha256": digest,
        }
    )
    return item


def _import_vyper_2026_inventory_standard_json(
    row: dict[str, Any],
    metadata: dict[str, Any],
    source_path: Path,
    package_root: Path,
    repo_root: Path,
    counts: Counter[str],
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        counts["source_catalog.parquet:json_read_error"] += 1
        return []
    if payload.get("language") != "Vyper":
        counts["source_catalog.parquet:non_vyper_json"] += 1
        return []
    compiler_raw = metadata.get("compiler_version") or metadata.get("source_compiler")
    compiler = compiler_version_for_spec(compiler_raw or payload.get("compiler_version"))
    if compiler is None or not is_supported_source_version(compiler):
        counts["source_catalog.parquet:unsupported_compiler"] += 1
        return []
    sources = payload.get("sources")
    if not isinstance(sources, dict) or not sources:
        counts["source_catalog.parquet:json_missing_sources"] += 1
        return []

    written_sources: dict[str, Path] = {}
    for source_name, source_info in sources.items():
        content = _standard_json_source_content(source_info)
        if content is None:
            counts["source_catalog.parquet:json_source_missing_content"] += 1
            continue
        safe_source = Path(*(_safe_filename(part) for part in Path(str(source_name)).parts))
        digest = _source_hash(content)
        corpus_path = _write_corpus_source(content, package_root / safe_source, digest, counts)
        written_sources[str(source_name)] = corpus_path
        counts["source_catalog.parquet:json_sources_written"] += 1

    items: list[dict[str, Any]] = []
    selected_sources = _chainsecurity_output_sources(payload, written_sources)
    compiler_search_paths = _standard_json_compiler_search_paths(payload, package_root)
    for source_name in selected_sources:
        corpus_path = written_sources.get(source_name)
        if corpus_path is None or corpus_path.suffix != ".vy":
            continue
        try:
            source_text = corpus_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            counts["source_catalog.parquet:read_error"] += 1
            continue
        if _looks_like_non_vyper_source(source_text):
            counts["source_catalog.parquet:non_vyper_source"] += 1
            continue
        source_spec = _vyper_2026_source_spec(metadata, source_text, payload)
        if source_spec is None or not is_supported_source_version(source_spec):
            counts["source_catalog.parquet:unsupported_pragma"] += 1
            continue
        item = _vyper_2026_inventory_item(row, metadata, repo_root, source_path)
        item.update(
            {
                "relpath": str(Path("standard_json") / package_root.name / source_name),
                "corpus_path": str(corpus_path),
                "corpus_repo_root": str(package_root),
                "pragma": source_spec,
                "source_pragma": infer_pragma(source_text),
                "source_compiler": compiler_version_for_spec(source_spec),
                "compiler_version": payload.get("compiler_version") or metadata.get("compiler_version"),
                "sha256": _source_hash(source_text),
                "compiler_search_paths": [str(path) for path in compiler_search_paths],
                "standard_json": str(source_path),
            }
        )
        items.append(item)
    return items


def _vyper_2026_inventory_item(
    row: dict[str, Any], metadata: dict[str, Any], repo_root: Path, source_path: Path
) -> dict[str, Any]:
    return {
        "source_id": row.get("source_id"),
        "source_path": str(source_path),
        "repo_root": str(repo_root),
        "repo": "vyper_2026",
        "compiler_version": metadata.get("compiler_version"),
        "contract_name": metadata.get("contract_name"),
        "chain": metadata.get("chain"),
        "address": metadata.get("address"),
        "source_available": True,
        "source_origin": metadata.get("source_origin") or row.get("representative_provider"),
        "source_len": row.get("source_len"),
        "source_format": row.get("source_format"),
        "representative_source_path": row.get("representative_source_path"),
        "catalog_path": row.get("catalog_path"),
    }


def _vyper_2026_source_spec(
    metadata: dict[str, Any], source_text: str, payload: dict[str, Any] | None
) -> str | None:
    return (
        metadata.get("source_compiler")
        or compiler_version_for_spec(metadata.get("compiler_version"))
        or compiler_version_for_spec(payload.get("compiler_version") if payload else None)
        or infer_pragma(source_text)
    )


def _looks_like_non_vyper_source(source: str) -> bool:
    return source.lstrip().startswith("//")


def _resolve_vyper_2026_inventory_path(
    raw_path: object, repo_root: Path, inventory_dir: Path
) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    for base in (repo_root, inventory_dir, inventory_dir.parent):
        candidate = base / path
        if candidate.exists():
            return candidate
    return repo_root / path


def import_chainsecurity(source: Path, output: Path) -> dict[str, Any]:
    root = source.expanduser()
    export_dir = root / "export" if (root / "export").exists() else root
    contracts_dir = output / "contracts"
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    json_stems: set[str] = set()

    for metadata_path in sorted(export_dir.glob("*.json")):
        counts["json_seen"] += 1
        json_stems.add(metadata_path.stem)
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            counts["json_read_error"] += 1
            continue
        if payload.get("language") != "Vyper":
            counts["non_vyper_json"] += 1
            continue

        compiler_raw = payload.get("compiler_version")
        compiler = compiler_version_for_spec(compiler_raw)
        if compiler is None or not is_supported_source_version(compiler):
            counts["unsupported_compiler"] += 1
            continue

        sources = payload.get("sources")
        if not isinstance(sources, dict) or not sources:
            counts["json_missing_sources"] += 1
            continue

        chain, address = _chainsecurity_id(metadata_path)
        package_id = f"{chain}_{address}" if chain and address else metadata_path.stem
        corpus_repo = "chainsecurity"
        package_root = contracts_dir / corpus_repo / package_id
        written_sources: dict[str, Path] = {}
        for source_name, source_info in sources.items():
            content = _standard_json_source_content(source_info)
            if content is None:
                counts["json_source_missing_content"] += 1
                continue
            safe_source = Path(*(_safe_filename(part) for part in Path(str(source_name)).parts))
            relpath = Path(package_id) / safe_source
            digest = _source_hash(content)
            corpus_path = _write_corpus_source(
                content, contracts_dir / corpus_repo / relpath, digest, counts
            )
            written_sources[str(source_name)] = corpus_path
            counts["json_sources_written"] += 1

        selected_sources = _chainsecurity_output_sources(payload, written_sources)
        compiler_search_paths = _standard_json_compiler_search_paths(payload, package_root)
        for source_name in selected_sources:
            corpus_path = written_sources.get(source_name)
            if corpus_path is None or corpus_path.suffix != ".vy":
                continue
            try:
                source_text = corpus_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                counts["read_error"] += 1
                continue
            source_spec = compiler or infer_pragma(source_text)
            if not is_supported_source_version(source_spec):
                counts["unsupported_pragma"] += 1
                continue
            item = {
                "source_path": str(metadata_path),
                "repo_root": str(root),
                "repo": corpus_repo,
                "relpath": str(Path(package_id) / source_name),
                "corpus_path": str(corpus_path),
                "corpus_repo_root": str(package_root),
                "pragma": source_spec,
                "source_pragma": infer_pragma(source_text),
                "source_compiler": compiler_version_for_spec(source_spec),
                "compiler_version": compiler_raw,
                "sha256": _source_hash(source_text),
                "chain": chain,
                "address": address,
                "compiler_search_paths": [str(path) for path in compiler_search_paths],
                "standard_json": str(metadata_path),
            }
            items.append(item)
            counts["applicable"] += 1
            by_repo[corpus_repo] += 1
            if item["source_compiler"] is not None:
                by_compiler[item["source_compiler"]] += 1

    for source_path in sorted(export_dir.glob("*.vy")):
        counts["flat_seen"] += 1
        if source_path.stem in json_stems:
            counts["flat_with_json_companion"] += 1
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            counts["read_error"] += 1
            continue
        source_spec = infer_pragma(source_text)
        if source_spec is None:
            counts["missing_pragma"] += 1
            continue
        if not is_supported_source_version(source_spec):
            counts["unsupported_pragma"] += 1
            continue
        chain, address = _chainsecurity_id(source_path)
        corpus_repo = "chainsecurity_flat"
        relpath = Path(source_path.name)
        digest = _source_hash(source_text)
        corpus_path = _write_corpus_source(
            source_text, contracts_dir / corpus_repo / relpath, digest, counts
        )
        compiler = compiler_version_for_spec(source_spec)
        item = {
            "source_path": str(source_path),
            "repo_root": str(root),
            "repo": corpus_repo,
            "relpath": str(relpath),
            "corpus_path": str(corpus_path),
            "corpus_repo_root": str(contracts_dir / corpus_repo),
            "pragma": source_spec,
            "source_compiler": compiler,
            "sha256": digest,
            "chain": chain,
            "address": address,
        }
        items.append(item)
        counts["applicable"] += 1
        by_repo[corpus_repo] += 1
        if compiler is not None:
            by_compiler[compiler] += 1

    manifest_path = output / "chainsecurity-manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": [str(root)],
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def dedupe_manifests(manifest_paths: list[Path] | None, output_path: Path) -> dict[str, Any]:
    if manifest_paths is None:
        corpus_root = output_path.parent
        manifest_paths = [
            corpus_root / name
            for name in (
                "manifest.json",
                "codeslaw-manifest.json",
                "codeslaw-buckets-manifest.json",
                "old-vyper-bug-manifest.json",
                "smart-contract-fiesta-manifest.json",
                "vyper-2026-manifest.json",
            )
            if (corpus_root / name).exists()
        ]

    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    roots: list[str] = []
    by_sha: dict[str, dict[str, Any]] = {}

    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        roots.append(str(manifest_path))
        for item in manifest.get("items", []):
            counts["items_seen"] += 1
            corpus_path = item.get("corpus_path")
            if not corpus_path or not Path(corpus_path).exists():
                counts["missing_corpus_path"] += 1
                continue
            digest = item.get("sha256")
            if not digest:
                counts["missing_sha256"] += 1
                continue
            repaired = _repair_manifest_item_path(item, digest, counts)
            if repaired is None:
                continue
            existing = by_sha.get(digest)
            if existing is not None:
                counts["duplicates"] += 1
                existing["duplicate_count"] = existing.get("duplicate_count", 0) + 1
                sources = existing.setdefault("duplicate_sources", [])
                if len(sources) < 20:
                    sources.append(_duplicate_source(repaired, manifest_path))
                continue
            kept = dict(repaired)
            kept["source_manifest"] = str(manifest_path)
            by_sha[digest] = kept
            counts["deduped"] += 1

    items = list(by_sha.values())
    for item in items:
        by_repo[item.get("repo") or "unknown"] += 1
        compiler = item.get("source_compiler")
        if compiler:
            by_compiler[compiler] += 1

    manifest = {
        "manifest": str(output_path),
        "roots": roots,
        "output": str(output_path.parent),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "items": items,
    }
    return _write_manifest(output_path, manifest)


def fetch_codeslaw(chain: str, query: str, sort: str, limit: int, output: Path) -> dict[str, Any]:
    search_query = _codeslaw_chain_query(chain, query)
    search_url = "https://www.codeslaw.app/api/search?" + urllib.parse.urlencode(
        {"q": search_query, "sort": sort}
    )
    search = _fetch_json(search_url)
    matches = search.get("result", {}).get("FileMatches", [])[:limit]
    contracts_dir = output / "contracts"
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    seen_addresses: set[tuple[str, str]] = set()

    _collect_codeslaw_matches(
        matches, contracts_dir, counts, by_repo, by_compiler, items, seen_addresses
    )

    manifest_path = output / "codeslaw-manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": [search_url],
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def fetch_codeslaw_buckets(chains: tuple[str, ...], output: Path) -> dict[str, Any]:
    contracts_dir = output / "contracts"
    counts: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    by_compiler: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    seen_addresses: set[tuple[str, str]] = set()
    roots: list[str] = []
    bucket_stats: list[dict[str, Any]] = []
    capped_buckets: list[dict[str, Any]] = []

    for chain in chains:
        for version in KNOWN_VERSIONS:
            escaped = str(version).replace(".", r"\.")
            query = _codeslaw_chain_query(
                chain,
                rf'lang:vyper regex:"(?:@version|pragma version)[^\n]*{escaped}"',
            )
            search_url = "https://www.codeslaw.app/api/search?" + urllib.parse.urlencode(
                {"q": query}
            )
            roots.append(search_url)
            result = _fetch_json(search_url).get("result", {})
            matches = result.get("FileMatches", []) or []
            stats = result.get("Stats", {}) or {}
            file_count = int(stats.get("FileCount") or 0)
            bucket = {
                "chain": chain,
                "version": str(version),
                "file_count": file_count,
                "match_count": int(stats.get("MatchCount") or 0),
                "returned": len(matches),
                "query": query,
            }
            if file_count or matches:
                bucket_stats.append(bucket)
            if file_count > len(matches):
                capped_buckets.append(bucket)
                counts["capped_buckets"] += 1
            _collect_codeslaw_matches(
                matches, contracts_dir, counts, by_repo, by_compiler, items, seen_addresses
            )

    manifest_path = output / "codeslaw-buckets-manifest.json"
    manifest = {
        "manifest": str(manifest_path),
        "roots": roots,
        "output": str(output),
        "counts": dict(counts),
        "by_repo": by_repo.most_common(),
        "by_source_compiler": by_compiler.most_common(),
        "bucket_stats": bucket_stats,
        "capped_buckets": capped_buckets,
        "items": items,
    }
    return _write_manifest(manifest_path, manifest)


def _collect_codeslaw_matches(
    matches: list[dict[str, Any]],
    contracts_dir: Path,
    counts: Counter[str],
    by_repo: Counter[str],
    by_compiler: Counter[str],
    items: list[dict[str, Any]],
    seen_addresses: set[tuple[str, str]],
) -> None:
    for match in matches:
        contract = match.get("Contract") or {}
        match_chain = contract.get("chain") or match.get("Chain")
        address = (contract.get("address") or "").lower()
        if not match_chain or not address or (match_chain, address) in seen_addresses:
            continue
        seen_addresses.add((match_chain, address))
        counts["contracts_seen"] += 1
        detail_url = "https://www.codeslaw.app/api/contracts?" + urllib.parse.urlencode(
            {"chain": match_chain, "address": address}
        )
        detail = _fetch_json(detail_url)
        for contract_detail in detail.get("contracts", []):
            for index, file in enumerate(contract_detail.get("files") or []):
                filename = file.get("filename") or f"{address}_{index}.vy"
                code = file.get("code")
                if not isinstance(code, str) or not filename.endswith(".vy"):
                    counts["non_vyper_file"] += 1
                    continue
                pragma = infer_pragma(code)
                if pragma is None:
                    counts["missing_pragma"] += 1
                    continue
                if not is_supported_source_version(pragma):
                    counts["unsupported_pragma"] += 1
                    continue
                digest = _source_hash(code)
                corpus_repo = f"codeslaw__{match_chain}"
                relpath = Path(address) / _safe_filename(filename)
                corpus_path = contracts_dir / corpus_repo / relpath
                corpus_path = _write_corpus_source(code, corpus_path, digest, counts)
                compiler = compiler_version_for_spec(pragma)
                items.append(
                    {
                        "source_path": detail_url,
                        "repo_root": f"codeslaw://{match_chain}",
                        "repo": corpus_repo,
                        "relpath": str(relpath),
                        "corpus_path": str(corpus_path),
                        "corpus_repo_root": str(contracts_dir / corpus_repo),
                        "pragma": pragma,
                        "source_compiler": compiler,
                        "sha256": digest,
                        "address": address,
                        "chain": match_chain,
                        "contract_name": contract_detail.get("name"),
                        "label": contract_detail.get("label"),
                        "codeslaw_url": f"https://www.codeslaw.app/contracts/{match_chain}/{address}",
                    }
                )
                counts["applicable"] += 1
                by_repo[corpus_repo] += 1
                if compiler is not None:
                    by_compiler[compiler] += 1


def smoke_corpus(
    manifest_path: Path,
    output_path: Path,
    target_version: str,
    workers: int,
    limit: int,
    paths: list[Path] | None = None,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = _smoke_items(manifest["items"], paths, limit)
    started = time.time()
    results: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_smoke_one, item, target_version) for item in items]
        for index, future in enumerate(cf.as_completed(futures), 1):
            results.append(future.result())
            if index % 50 == 0:
                output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
                print(f"progress {index}/{len(items)}", flush=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    summary = _smoke_summary(results, manifest_path, output_path, time.time() - started)
    _summary_path(output_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _smoke_items(
    manifest_items: list[dict[str, Any]], paths: list[Path] | None, limit: int
) -> list[dict[str, Any]]:
    if paths:
        wanted = {str(path) for path in paths}
        wanted.update(str(path.resolve()) for path in paths if path.exists())
        return [item for item in manifest_items if item.get("corpus_path") in wanted]
    return manifest_items[: limit or None]


def _smoke_one(item: dict[str, Any], target_version: str) -> dict[str, Any]:
    path = Path(item["corpus_path"])
    started = time.time()
    try:
        original = path.read_text(encoding="utf-8")
        compiler_search_paths = _item_compiler_search_paths(item)
        source_version = _item_source_version(item)
        config = Config(
            paths=(path,),
            target_version=target_version,
            source_version=source_version,
            compiler_search_paths=compiler_search_paths,
        )
        source_compile = compile_source_file(path, config, source_version)
        source_version, config, source_compile = _retry_source_compile_with_newer_version(
            item, path, config, source_version, source_compile
        )
        source_ast = source_compile.artifacts.get("ast") if source_compile.artifacts else None
        file_config = replace(
            config, source_ast=source_ast if isinstance(source_ast, dict) else None
        )
        rewrite = apply_rules(original, file_config, path)
        with target_overlay(
            {path: rewrite.source}, config.target_version, config.compiler_search_paths
        ) as overlay:
            target_compile = compile_target_source(path, rewrite.source, config, overlay)
        abi_equal, method_ids_equal, storage_layout_equal = compare_artifacts(
            source_compile, target_compile
        )
        artifact_details = _artifact_detail_fields(
            source_compile,
            target_compile,
            abi_equal,
            method_ids_equal,
            storage_layout_equal,
        )
        return {
            **item,
            "changed": original != rewrite.source,
            "fixes": [fix.rule for fix in rewrite.fixes],
            "diagnostics": [diag.rule for diag in rewrite.diagnostics],
            "source_compile": source_compile.status,
            "target_compile": target_compile.status,
            "source_error": _error_excerpt(source_compile.stderr),
            "target_error": _error_excerpt(target_compile.stderr),
            "abi_equal": abi_equal,
            "method_ids_equal": method_ids_equal,
            "storage_layout_equal": storage_layout_equal,
            **artifact_details,
            "seconds": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            **item,
            "source_compile": "exception",
            "target_compile": "exception",
            "source_error": f"{type(exc).__name__}: {exc}",
            "target_error": traceback.format_exc()[-2000:],
            "seconds": round(time.time() - started, 3),
        }


def _artifact_detail_fields(
    source_compile,
    target_compile,
    abi_equal: bool | None,
    method_ids_equal: bool | None,
    storage_layout_equal: bool | None,
) -> dict[str, list[str]]:
    requested = {
        "abi_diff": abi_equal is False,
        "method_id_diff": method_ids_equal is False,
        "storage_layout_diff": storage_layout_equal is False,
    }
    if not any(requested.values()):
        return {}
    abi_diff, method_id_diff, storage_layout_diff = compare_artifact_details(
        source_compile, target_compile
    )
    details = {
        "abi_diff": abi_diff,
        "method_id_diff": method_id_diff,
        "storage_layout_diff": storage_layout_diff,
    }
    return {field: details[field] for field, include in requested.items() if include}


def _smoke_summary(
    results: list[dict[str, Any]], manifest_path: Path, output_path: Path, elapsed: float
) -> dict[str, Any]:
    status_pairs = Counter((item["source_compile"], item["target_compile"]) for item in results)
    failed_repos = Counter(
        item["repo"]
        for item in results
        if item["source_compile"] != "passed" or item["target_compile"] != "passed"
    )
    source_errors = Counter(
        item.get("source_error")
        for item in results
        if item.get("source_error") and item["source_compile"] != "passed"
    )
    target_errors = Counter(
        item.get("target_error")
        for item in results
        if item.get("target_error") and item["target_compile"] != "passed"
    )
    failed_compilers = Counter(
        item.get("source_compiler") or item.get("pragma") or "unknown"
        for item in results
        if item["source_compile"] != "passed" or item["target_compile"] != "passed"
    )
    fixes = Counter(fix for item in results for fix in item.get("fixes", []))
    diagnostics = Counter(diag for item in results for diag in item.get("diagnostics", []))
    return {
        "manifest": str(manifest_path),
        "results": str(output_path),
        "total": len(results),
        "elapsed_seconds": round(elapsed, 2),
        "status_pairs": {
            f"{source}->{target}": count for (source, target), count in status_pairs.most_common()
        },
        "failed_repos": failed_repos.most_common(20),
        "changed": sum(1 for item in results if item.get("changed")),
        "abi_changed": sum(1 for item in results if item.get("abi_equal") is False),
        "method_ids_changed": sum(1 for item in results if item.get("method_ids_equal") is False),
        "storage_layout_changed": sum(
            1 for item in results if item.get("storage_layout_equal") is False
        ),
        "failed_compilers": failed_compilers.most_common(20),
        "top_source_errors": source_errors.most_common(20),
        "top_target_errors": target_errors.most_common(20),
        "top_fixes": fixes.most_common(20),
        "top_diagnostics": diagnostics.most_common(20),
    }


def _build_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "manifest": manifest["manifest"],
        "counts": manifest["counts"],
        "repos": len(manifest["by_repo"]),
        "top_repos": manifest["by_repo"][:20],
        "source_compilers": manifest["by_source_compiler"],
    }
    if "by_metadata_compiler" in manifest:
        summary["metadata_compilers"] = manifest["by_metadata_compiler"]
    return summary


def _summary_path(results_path: Path) -> Path:
    if results_path.name.endswith("-results.json"):
        return results_path.with_name(f"{results_path.name[: -len('-results.json')]}-summary.json")
    return results_path.with_name(f"{results_path.stem}-summary.json")


def _source_hash(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def _write_manifest(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _write_corpus_source(
    source: str, preferred_path: Path, digest: str, counts: Counter[str]
) -> Path:
    target = _collision_safe_path(preferred_path, digest)
    if target != preferred_path:
        counts["corpus_path_collisions"] += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def _collision_safe_path(preferred_path: Path, digest: str) -> Path:
    existing_hash = _file_hash(preferred_path)
    if existing_hash is None or existing_hash == digest:
        return preferred_path

    stem = preferred_path.stem
    suffix = preferred_path.suffix
    parent = preferred_path.parent
    short = digest[:8]
    candidate = parent / f"{stem}.{short}{suffix}"
    index = 2
    while True:
        candidate_hash = _file_hash(candidate)
        if candidate_hash is None or candidate_hash == digest:
            return candidate
        candidate = parent / f"{stem}.{short}.{index}{suffix}"
        index += 1


def _repair_manifest_item_path(
    item: dict[str, Any], digest: str, counts: Counter[str]
) -> dict[str, Any] | None:
    corpus_path = Path(str(item["corpus_path"]))
    if _file_hash(corpus_path) == digest:
        return item

    counts["corpus_path_hash_mismatch"] += 1
    source_path_raw = item.get("source_path")
    if not isinstance(source_path_raw, str) or "://" in source_path_raw:
        counts["unrepairable_corpus_path"] += 1
        return None

    source_path = Path(source_path_raw).expanduser()
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        counts["unrepairable_corpus_path"] += 1
        return None
    if _source_hash(source) != digest:
        counts["unrepairable_corpus_path"] += 1
        return None

    repaired_path = _write_corpus_source(source, corpus_path, digest, counts)
    repaired = dict(item)
    repaired["corpus_path"] = str(repaired_path)
    return repaired


def _error_excerpt(text: str | None) -> str | None:
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    return "\n".join(lines[:8])[:2000]


def _is_excluded(path: Path) -> bool:
    if set(path.parts) & EXCLUDED_PARTS:
        return True
    return any(_has_marker(path, marker) for marker in FIXTURE_MARKERS)


def _has_marker(path: Path, marker: tuple[str, ...]) -> bool:
    parts = path.parts
    width = len(marker)
    return any(
        tuple(parts[index : index + width]) == marker for index in range(len(parts) - width + 1)
    )


def _git_root(path: Path) -> Path:
    current = path.parent
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return path.parent


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "vyupgrade-corpus/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def _codeslaw_chain_query(chain: str, query: str) -> str:
    if "chain:" in query:
        return query
    return f"chain:{chain} {query}"


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "contract.vy"


def _fiesta_source_files(contract_dir: Path) -> list[tuple[str, str, Path]]:
    source_files: list[tuple[str, str, Path]] = []
    for path in sorted(contract_dir.glob("*.vy")):
        try:
            source_files.append((path.name, path.read_text(encoding="utf-8"), path))
        except (OSError, UnicodeDecodeError):
            continue
    contract_json = contract_dir / "contract.json"
    if contract_json.exists():
        try:
            payload = json.loads(contract_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return source_files
        for name, source in _standard_json_sources(payload):
            source_files.append((name, source, contract_json))
    return source_files


def _standard_json_sources(payload: dict[str, Any]) -> list[tuple[str, str]]:
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return []
    extracted: list[tuple[str, str]] = []
    for name, source_info in sources.items():
        if not isinstance(source_info, dict):
            continue
        content = source_info.get("content")
        if isinstance(content, str) and (
            str(name).endswith(".vy") or infer_pragma(content) is not None
        ):
            extracted.append((str(name), content))
    return extracted


def _standard_json_source_content(source_info: object) -> str | None:
    if not isinstance(source_info, dict):
        return None
    content = source_info.get("content")
    return content if isinstance(content, str) else None


def _item_compiler_search_paths(item: dict[str, Any]) -> tuple[Path, ...]:
    paths: list[Path] = [Path(item["corpus_repo_root"])]
    paths.extend(
        path
        for path in (Path(path) for path in item.get("compiler_search_paths", []) if path)
        if path.exists()
    )
    standard_json = item.get("standard_json")
    if isinstance(standard_json, str):
        try:
            payload = json.loads(Path(standard_json).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            paths.extend(
                _standard_json_compiler_search_paths(payload, Path(item["corpus_repo_root"]))
            )
    return _unique_paths(paths)


def _item_source_version(item: dict[str, Any]) -> str:
    compiler = compiler_version_for_spec(item.get("compiler_version"))
    if compiler is not None and item.get("standard_json"):
        return compiler
    pragma = str(item["pragma"])
    try:
        source = Path(item["corpus_path"]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return pragma
    return compiler_version_for_source(pragma, source) or pragma


def _retry_source_compile_with_newer_version(
    item: dict[str, Any],
    path: Path,
    config: Config,
    source_version: str,
    source_compile: Any,
) -> tuple[str, Config, Any]:
    if source_compile.status == "passed" or _has_exact_source_compiler(item):
        return source_version, config, source_compile
    current = parse_version(source_version)
    candidates = known_versions_satisfying(item.get("pragma"))
    for candidate in candidates:
        if current is not None and candidate <= current:
            continue
        retry_version = str(candidate)
        retry_config = replace(config, source_version=retry_version)
        retry_compile = compile_source_file(path, retry_config, retry_version)
        if retry_compile.status == "passed":
            return retry_version, retry_config, retry_compile
    return source_version, config, source_compile


def _has_exact_source_compiler(item: dict[str, Any]) -> bool:
    return bool(item.get("standard_json")) and compiler_version_for_spec(item.get("compiler_version")) is not None


def _standard_json_compiler_search_paths(payload: dict[str, Any], package_root: Path) -> tuple[Path, ...]:
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return ()
    raw_paths = settings.get("search_paths")
    if not isinstance(raw_paths, list):
        return ()
    paths: list[Path] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            continue
        path = _standard_json_search_path(package_root, raw_path)
        if path is not None and path.exists():
            paths.append(path)
    return _unique_paths(paths)


def _standard_json_search_path(package_root: Path, raw_path: str) -> Path | None:
    path = Path(raw_path)
    parts = [part for part in path.parts if part not in {"", "."}]
    if path.is_absolute() or any(part == ".." for part in parts):
        return None
    if not parts:
        return package_root
    return package_root.joinpath(*(_safe_filename(part) for part in parts))


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    unique: dict[str, Path] = {}
    for path in paths:
        unique.setdefault(str(path), path)
    return tuple(unique.values())


def _chainsecurity_id(path: Path) -> tuple[str | None, str | None]:
    match = re.match(r"(?P<chain>\d+)_(?P<address>0x[a-fA-F0-9]{40})$", path.stem)
    if match is None:
        return None, None
    return match.group("chain"), match.group("address").lower()


def _chainsecurity_output_sources(
    payload: dict[str, Any], written_sources: dict[str, Path]
) -> tuple[str, ...]:
    settings = payload.get("settings", {})
    if not isinstance(settings, dict):
        settings = {}
    compilation_target = settings.get("compilationTarget")
    if isinstance(compilation_target, dict):
        selected = [
            source_name
            for source_name in compilation_target
            if source_name in written_sources and written_sources[source_name].suffix == ".vy"
        ]
        if selected:
            return tuple(dict.fromkeys(selected))

    output_selection = settings.get("outputSelection")
    if not isinstance(output_selection, dict):
        return tuple(name for name, path in written_sources.items() if path.suffix == ".vy")
    selected: list[str] = []
    for source_name in output_selection:
        if source_name == "*":
            return tuple(name for name, path in written_sources.items() if path.suffix == ".vy")
        if source_name in written_sources:
            selected.append(source_name)
    return tuple(dict.fromkeys(selected))


def _duplicate_source(item: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    return {
        "manifest": str(manifest_path),
        "repo": item.get("repo"),
        "relpath": item.get("relpath"),
        "source_path": item.get("source_path"),
        "chain": item.get("chain"),
        "address": item.get("address"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
