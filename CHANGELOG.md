# Changelog

## 0.3.1

- Fixed `--format mamushi` write runs so missing, timed out, or failing formatter executions are captured in report output instead of crashing.

## 0.3.0

- Added opt-in support for targeting Vyper `0.5.0a1` and `0.5.0a2`.
- Added rewrites for `isqrt`, duplicate or repeated `implements` declarations, concrete interface defaults, and docstring-only function bodies when migrating to the alpha targets.
- Added an error diagnostic when the inferred source version is newer than the requested target.
- Moved rule gating into descriptors and split rule implementations and tests by rule group for easier maintenance.
- Isolated compiler-backed CLI tests from fast CLI tests to keep the default suite quick while preserving real compiler coverage.

## 0.2.0

- Added support for validating and migrating older Vyper beta-era contracts.
- Added optional interface splitting into generated `.vyi` files.
- Added colored CLI diffs and more detailed ABI, method identifier, storage layout, and EVM default diagnostics.
- Improved target validation coverage across Curve, Yearn, and Yield Basis corpora.
- Fixed additional rewrites for external calls, shifted `method_id()` values, signed hashmap keys, pure/view interactions, blueprint offsets, legacy ERC interface facts, and older syntax forms.
- Hardened reporting, compiler command preparation, corpus tooling, and rule-code coverage checks.

## 0.1.0

- Initial public release of `vyupgrade`.
- Supports compiler-backed Vyper migrations from `0.2.1` through `0.4.3`.
- Validates rewritten contracts by compiling source and target outputs and comparing ABI, method identifiers, and storage layout.
- Includes focused rewrites for legacy syntax, external call keywords, integer division, typed loops, nonreentrant locks, built-in interface imports, and manual-review diagnostics.
