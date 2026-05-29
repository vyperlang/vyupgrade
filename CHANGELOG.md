# Changelog

## 0.1.0

- Initial public release of `vyupgrade`.
- Supports compiler-backed Vyper migrations from `0.2.1` through `0.4.3`.
- Validates rewritten contracts by compiling source and target outputs and comparing ABI, method identifiers, and storage layout.
- Includes focused rewrites for legacy syntax, external call keywords, integer division, typed loops, nonreentrant locks, built-in interface imports, and manual-review diagnostics.
