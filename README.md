# vyupgrade

A compiler-backed codemod tool for upgrading Vyper contracts across language versions.

The first supported migration path is Vyper `0.3.x` to stable `0.4.3`.

```bash
vyupgrade contracts/ --target-version 0.4.3 --diff
vyupgrade contracts/ --target-version 0.4.3 --write --report-json vyupgrade-report.json
```

Use `--bump-pragma` when you want the migration output to compile against the
target compiler instead of preserving the original pragma range.

The current MVP includes source-preserving rewrites for 0.3.x-to-0.4.3 syntax:
pragma spelling, `@deploy`, ABI builtin renames, built-in interface imports,
external call keywords, integer `//`, struct keyword arguments, typed loops,
single-name `@nonreentrant`, `sqrt`, bitwise builtins, and common 0.3.x legacy
constants such as `MAX_UINT256` and `ZERO_ADDRESS`.

See [docs/vyper-syntax-history.md](docs/vyper-syntax-history.md) for the
versioned Vyper syntax history from `0.4.3` through `0.2.1`, with PR links and
before/after examples.

For the local Yearn smoke contracts:

```bash
sh scripts/smoke-yearn.sh
```

Compiler subprocesses run through `uv run --python 3.11 --with vyper==...` by
default so older source compilers do not inherit an incompatible project
interpreter. Override with `--source-python`, `--target-python`,
`--source-vyper`, or `--target-vyper` when needed.
