# AGENTS.md

Instructions for AI coding agents working on `vyupgrade`.

## Project purpose

`vyupgrade` is a compiler-backed codemod for Vyper contracts. It rewrites legacy
Vyper source to a requested target compiler, then validates the result by
compiling the original and migrated source and comparing ABI, method identifiers,
and storage layout. Treat the compiler-backed validation path as a core safety
feature, not an optional smoke check.

## Repository map

- `src/vyupgrade/cli.py` — command-line orchestration, config loading, report
  generation, write/diff/check behavior, and validation diagnostics.
- `src/vyupgrade/rules.py` — ordered rule pipeline. New rule groups must be
  imported here and inserted at the correct stage.
- `src/vyupgrade/rule_registry.py` — rule descriptors and version gating.
- `src/vyupgrade/rule_groups/` — migration rule implementations, grouped by
  subject area.
- `src/vyupgrade/versions.py` — supported Vyper versions, pragma/spec parsing,
  source/target crossing logic, and default EVM version mapping.
- `src/vyupgrade/compiler.py` — compiler subprocess setup, temporary target
  overlays, artifact canonicalization, and ABI/method/storage comparisons.
- `src/vyupgrade/analysis.py` and `src/vyupgrade/ast_facts.py` — lightweight
  source facts and compiler AST helpers for type-aware rewrites.
- `src/vyupgrade/source.py` and `src/vyupgrade/rule_helpers.py` — edit, masking,
  parsing, insertion, and replacement helpers shared by rules.
- `docs/vyper-syntax-history.md` — source-visible Vyper syntax history used as
  source material for migrations.
- `docs/migration-coverage.md` — mapping from each syntax-history entry to an
  automated rule, diagnostic, explicit no-op, or validation-only behavior.
- [`DEVELOPMENT.md`](DEVELOPMENT.md) — maintainer setup, architecture,
  rule-writing, corpus, and release workflow reference.
- `scripts/corpus.py` — corpus import, dedupe, and compiler-backed smoke tooling.
- `scripts/smoke-wheel.sh` — package smoke test used by CI and publish workflows.

## Commands to run

Use the locked environment unless intentionally updating dependencies.

```bash
uv run --locked ruff check src tests scripts/corpus.py scripts/release_notes.py scripts/release_preflight.py
uv run --locked pytest
scripts/smoke-wheel.sh
```

For a targeted change, run the matching focused tests first, then the full suite.
Useful focused commands:

```bash
uv run --locked pytest tests/rule_groups/test_<area>.py
uv run --locked pytest tests/test_versions.py tests/test_docs.py tests/test_rule_registry.py
uv run --locked pytest tests/test_cli.py tests/test_compiler.py
```

`tests/test_cli_integration.py` invokes real Vyper compilers and can be slower
than the default unit tests. Run it when changing compiler selection, overlays,
validation outputs, target pragmas, or alpha/final target support.

## Migration-rule invariants

- Prefer a `Fix` only when the rewrite is local, deterministic, and intended to
  preserve public behavior. Use a `Diagnostic` for ambiguous or behavior-changing
  cases.
- Every emitted code must be version-gated by a `Rule(..., changes=(...))`
  descriptor that is part of `RULES` in `src/vyupgrade/rules.py`.
- Fix codes use `VY###`; diagnostic codes use `VYD###`. Do not reuse a code.
- Rules must be idempotent: running `apply_rules()` twice should not make a
  second change.
- Rewrites must avoid comments, string literals, and docstrings unless the rule
  intentionally targets documentation syntax. Use `context.code_mask`,
  `span_is_code()`, and helpers from `source.py`/`rule_helpers.py`.
- For multiple edits, use `TextEdit` plus `apply_edits()` and avoid overlapping
  spans. When nested matches are possible, prefer the existing
  `innermost_non_overlapping()` helper.
- Preserve imports, pragmas, formatting, and user comments where practical.
- Keep compiler validation meaningful. Do not silence ABI, method identifier, or
  storage layout differences unless there is an existing canonicalization reason
  in `compiler.py` and tests cover it.

## Version gating quick reference

Rule activation lives in `src/vyupgrade/rule_registry.py`:

- `crossing("VY###", "0.x.y")` — normal historical migration. It runs when the
  source floor is older than the introduced version and the target is at or after
  it.
- `target_floor("VY###", "0.x.y")` — target-capability rule. It runs whenever
  the requested target is at or after the version, even if the source floor is
  already at or after that version. Use for legacy syntax cleanups that may be
  present under broad, missing, or beta-era pragmas.
- `target_update("VY###", "0.x.y")` — currently gated like `target_floor`, but
  used to signal intentional target-directed updates such as pragma rewriting.

`select` and `ignore` are rule-code based. A descriptor with several codes runs
when any descriptor code is enabled, but emitted fixes/diagnostics are filtered
again before returning.

## Adding support for a new Vyper release

1. Read the upstream Vyper release notes and linked PRs. Only track
   source-visible syntax or spelling changes: decorators, keywords,
   declarations, type names, builtin names/signatures, call syntax, imports,
   pragmas, literals, and newly accepted source forms.
2. Update `docs/vyper-syntax-history.md` with a new heading and concise
   before/after examples. Exclude backend-only, optimizer-only, ABI-layout-only,
   CLI-only, and pure runtime semantic changes unless source text must change.
3. Update `src/vyupgrade/versions.py`:
   - add prereleases to `ALPHA_RELEASE_VERSIONS` when they remain opt-in;
   - extend the `(minor, last_patch)` ranges in `KNOWN_VERSIONS` and
     `SUPPORTED_RELEASE_VERSIONS` for new final releases;
   - adjust `default_evm_version()` if Vyper changed its default EVM version;
   - update source syntax floors in `_source_syntax_floor()` when broad pragmas
     need a newer compiler to parse newly introduced syntax.
4. Decide whether the default target should move. If it moves, update all
   defaults and examples together: `Config.target_version`, `cli.py`, README,
   docs, tests, and any pyproject examples.
5. Classify every syntax-history entry in `docs/migration-coverage.md` as one of
   automated rewrite, diagnostic, no-op, or validation-only. This file may not
   use markdown tables, may not contain TODO-style gaps, and must mention every
   version-gated rule code.
6. Implement automated rules in the closest existing `src/vyupgrade/rule_groups/`
   module, or create a new focused module only when the domain does not fit. Add
   the module to `src/vyupgrade/rules.py` in an order that preserves assumptions
   of later rules.
7. Add tests before considering the migration complete. Cover the before/after
   rewrite, diagnostics for unsafe cases, comments/strings/docstrings, version
   gating, idempotence, and compiler validation when relevant.
8. Run the focused tests, then the full validation commands. For broad release
   support, also run a corpus smoke with `scripts/corpus.py smoke` against a
   representative manifest.
9. Update `README.md`, `CHANGELOG.md`, and release notes when changing supported
   versions or user-facing behavior.

## Rule implementation checklist

- Use `RuleContext.facts` for lightweight source facts such as interfaces,
  structs, storage variables, function decorators, loop variables, and return
  types.
- Use `config.source_ast` plus helpers in `ast_facts.py` when compiler spans or
  parsed constants are needed. `cli._prepare_rewrites()` already populates the
  AST from source compilation when available.
- Prefer local helpers over hand-rolled parsing when splitting arguments,
  inserting imports, finding matching delimiters, or computing line numbers.
- If a rewrite depends on mutability or return types of external calls, update
  built-in interface facts in `vyper_builtins.py` and add tests that prove the
  call becomes `staticcall` or `extcall` correctly.
- If a rewrite introduces imports, use `insert_import()` and ensure duplicate
  imports are not produced.
- If behavior may change, emit a diagnostic and document the manual review path.
  Aggressive behavior-changing rewrites should be guarded by `config.aggressive`.

## Documentation and tests that intentionally fail when docs drift

The test suite enforces several maintenance contracts:

- `tests/test_rule_registry.py` checks that rule codes used in source are present
  in `RULE_CHANGES`.
- `tests/test_docs.py` checks that `migration-coverage.md` references all
  version-gated rules, tracks all version headings from syntax history, contains
  no tables, and has no unresolved TODO/gap language.
- `tests/test_versions.py` checks supported versions, spec resolution, syntax
  floors, patch-level crossings, and default EVM mappings.

When these fail after adding a version, update the docs or version map rather
than relaxing the tests.

## Release notes and publishing

`CHANGELOG.md` is the source for GitHub release notes. `scripts/release_notes.py`
extracts the section matching the pushed tag. Publishing is handled by
`.github/workflows/publish.yml` on `v*` tags using PyPI Trusted Publishing after
lint, tests, and `scripts/smoke-wheel.sh` pass.

Before tagging a release, update `pyproject.toml` version and `CHANGELOG.md`, run
all validation commands, then tag with a `v` prefix such as `v0.4.2`.
