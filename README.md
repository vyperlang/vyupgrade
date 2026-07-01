# vyupgrade

A compiler-backed tool for upgrading Vyper contracts across language
versions. It rewrites legacy syntax to a chosen target compiler, then proves the
rewrite is safe by compiling the source and the result and comparing their ABI,
method identifiers, and storage layout.

It covers installable Vyper `0.1.0b*` prereleases through `0.4.3`, plus opt-in
`0.5.0a1` and `0.5.0a2` alpha targets. Rules are version-gated: a given rewrite
only fires when the migration from the source version to the target version
actually crosses the compiler release that introduced the change.

## Install

Run it once without installing:

```bash
uvx vyupgrade contracts/
```

Or install it as a tool:

```bash
uv tool install vyupgrade
vyupgrade contracts/
```

## Usage

Preview the changes as a unified diff:

```bash
vyupgrade contracts/ --diff
```

Apply them in place and write a machine-readable report:

```bash
vyupgrade contracts/ --write --report-json vyupgrade-report.json
```

Fail without writing when files would change, for use in CI:

```bash
vyupgrade contracts/ --check
```

The target defaults to `0.4.3`; pass `--target-version` to migrate to a
different release, including an explicit alpha target such as `0.5.0a2`.

Paths may be files or directories; directories are searched recursively for
`.vy` and `.vyi` sources. The source version is inferred per file from its
`#pragma version` (or legacy `# @version`) line. Pass `--source-version` to
override the inference for files that have no pragma.

For broad source pragmas, rule gating uses the oldest satisfying compiler so
historical migrations still run, while source validation uses the newest
satisfying compiler no newer than the requested target.

### How it validates

For each file, `vyupgrade` compiles the original under its source compiler and
the rewritten output under the target compiler, then compares the two
artifacts. A migration is only written back when every file still compiles
under the target. Differences in ABI, method identifiers, or storage layout are
surfaced as diagnostics rather than silently accepted.

Compiler subprocesses run through the bundled `uv`, using
`uv run --no-project --with vyper==<version>` so each side gets the exact
compiler it needs instead of inheriting an incompatible interpreter. When a file
belongs to another project, the nearest `pyproject.toml` is read and any
declared packages matching its Vyper imports (such as `snekmate`) are added to
the compiler environment.

For `0.1.0b*` source compilers, `vyupgrade` runs the compiler through a
`typed-ast` compatibility wrapper so the legacy compiler sees pre-Python-3.8
AST node classes without requiring a local Python 3.6 or 3.7 interpreter. When
an old compiler cannot produce a modern validation output format, that format is
dropped for source validation instead of blocking a compile check that the
compiler never supported.

Dependency inference is intentionally conservative. Exact requirements,
ordinary version ranges, and Git dependencies are supported. Project-specific
specifier syntaxes that cannot be translated to a compiler environment, such as
Poetry caret requirements, are skipped; use `--compiler-search-paths`,
`--source-vyper`, or `--target-vyper` for unusual layouts.

## Options

- `--target-version` — target Vyper version or spec (default `0.4.3`).
- `--source-version` — override the per-file inferred source version.
- `--diff` — print a unified diff instead of the report.
- `--write` — apply changes in place (only when every file compiles).
- `--check` — exit non-zero if any file would change; write nothing.
- `--aggressive` — enable rewrites that change behavior or are not provably safe (e.g. `enum` → `flag`).
- `--split-interfaces` — move top-level `interface` blocks into sibling `.vyi` files and import them.
- `--select` / `--ignore` — comma-separated rule codes to include or exclude.
- `--report-json PATH` — write a JSON report of fixes, diagnostics, and validation results.
- `--format mamushi` — run `mamushi` over written files to reformat them.
- `--test-command CMD` — run a test command after a successful write and record its result.
- `--enable-decimals` — treat decimals as enabled when reasoning about `0.4.x` rules.
- `--source-vyper` / `--target-vyper` — pin the exact compiler version for each side.
- `--source-python` / `--target-python` — pin the Python interpreter for each compiler subprocess.
- `--compiler-search-paths` — extra import search paths for the compiler.
- `--config PATH` — read configuration from a specific `pyproject.toml`.

### Configuration

Defaults can live in `pyproject.toml` under `[tool.vyupgrade]`. Command-line
flags take precedence.

```toml
[tool.vyupgrade]
paths = ["contracts/"]
target-version = "0.4.3"
source-version = "infer"
report-json = "vyupgrade-report.json"
aggressive = false
split-interfaces = false
format = "none"
```

### Exit codes

- `0` — success.
- `1` — `--check` found files that would change.
- `2` — a file failed to compile under the target compiler.
- `3` — a file failed to compile under the source compiler.
- `4` — usage error (no paths, or conflicting flags).
- `5` — an error-severity diagnostic was raised.

## Coverage

Rewrites carry a `VY###` code and diagnostics a `VYD###` code. Where the source
intent cannot be proven safe, the change is reported as a manual-review
diagnostic instead of being applied.

- [docs/migration-coverage.md](docs/migration-coverage.md) — every syntax
  change mapped to a rule, diagnostic, explicit no-op, or validation-only
  behavior.
- [docs/vyper-syntax-history.md](docs/vyper-syntax-history.md) — the versioned
  Vyper syntax history from `0.5.0a2` back through the `0.1.0b*` prereleases,
  with PR links and before/after examples.
- [CHANGELOG.md](CHANGELOG.md) — release notes.
- [DEVELOPMENT.md](DEVELOPMENT.md) — maintainer validation and release workflow.
