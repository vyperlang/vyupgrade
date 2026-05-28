# vyupgrade

A compiler-backed codemod tool for upgrading Vyper contracts across language versions.

The supported migration model covers Vyper syntax changes from `0.2.1` through
`0.4.3`, with version-gated rules so patch-level changes only apply when the
source and target range crosses the relevant compiler version.

```bash
vyupgrade contracts/ --target-version 0.4.3 --diff
vyupgrade contracts/ --target-version 0.4.3 --write --report-json vyupgrade-report.json
```

Use `--bump-pragma` when you want the migration output to compile against the
target compiler instead of preserving the original pragma range.

The current rule set includes source-preserving rewrites and diagnostics for
legacy 0.2.x syntax, 0.3.x patch changes, and 0.4.x migrations: pragma spelling,
decorator renames, `@deploy`, ABI builtin renames, built-in interface imports,
external call keywords, integer `//`, struct keyword arguments, typed loops,
single-name `@nonreentrant`, `sqrt`, bitwise builtins, legacy constants, and
manual-review diagnostics where source intent cannot be proved safely.

See [docs/vyper-syntax-history.md](docs/vyper-syntax-history.md) for the
versioned Vyper syntax history from `0.4.3` through `0.2.1`, with PR links and
before/after examples. See
[docs/migration-coverage.md](docs/migration-coverage.md) for the rule-level
coverage map.

For the local Yearn smoke contracts:

```bash
sh scripts/smoke-yearn.sh
```

For a broader local corpus, build an ignored checkout-local corpus and run the
compiler-backed smoke over its manifest:

```bash
uv run scripts/corpus.py build
uv run scripts/corpus.py smoke --workers 8
```

The builder copies supported Vyper sources from `~/dev` and `~/yearn` into
`corpus/vyper/contracts/`, writes `corpus/vyper/manifest.json`, and keeps the
corpus out of git. Codeslaw can also seed deployed verified contracts. The
Codeslaw search endpoint currently returns at most 100 results per query and
does not expose pagination, so `codeslaw` is a top-results sample:

```bash
uv run scripts/corpus.py codeslaw --limit 100
uv run scripts/corpus.py smoke --manifest corpus/vyper/codeslaw-manifest.json --output corpus/vyper/codeslaw-smoke-results.json
```

For broader Codeslaw coverage, use chain/version buckets and review
`capped_buckets` in the manifest for partitions that still hit the 100-result
search cap:

```bash
uv run scripts/corpus.py codeslaw-buckets
uv run scripts/corpus.py smoke --manifest corpus/vyper/codeslaw-buckets-manifest.json --output corpus/vyper/codeslaw-buckets-smoke-results.json
```

The 2023 Etherscan Vyper reentrancy corpus can be imported from the local
`~/yearn/old-vyper-bug` checkout. This uses its explorer CSV exports for source
compiler attribution, so contracts without a clean in-source pragma still carry
the original version:

```bash
uv run scripts/corpus.py old-vyper-bug
uv run scripts/corpus.py smoke --manifest corpus/vyper/old-vyper-bug-manifest.json --output corpus/vyper/old-vyper-bug-smoke-results.json
```

Smart Contract Fiesta and the local `vyper-2026` workspace can be folded into
the same ignored corpus. Fiesta contributes source files from verified Vyper
metadata. The 2026 workspace contributes source-hash/version provenance when
the Parquet tables do not include local raw source paths:

```bash
uv run scripts/corpus.py smart-contract-fiesta
uv run scripts/corpus.py vyper-2026
uv run scripts/corpus.py dedupe
uv run scripts/corpus.py smoke --manifest corpus/vyper/deduped-manifest.json --output corpus/vyper/deduped-smoke-results.json --workers 8
```

Compiler subprocesses run through the packaged `uv` executable discovered with
`uv.find_uv_bin()`, using `uv run --no-project --python ... --with
vyper==...` by default so older source compilers do not inherit an incompatible
project interpreter.
Override with `--source-python`, `--target-python`, `--source-vyper`, or
`--target-vyper` when needed.
