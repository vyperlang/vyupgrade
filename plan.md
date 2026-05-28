# `vyupgrade` — spec for a pyupgrade-like Vyper upgrader

The right scope is **not** “auto-port any Vyper contract.” It should be a **compiler-backed codemod tool** that performs safe mechanical rewrites, performs type-aware rewrites only when it can prove enough context, and emits review diagnostics for everything else.

As of now, I would target **Vyper 0.3.x ? 0.4.x** first, with **0.4.3 as the default stable target**. PyPI shows 0.4.3 as the stable release, while 0.5.0a1 exists as a pre-release and explicitly points users back to stable 0.4.3. ([PyPI][1]) The main reason to start with 0.3.x ? 0.4.x is that Vyper 0.4.0 was a major language overhaul: import/module changes, required external call keywords, integer `//`, struct keyword arguments, typed loop variables, `enum` ? `flag`, interface path changes, and more. ([docs.vyperlang.org][2])

---

## 1. Product goal

`vyupgrade` upgrades Vyper source code between language versions.

It should:

```bash
vyupgrade contracts/ --target-version 0.4.3 --write
```

and produce either:

```text
changed 7 files
applied 31 fixes
left 4 review diagnostics
target compile: passed
```

or:

```text
changed 5 files
target compile: failed
reverted writes; see report.json
```

The core promise should be:

> `vyupgrade` never silently applies a rewrite that it cannot validate syntactically, structurally, or through compiler output.

That is the big difference from a normal formatter.

---

## 2. Initial supported version matrix

### MVP

| From                                        |      To | Status                 |
| ------------------------------------------- | ------: | ---------------------- |
| `0.3.7`–`0.3.10`                            | `0.4.3` | Primary target         |
| `0.4.0`–`0.4.2`                             | `0.4.3` | Secondary target       |
| no pragma, user supplies `--source-version` | `0.4.3` | Supported with warning |

### Later

| From            |           To | Notes                                          |
| --------------- | -----------: | ---------------------------------------------- |
| `0.2.1`–`0.3.x` |      `0.4.x` | More syntax churn; useful but not MVP          |
| pre-`0.2.1`     | modern Vyper | Best handled as diagnostics + partial rewrites |
| `0.4.x`         |      `0.5.x` | Wait until 0.5 stable                          |

Old Vyper migrations are worth supporting eventually: Vyper 0.2.1 renamed `@public`/`@private` to `@external`/`@internal`, renamed `@constant` to `@view`, changed event syntax, changed mappings to `HashMap[...]`, changed interfaces from `contract` to `interface`, and changed `bytes`/`string` to `Bytes`/`String`. ([docs.vyperlang.org][2]) But that should be a second milestone, not the first.

---

## 3. Non-goals

`vyupgrade` should **not** claim to prove full semantic equivalence. It can compare ABI, storage layout, compiler diagnostics, and optionally run tests, but smart-contract behavior across compiler versions is too broad to certify automatically.

It should also avoid:

| Non-goal                                   | Reason                                                                            |
| ------------------------------------------ | --------------------------------------------------------------------------------- |
| Gas optimization                           | That biases migrations and risks changing intent                                  |
| Rewriting deployed contracts or migrations | Out of source-codemod scope                                                       |
| Inventing module architecture              | Vyper 0.4 modules are powerful, but automatically restructuring projects is risky |
| Auto-fixing ambiguous external calls       | `extcall` vs `staticcall` depends on interface mutability                         |
| Auto-porting pre-0.2 code by default       | Too many old syntax and semantic changes                                          |

---

## 4. Core CLI

```bash
# dry-run, show summary
vyupgrade contracts/

# write changes
vyupgrade contracts/ --target-version 0.4.3 --write

# check mode for CI
vyupgrade contracts/ --target-version 0.4.3 --check

# show unified diff
vyupgrade contracts/ --diff

# emit machine-readable diagnostics
vyupgrade contracts/ --report-json vyupgrade-report.json

# run only selected rules
vyupgrade contracts/ --select VY040,VY041,VY050

# disable selected rules
vyupgrade contracts/ --ignore VY060,VY090

# allow inferred rewrites that require type information
vyupgrade contracts/ --aggressive --write

# validate with a project test suite after compiling
vyupgrade contracts/ --write --test-command "pytest -q"

# use a known source compiler
vyupgrade contracts/ --source-version 0.3.10 --target-version 0.4.3
```

Recommended exit codes:

| Code | Meaning                          |
| ---: | -------------------------------- |
|  `0` | No changes needed, no errors     |
|  `1` | Changes needed in `--check` mode |
|  `2` | Target compile failed            |
|  `3` | Source compile failed            |
|  `4` | Internal tool error              |
|  `5` | Unsafe rewrite blocked           |

---

## 5. Config file

Use `pyproject.toml`:

```toml
[tool.vyupgrade]
target-version = "0.4.3"
source-version = "infer"
paths = ["contracts", "interfaces"]
include = ["*.vy", "*.vyi"]
exclude = ["lib/vendor/**", "build/**"]

write = false
aggressive = false
format = "none"          # none | mamushi
report-json = "vyupgrade-report.json"

preserve-evm-version = true
preserve-optimization = true
compiler-search-paths = ["."]
```

Vyper supports `.vy` modules and `.vyi` or `.json` interfaces through its import system, so the project scanner should include all three where relevant. ([docs.vyperlang.org][3])

---

## 6. Rewrite rule classes

Each rule should have a safety class:

| Class              | Meaning                                                              | Default behavior                         |
| ------------------ | -------------------------------------------------------------------- | ---------------------------------------- |
| **A: mechanical**  | Token-level or syntax-level rewrite with no expected semantic change | Auto-apply                               |
| **B: inferred**    | Requires type/interface/compiler information                         | Auto-apply only if inference succeeds    |
| **C: review**      | Probably correct but may change behavior                             | Emit diagnostic; optional `--aggressive` |
| **D: unsupported** | Tool cannot safely rewrite                                           | Diagnostic only                          |

Every emitted change should carry a rule ID:

```text
VY040 external-call-keyword: added staticcall to IERC20.balanceOf(...)
VY050 integer-division: changed / to // for uint256 operands
VY090 nonreentrant-lock: review required; named locks collapse to global lock in 0.4.x
```

---

## 7. MVP rule set: Vyper 0.3.x ? 0.4.3

The 0.4.0 release notes are the main source for this batch: they list the external-call keyword requirement, integer floordiv change, struct keyword-argument requirement, typed loop variables, `enum` ? `flag`, named reentrancy lock removal, ERC interface import rename, and `create_from_blueprint` default offset change. ([docs.vyperlang.org][2])

| Rule    | Rewrite                                                                   | Class | Notes                                                                                                                                                                            |
| ------- | ------------------------------------------------------------------------- | ----: | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VY001` | `# @version ...` ? `#pragma version ...`                                  |     A | Keep range unless `--bump-pragma` is set. Vyper supports both forms, but `#pragma version` is the modern spelling. ([docs.vyperlang.org][3])                                     |
| `VY002` | Add `@deploy` above `def __init__`                                        |     A | In current docs, `__init__()` must be declared with `@deploy`. ([docs.vyperlang.org][4])                                                                                         |
| `VY010` | `_abi_encode` ? `abi_encode`                                              |     A | Current docs say the old name prior to 0.4.0 was `_abi_encode`. ([docs.vyperlang.org][5])                                                                                        |
| `VY011` | `_abi_decode` ? `abi_decode`                                              |     A | Same for `_abi_decode`. ([docs.vyperlang.org][5])                                                                                                                                |
| `VY020` | `from vyper.interfaces import ERC20` ? `from ethereum.ercs import IERC20` |     B | Needs an explicit built-in-interface mapping table. Current docs import built-ins from `ethereum.ercs`, e.g. `IERC20`. ([docs.vyperlang.org][6])                                 |
| `VY030` | `enum Foo:` ? `flag Foo:`                                                 |   B/C | Auto only when usage is compatible. Otherwise diagnostic.                                                                                                                        |
| `VY040` | `token.transfer(...)` ? `extcall token.transfer(...)`                     |     B | Requires interface mutability. `extcall` is required for payable/nonpayable external calls. ([docs.vyperlang.org][6])                                                            |
| `VY041` | `token.balanceOf(...)` ? `staticcall token.balanceOf(...)`                |     B | Requires interface mutability. `staticcall` is required for view/pure external calls. ([docs.vyperlang.org][6])                                                                  |
| `VY050` | `a / b` ? `a // b`                                                        |     B | Only when both operands are integer-typed. Decimal division must remain `/`; decimals are still distinct and must be enabled with `--enable-decimals`. ([docs.vyperlang.org][7]) |
| `VY060` | `MyStruct({a: x, b: y})` ? `MyStruct(a=x, b=y)`                           |     B | Struct keyword args are required in 0.4.x.                                                                                                                                       |
| `VY070` | `for i in range(n):` ? `for i: uint256 in range(n):`                      |     B | Infer type from `range` or array element. Current syntax requires `for i: <TYPE> in <ITERABLE>`. ([docs.vyperlang.org][4])                                                       |
| `VY080` | `create_from_blueprint(x)` ? `create_from_blueprint(x, code_offset=0)`    |     C | Only if preserving old behavior. Current default is `3`; before 0.4.0 it was `0`. ([docs.vyperlang.org][5])                                                                      |
| `VY090` | `@nonreentrant("lock")` ? `@nonreentrant`                                 |     C | Named locks were removed. If multiple lock names exist, never silently rewrite without review.                                                                                   |
| `VY100` | `sqrt(x)` ? `math.sqrt(x)` + `import math`                                |     B | Needed for 0.4.2+. Docs say `sqrt` moved to the math stdlib module in 0.4.2. ([docs.vyperlang.org][8])                                                                           |
| `VY110` | `bitwise_and(a,b)` ? `a & b`; similar for `or`, `xor`, `not`              |     B | Current docs say bitwise builtins were deprecated and removed in 0.4.2. ([docs.vyperlang.org][5])                                                                                |

---

## 8. Diagnostics-only rules for MVP

Some changes should be detected but not auto-fixed by default.

| Rule     | Diagnostic                                                                                          |
| -------- | --------------------------------------------------------------------------------------------------- |
| `VYD001` | Decimal type is used; target compile must pass with `--enable-decimals`.                            |
| `VYD002` | Multiple named reentrancy locks found; 0.4.x global lock may alter callback/reentrancy behavior.    |
| `VYD003` | External call target has no known interface; cannot choose `extcall` vs `staticcall`.               |
| `VYD004` | `/` operator involves unknown or decimal-like type; cannot safely rewrite to `//`.                  |
| `VYD005` | Source has no version pragma and no `--source-version`.                                             |
| `VYD006` | Source compile fails under declared source compiler; rewrite quality is degraded.                   |
| `VYD007` | ABI changed after migration.                                                                        |
| `VYD008` | Storage layout changed after migration.                                                             |
| `VYD009` | Target compiler default EVM version differs from source-era default; preserve explicitly or review. |
| `VYD010` | `block.prevrandao` usage found; signature changed in 0.4.0 and requires manual review.              |

---

## 9. Architecture

### 9.1 Pipeline

```text
discover files
  ?
infer source + target versions
  ?
build import graph
  ?
run source compiler, if available
  ?
collect AST / annotated AST / ABI / layout
  ?
plan rewrite patches
  ?
apply patches to an in-memory buffer
  ?
run target compiler
  ?
compare artifacts
  ?
optionally format
  ?
optionally run test command
  ?
write files or print diff
```

The Vyper CLI can emit outputs including `ast`, `annotated_ast`, `abi`, `bytecode`, `method_identifiers`, and `layout`, which gives this tool a decent validation surface. ([docs.vyperlang.org][9]) The `layout` output is especially useful because it exposes storage placement and supports a hard “do not silently alter layout” check. ([docs.vyperlang.org][9])

### 9.2 Main components

```text
vyupgrade/
  cli.py
  config.py
  project.py
  versions.py
  compiler/
    manager.py
    artifacts.py
  parser/
    tolerant_tokens.py
    source_map.py
  analysis/
    symbols.py
    imports.py
    types.py
    interfaces.py
  rules/
    base.py
    v03_to_v04.py
    v04_patch.py
  rewrite/
    patch.py
    apply.py
    comments.py
  validate/
    compile.py
    compare.py
    test_command.py
  report/
    text.py
    json.py
    sarif.py
```

### 9.3 Parser strategy

Use a **hybrid parser**, not a full new compiler frontend.

1. Use a tolerant token/source-span parser for source-preserving edits.
2. Use Vyper compiler outputs for AST, type, ABI, method ID, and layout validation.
3. Fall back to diagnostics if source compiler cannot parse the file.

This avoids the trap of trying to maintain a second Vyper parser. It also keeps formatting/comments intact.

### 9.4 Compiler manager

The compiler manager should support:

```bash
vyper --version
vyper -f ast,annotated_ast,abi,method_identifiers,layout file.vy
vyper-json project.json
```

It should find compilers in this order:

1. Explicit `--source-vyper /path/to/vyper`
2. Explicit `--target-vyper /path/to/vyper`
3. Current virtualenv
4. `uvx vyper==...` or isolated venv
5. Docker image, optional later

The report must record exact compiler versions and commands.

---

## 10. Validation model

A rewrite is accepted only if the target source compiles.

Then compare:

| Artifact               |                   Required? | Purpose                                     |
| ---------------------- | --------------------------: | ------------------------------------------- |
| target compile success |                    Required | Minimum safety                              |
| ABI selectors          | Required if source compiled | Catch public interface drift                |
| method identifiers     | Required if source compiled | Catch selector changes                      |
| storage layout         | Required if source compiled | Catch storage corruption                    |
| events                 | Required if source compiled | Catch log/indexed changes                   |
| warnings               |                    Required | Surface new compiler concerns               |
| bytecode               |               Informational | Expected to differ across compiler versions |
| tests                  |                    Optional | Project-level behavior check                |

A target bytecode diff should **not** fail migration by itself. Different compiler versions and default EVM versions can naturally produce different bytecode. Vyper 0.4.3, for example, updated the default EVM version to `prague`. ([docs.vyperlang.org][2])

---

## 11. Report format

Text summary:

```text
vyupgrade 0.1.0
source: inferred 0.3.10
target: 0.4.3

changed:
  contracts/Vault.vy
    VY001 pragma: # @version -> #pragma version
    VY002 constructor: added @deploy
    VY040 external call: added extcall to token.transfer(...)
    VY050 division: / -> // for uint256 operands

review:
  contracts/Vault.vy:88 VYD002 multiple named reentrancy locks
  contracts/Factory.vy:41 VY080 create_from_blueprint default changed; add code_offset=0?

validation:
  source compile: passed
  target compile: passed
  ABI: unchanged
  method IDs: unchanged
  storage layout: unchanged
```

JSON report:

```json
{
  "source_version": "0.3.10",
  "target_version": "0.4.3",
  "files": [
    {
      "path": "contracts/Vault.vy",
      "changed": true,
      "fixes": [
        {
          "rule": "VY040",
          "line": 88,
          "message": "added extcall to nonpayable external call",
          "before": "token.transfer(msg.sender, amount)",
          "after": "extcall token.transfer(msg.sender, amount)"
        }
      ],
      "diagnostics": []
    }
  ],
  "validation": {
    "source_compile": "passed",
    "target_compile": "passed",
    "abi_equal": true,
    "method_ids_equal": true,
    "storage_layout_equal": true
  }
}
```

SARIF output is worth adding for GitHub code scanning, but not MVP-critical.

---

## 12. Examples of key rewrites

### Constructor

Before:

```vyper
# @version ^0.3.10

owner: public(address)

@external
def __init__():
    self.owner = msg.sender
```

After:

```vyper
#pragma version ^0.4.0

owner: public(address)

@deploy
def __init__():
    self.owner = msg.sender
```

The tool should also remove invalid decorators around `__init__` where the target compiler rejects them.

### External calls

Before:

```vyper
interface Token:
    def transfer(to: address, amount: uint256) -> bool: nonpayable
    def balanceOf(owner: address) -> uint256: view

@external
def f(token: Token):
    token.transfer(msg.sender, 1)
    b: uint256 = token.balanceOf(msg.sender)
```

After:

```vyper
interface Token:
    def transfer(to: address, amount: uint256) -> bool: nonpayable
    def balanceOf(owner: address) -> uint256: view

@external
def f(token: Token):
    extcall token.transfer(msg.sender, 1)
    b: uint256 = staticcall token.balanceOf(msg.sender)
```

Current Vyper requires either `extcall` or `staticcall` before external calls, and the keyword must match function visibility/mutability. ([docs.vyperlang.org][6])

### Integer division

Before:

```vyper
shares: uint256 = amount / price
```

After:

```vyper
shares: uint256 = amount // price
```

Only apply when the annotated AST or local type analysis says the operands are integer types. Leave decimal division alone.

### Struct instantiation

Before:

```vyper
p: Person = Person({name: "alice", age: 30})
```

After:

```vyper
p: Person = Person(name="alice", age=30)
```

### Loop variables

Before:

```vyper
for i in range(MAX_USERS):
    self.users[i] = empty(address)
```

After:

```vyper
for i: uint256 in range(MAX_USERS):
    self.users[i] = empty(address)
```

---

## 13. Formatter integration

Do **not** build formatting into `vyupgrade`. Instead:

```bash
vyupgrade contracts/ --write --format mamushi
```

Mamushi is a Vyper formatter and says it compares the AST of reformatted code with the original by default for safety. ([GitHub][10]) That makes it a good optional final step, but the upgrader should work without it.

---

## 14. Acceptance criteria for MVP

The MVP is ready when:

1. It upgrades a fixture suite of 0.3.10 contracts to 0.4.3.
2. It handles `.vy` and `.vyi` files.
3. It preserves comments and most formatting.
4. It is idempotent: running twice produces no second diff.
5. It never rewrites comments or strings.
6. It compiles all changed files with the target compiler.
7. It compares ABI, method identifiers, and storage layout when source compilation succeeds.
8. It emits review diagnostics rather than guessing for ambiguous external calls, division, decimals, and named reentrancy locks.
9. It supports `--check`, `--diff`, `--write`, `--select`, `--ignore`, and `--report-json`.

---

## 15. Milestones

### Milestone 0 — discovery and reporting

Implement file discovery, pragma inference, compiler execution, JSON report, and target compile check. No rewrites yet.

### Milestone 1 — safe mechanical rewrites

Implement:

```text
VY001 pragma modernization
VY002 @deploy for __init__
VY010 _abi_encode -> abi_encode
VY011 _abi_decode -> abi_decode
```

### Milestone 2 — type-aware 0.4 rewrites

Implement:

```text
VY040/VY041 extcall/staticcall
VY050 integer / -> //
VY060 struct kwargs
VY070 typed loop variables
```

### Milestone 3 — project-level migration

Implement import graph, interface path migration, storage-layout comparison, method-ID comparison, and multi-file validation.

### Milestone 4 — conservative behavior-preservation rules

Implement diagnostics and opt-in rewrites for:

```text
VY080 create_from_blueprint code_offset
VY090 named nonreentrant locks
VY100 sqrt -> math.sqrt
VY110 bitwise builtins -> operators
```

### Milestone 5 — legacy support

Add pre-0.3 migrations, including decorator renames, event syntax, mapping syntax, old interface declarations, and old bytes/string type names. Those are documented breaking changes from Vyper 0.2.1. ([docs.vyperlang.org][2])

---

## 16. Recommended repository positioning

Name options:

```text
vyupgrade
vyper-upgrade
snekmate-upgrade  # cute but too tied to one ecosystem
```

I would use **`vyupgrade`**.

README tagline:

> A compiler-backed codemod tool for upgrading Vyper contracts across language versions.

One-liner:

```bash
pipx run vyupgrade contracts/ --target-version 0.4.3 --diff
```

Pre-commit hook:

```yaml
repos:
  - repo: https://github.com/your-org/vyupgrade
    rev: v0.1.0
    hooks:
      - id: vyupgrade
        args: ["--target-version", "0.4.3", "--check"]
```

---

## 17. Biggest risks

The hard parts are not the string rewrites. They are:

| Risk                                      | Mitigation                                                     |
| ----------------------------------------- | -------------------------------------------------------------- |
| Old compiler cannot run on current Python | Isolated compiler manager; allow explicit compiler path        |
| External call mutability unknown          | Require interface graph; otherwise diagnostic                  |
| Integer division ambiguity                | Use annotated AST/type info; otherwise diagnostic              |
| Reentrancy lock semantics                 | Never auto-collapse multiple named locks silently              |
| Storage layout drift                      | Compare `vyper -f layout` source vs target where possible      |
| Formatting churn                          | Patch spans only; optional Mamushi formatting                  |
| Compiler default EVM changes              | Preserve explicit EVM pragmas or emit diagnostic               |
| False confidence                          | Report “validated compile/layout/ABI,” not “proven equivalent” |

---

## 18. MVP cut line

Build **only this** first:

```text
0.3.10-ish source
? 0.4.3 target
? safe rewrites
? compiler-backed validation
? review diagnostics for ambiguous cases
```

That is narrow enough to ship, and it solves the most painful real migration: the Vyper 0.4 language break.

[1]: https://pypi.org/project/vyper/ "vyper · PyPI"
[2]: https://docs.vyperlang.org/en/latest/release-notes.html "Release Notes - Vyper documentation"
[3]: https://docs.vyperlang.org/en/latest/structure-of-a-contract.html "Structure of a Contract - Vyper documentation"
[4]: https://docs.vyperlang.org/en/latest/control-structures.html "Control Structures - Vyper documentation"
[5]: https://docs.vyperlang.org/en/latest/built-in-functions.html "Built-in Functions - Vyper documentation"
[6]: https://docs.vyperlang.org/en/stable/interfaces.html "Interfaces - Vyper documentation"
[7]: https://docs.vyperlang.org/en/stable/types.html?utm_source=chatgpt.com "Types - Vyper documentation"
[8]: https://docs.vyperlang.org/en/latest/built-in-functions.html?utm_source=chatgpt.com "Built-in Functions - Vyper documentation"
[9]: https://docs.vyperlang.org/en/latest/compiling-a-contract.html "Compiling a Contract - Vyper documentation"
[10]: https://github.com/benber86/mamushi "GitHub - benber86/mamushi: Formatter for Vyper · GitHub"
