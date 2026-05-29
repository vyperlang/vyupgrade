# Changelog

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
