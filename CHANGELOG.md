# Changelog

## Unreleased

- Added opt-in support for targeting Vyper `0.5.0a3`; custom error syntax is
  treated as newly accepted source and broad alpha pragmas compile with `a3`
  when needed.
- Added a range migration that collapses leading loop sentinel `if`/`break`
  blocks into `range(..., bound=...)` when the bounded stop is inferable.

## 0.4.1 - 2026-06-06

- Fixed standard-json corpus imports so only declared compilation targets are
  smoked while helper package modules remain available for dependency
  resolution.
- Fixed target validation for multiline function NatSpec docstrings,
  package-local helper imports, and decimal-enabled math imports.
- Added a migration rewrite for non-ASCII string literal characters that modern
  target compilers reject.
- Kept confirmed decimal selector changes surfaced as method identifier diffs.

## 0.4.0 - 2026-06-02

- Added corpus import tooling and targeted corpus smoke selection for repeatable large-corpus regression checks.
- Fixed broad migration failures found in large-corpus validation, including overlapping bitwise rewrites, integer division detection, signedness casts, external-call inference, struct literal ordering, NatSpec validation cleanup, and dependency import resolution.
- Improved source and target compiler validation for standard-json packages, overlay import trees, broad version pragmas, old compiler span crashes, and common dependency hints.
- Normalized noisy ABI, method identifier, and storage layout metadata so corpus reports focus on meaningful behavior changes.
- Preserved legacy public fixed-array getter selectors and legacy storage/interface type surfaces when comparing upgraded artifacts.
- Kept storage layout movements surfaced by default, including nonreentrant transient-storage moves, instead of inserting compatibility gaps automatically.

## 0.3.1 - 2026-05-31

- Fixed `--format mamushi` write runs so missing, timed out, or failing formatter executions are captured in report output instead of crashing.

## 0.3.0 - 2026-05-30

- Added opt-in support for targeting Vyper `0.5.0a1` and `0.5.0a2`.
- Added rewrites for `isqrt`, duplicate or repeated `implements` declarations, concrete interface defaults, and docstring-only function bodies when migrating to the alpha targets.
- Added an error diagnostic when the inferred source version is newer than the requested target.
- Moved rule gating into descriptors and split rule implementations and tests by rule group for easier maintenance.
- Isolated compiler-backed CLI tests from fast CLI tests to keep the default suite quick while preserving real compiler coverage.

## 0.2.0 - 2026-05-29

- Added support for validating and migrating older Vyper beta-era contracts.
- Added optional interface splitting into generated `.vyi` files.
- Added colored CLI diffs and more detailed ABI, method identifier, storage layout, and EVM default diagnostics.
- Improved target validation coverage across Curve, Yearn, and Yield Basis corpora.
- Fixed additional rewrites for external calls, shifted `method_id()` values, signed hashmap keys, pure/view interactions, blueprint offsets, legacy ERC interface facts, and older syntax forms.
- Hardened reporting, compiler command preparation, corpus tooling, and rule-code coverage checks.

## 0.1.0 - 2026-05-29

- Initial public release of `vyupgrade`.
- Supports compiler-backed Vyper migrations from `0.2.1` through `0.4.3`.
- Validates rewritten contracts by compiling source and target outputs and comparing ABI, method identifiers, and storage layout.
- Includes focused rewrites for legacy syntax, external call keywords, integer division, typed loops, nonreentrant locks, built-in interface imports, and manual-review diagnostics.
