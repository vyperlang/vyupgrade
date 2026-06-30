# Development

`vyupgrade` is a compiler-backed migration tool for Vyper contracts. Development
work should preserve two guarantees: rewrites are only applied when they are
locally justified, and migrated output is validated against real Vyper compilers
by comparing ABI, method identifiers, and storage layout.

## Setup

Use Python 3.11+ and `uv`.

```bash
uv sync --locked --dev
```

Run commands through `uv run --locked` so development, CI, and release builds use
the same lockfile. Only update `uv.lock` when intentionally changing
requirements in `pyproject.toml`.

## Validation

Run the main validation suite before publishing changes:

```bash
uv run --locked ruff check src tests scripts/corpus.py scripts/release_notes.py
uv run --locked pytest
scripts/smoke-wheel.sh
```

Useful focused checks while iterating:

```bash
uv run --locked pytest tests/rule_groups/test_<area>.py
uv run --locked pytest tests/test_versions.py tests/test_docs.py tests/test_rule_registry.py
uv run --locked pytest tests/test_cli.py tests/test_compiler.py
```

`tests/test_cli_integration.py` uses real compiler subprocesses. Run it when
changing compiler commands, target overlays, output formats, pragma rewriting,
compiler dependency inference, or supported Vyper versions.

## Architecture

The CLI flow is:

1. `cli.main()` loads command-line and `[tool.vyupgrade]` config.
2. `project.discover_files()` finds `.vy` and `.vyi` inputs.
3. `cli._prepare_rewrites()` compiles each source under the inferred or provided
   source compiler, stores the source AST when available, and calls
   `rules.apply_rules()`.
4. `rules.apply_rules()` constructs a `MigrationContext`, then runs the ordered
   rule pipeline from `RULES`.
5. Optional interface splitting generates sibling `.vyi` files when
   `--split-interfaces` is enabled and `VY120` is active.
6. `cli._verify_rewrites()` builds a temporary target overlay, compiles migrated
   sources under the target compiler, and compares ABI, method identifiers, and
   storage layout.
7. The CLI writes diffs, in-place changes, human reports, and JSON reports only
   after validation has completed.

Important files:

- `src/vyupgrade/rules.py` defines rule order and `RULE_CHANGES`.
- `src/vyupgrade/rule_registry.py` defines `Rule`, `RuleContext`, and gating.
- `src/vyupgrade/rule_groups/` contains the actual migration rules.
- `src/vyupgrade/versions.py` owns supported Vyper versions and spec resolution.
- `src/vyupgrade/compiler.py` owns compiler subprocesses, temporary overlays,
  dependency inference, and artifact comparison canonicalization.
- `src/vyupgrade/analysis.py` extracts lightweight source facts for type-aware
  rules.
- `src/vyupgrade/ast_facts.py` extracts facts from compiler AST output.
- `docs/vyper-syntax-history.md` records source-visible upstream syntax changes.
- `docs/migration-coverage.md` records this project's behavior for each change.

## Rule model

A rule runner has this shape:

```python
def _some_rule(context: RuleContext) -> tuple[str, list[Fix], list[Diagnostic]]:
    ...
```

Register it with a descriptor:

```python
Rule("some_rule", runner=_some_rule, changes=(crossing("VY123", "0.4.4"),))
```

Activation helpers:

- `crossing(code, version)` runs when `source_floor < version <= target` and is
  the default for historical syntax migrations.
- `target_floor(code, version)` runs whenever the requested target is at least
  that version. Use it for legacy cleanup rules whose source syntax can still
  appear under broad, unknown, or already-modern pragmas.
- `target_update(code, version)` is currently gated like `target_floor`, but is
  used to communicate target-directed updates such as pragma changes.

Fixes and diagnostics use stable rule codes:

- `Fix("VY###", line, message, before, after)` for automated rewrites.
- `Diagnostic("VYD###", line, message, severity="warning")` for manual review or
  validation findings. Use `severity="error"` only for hard blockers.

All emitted codes must appear in a `Rule(..., changes=...)` descriptor included
in `RULES`. The docs and registry tests intentionally fail when a code is not
version-gated or not documented.

## Writing safe migrations

Prefer small, syntax-preserving edits. A migration should be idempotent and
should not change comments, strings, or docstrings unless that is the point of
the rule.

Use existing helpers:

- `context.code_mask`, `code_mask()`, and `span_is_code()` to skip comments and
  string literals.
- `TextEdit` and `apply_edits()` for multi-edit rewrites.
- `innermost_non_overlapping()` when nested matches may overlap.
- `line_number()` for fix/diagnostic locations.
- `split_top_level_args()` and `split_top_level_arg_spans()` for argument lists.
- `find_matching()` and `find_matching_open()` for balanced delimiters.
- `insert_import()` for new imports.
- `context.facts` for interfaces, structs, global variables, storage variables,
  function decorators, function return types, loop variables, and imported
  built-in interface facts.
- `config.source_ast` and `ast_facts.py` when a rule needs compiler AST spans or
  parsed constants.

Use diagnostics instead of rewrites when the safe target spelling depends on
runtime behavior, external project context, user intent, or a semantic choice.
If an intentionally behavior-changing rewrite is valuable, require
`config.aggressive` and document it clearly in migration coverage.

## Adding support for a new Vyper version

1. **Collect source-visible changes.** Read the upstream Vyper release notes and
   linked PRs. Track changes to source syntax and spelling: pragmas, decorators,
   declarations, imports, interfaces, type names, builtin names or signatures,
   call syntax, literals, and newly accepted forms. Ignore backend-only,
   optimizer-only, ABI-layout-only, EVM-default-only, and CLI-only changes unless
   source text or validation behavior must change.
2. **Update syntax history.** Add a heading to `docs/vyper-syntax-history.md`
   with short before/after examples for each source-visible change.
3. **Update version support.** In `src/vyupgrade/versions.py`, add opt-in alpha
   releases to `ALPHA_RELEASE_VERSIONS` or extend the final-release ranges in
   `KNOWN_VERSIONS` and `SUPPORTED_RELEASE_VERSIONS`. Update
   `default_evm_version()` if Vyper's default EVM changed. Update
   `_source_syntax_floor()` when broad pragmas need a newer compiler to parse a
   newly introduced syntax form.
4. **Decide the default target.** Alpha targets should remain opt-in. When a new
   final release becomes the intended default, update every default and example
   together: `Config.target_version`, `cli.py`, README, docs, tests, and
   configuration snippets.
5. **Classify coverage.** Update `docs/migration-coverage.md` for every new
   syntax-history entry. Each item should say automated rewrite, diagnostic,
   no-op, or validation-only. This document must not use tables and must not
   contain unresolved gap wording such as TODO.
6. **Implement rules.** Put the implementation in the closest existing
   `rule_groups` module or create a new focused module. Import it from
   `rules.py` and place it where earlier/later rule assumptions remain valid.
   For example, pragma and legacy syntax rules run early, interface facts should
   be normalized before external-call inference, numeric rewrites run before
   late cleanup, and validation metadata rules remain last.
7. **Add tests.** Cover successful rewrite output, diagnostics for ambiguous
   cases, comments/strings/docstrings, version gating, idempotence, and compiler
   validation where relevant. Add or update `tests/test_versions.py` for new
   version ranges, source syntax floors, and EVM defaults.
8. **Smoke real contracts.** For broad changes, run a corpus smoke through
   `scripts/corpus.py smoke` against a representative manifest and inspect the
   rule/diagnostic summary plus artifact diffs.
9. **Update user-facing docs.** Update README support statements, options or
   examples, and `CHANGELOG.md`.
10. **Run full validation.** Run lint, full pytest, and `scripts/smoke-wheel.sh`.

## Corpus tooling

`scripts/corpus.py` can build, import, dedupe, and smoke-test corpora. The
important maintainer command is the smoke runner:

```bash
uv run --locked python scripts/corpus.py smoke \
  --manifest corpus/vyper/deduped-manifest.json \
  --output corpus/vyper/smoke-results.json \
  --target-version 0.4.3 \
  --workers 4
```

Use `--limit` for a quick sample and `--path` to focus on known regressions. The
smoke command compiles source and target outputs, applies the same artifact
comparisons as the CLI, and records rule, diagnostic, compile, and diff details.

Corpus source directories and generated outputs live under `corpus/`, which is
ignored by Git.

## Release process

Publishing uses GitHub Actions and PyPI Trusted Publishing. The publish workflow
runs on `v*` tags, builds the package, publishes to PyPI, and creates or updates
the GitHub release using notes extracted from `CHANGELOG.md`.

Before tagging:

1. Update `pyproject.toml` version.
2. Add a matching `CHANGELOG.md` section. `scripts/release_notes.py` expects a
   heading such as `## 0.4.2 - YYYY-MM-DD` for tag `v0.4.2`.
3. Run:

   ```bash
   uv run --locked ruff check src tests scripts/corpus.py scripts/release_notes.py
   uv run --locked pytest
   scripts/smoke-wheel.sh
   ```

4. Tag and push:

   ```bash
   git tag v0.4.2
   git push origin v0.4.2
   ```

The PyPI trusted publisher should be configured for repository
`banteg/vyupgrade`, workflow `publish.yml`, and environment `pypi`.
