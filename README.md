# vyupgrade

A compiler-backed codemod tool for upgrading Vyper contracts across language versions.

The first supported migration path is Vyper `0.3.x` to stable `0.4.3`.

```bash
vyupgrade contracts/ --target-version 0.4.3 --diff
vyupgrade contracts/ --target-version 0.4.3 --write --report-json vyupgrade-report.json
```

