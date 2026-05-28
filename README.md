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

Compiler subprocesses run through `uv run --python 3.11 --with vyper==...` by
default so older source compilers do not inherit an incompatible project
interpreter. Override with `--source-python`, `--target-python`,
`--source-vyper`, or `--target-vyper` when needed.
