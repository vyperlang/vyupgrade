# Migration Coverage

This document maps the syntax history in
[`vyper-syntax-history.md`](vyper-syntax-history.md) to `vyupgrade` behavior.
Each source-visible entry is classified as an automated rewrite, a diagnostic,
an explicit no-op when the historical change only introduced newly accepted
syntax, or validation-only when surviving obsolete source is left for compiler
validation. Alpha targets are opt-in; the default target remains the latest
supported final release.

Rules are version-gated. Unless noted as a target-floor rule, a rule runs only
when the inferred source version is older than the listed change and the target
version is at or after it. Target-floor rules cover legacy source syntax
whenever the requested target is in the supported range.

`0.1.0b*` source compilers are validated through a `typed-ast` compatibility
wrapper. This preserves source compilation for legacy compilers that expected
pre-Python-3.8 AST node classes while still allowing `vyupgrade` to run under a
modern Python interpreter.

## v0.5.x prereleases

### v0.5.0a2

- Docstring-only function bodies rejected: `VY131` inserts an explicit `pass`
  after the docstring.
- Wildcard interface lengths and unbounded length spellings added: no-op. This
  is newly accepted source syntax.

### v0.5.0a1

- `isqrt` moved into `math`: `VY101`.
- Multi-name interface imports added: no-op. This is newly accepted import
  syntax.
- Multi-interface `implements` added and duplicate `implements` rejected:
  `VY121` merges repeated declarations and collapses duplicate names.
- `...` interface default parameter values added: no-op. This is newly
  accepted interface syntax. `VY122` replaces concrete interface default values
  with `...`.
- Numeric literal underscores added: no-op. This is newly accepted literal
  syntax.
- Abstract module methods added: no-op. This is new opt-in module syntax.
- Event fields cannot be module types: validation-only.

## v0.4.x prereleases

### v0.4.1b4

- Absolute relative imports disallowed: `VYD015`.

### v0.4.1b2

- `module.__at__()` casts added: no-op. Existing interface cast syntax is not
  mechanically equivalent in the general case.

### v0.4.1b1

- `mana` call kwarg alias added: no-op. Existing `gas=` spelling remains valid.
- `@external` became optional in `.vyi` files: no-op. Existing explicit
  decorators remain valid.
- Event instantiation keyword arguments added: `VY112`.
- Native hex string literals added: no-op. Existing byte literals remain valid.

### v0.4.0b6

- `block.prevrandao` signature changed to `bytes32`: `VYD001`.

### v0.4.0b5

- Bytestring downcasts added: no-op. This is newly accepted source syntax.

### v0.4.0b3

- External calls require keywords: `VY040`, `VY041`, `VY042`, `VY057`, and
  `VYD003`.

### v0.4.0b1

- Module syntax and imports added: no-op for new opt-in syntax. Module import
  rewrites and diagnostics are otherwise covered by `VY120` and `VYD015`.
- Named reentrancy locks removed: `VY090` and `VYD002`.
- `enum` declarations replaced by `flag`: `VY030`.
- Loop variables require type annotations: `VY070`.
- Struct instantiation uses keyword arguments: `VY060`.
- Integer division uses `//`: `VY050` and `VYD004`.
- Builtin constants removed: `VY012`.
- Two-argument `range` can include `bound=`: `VY071` and `VYD011`.
- Builtin ERC interface imports moved and gained an `I` prefix: `VY020`.

## v0.4.x

### v0.4.3

- `@raw_return` added: no-op. This is new opt-in syntax, not a source form that
  older contracts must rewrite to compile.

### v0.4.2

- Decimal `sqrt` moved into `math`: `VY100` rewrites `sqrt(x)` to
  `math.sqrt(x)` and inserts `import math`.
- Deprecated bitwise builtins removed: `VY110` rewrites `bitwise_and`,
  `bitwise_or`, `bitwise_xor`, and `bitwise_not` to operators. `VY111`
  rewrites literal `shift(x, N)` and `shift(x, -N)`. `VYD012` flags
  non-literal shift amounts for manual `<<` or `>>` review.
- `raw_create()` added: no-op. This is new deployment syntax, not a required
  migration.
- File-level nonreentrancy pragma, `@reentrant`, and
  `public(reentrant(...))` added: no-op. These are new opt-in forms.

### v0.4.1

- `@external` became optional in `.vyi` files: no-op. Existing explicit
  decorators remain valid.
- `module.__at__()` casts added: no-op. Existing interface cast syntax is not
  mechanically equivalent in the general case.
- Event instantiation keyword arguments added: `VY112` rewrites positional
  event logs to keyword arguments when the event declaration is locally known.
- Native hex string literals added: no-op. Existing byte literals remain valid.
- `mana` call kwarg alias added: no-op. Existing `gas=` spelling remains valid.
- Absolute relative imports disallowed: `VYD015` flags nested modules with bare
  sibling-style imports for manual `from . import ...` review.

### v0.4.0

- Constructor visibility changed to `@deploy`: `VY002` removes invalid
  constructor visibility decorators and inserts `@deploy`.
- Named reentrancy locks removed: `VY090` removes the single-lock case,
  reserves legacy lock storage slots, and emits a review diagnostic. `VYD002`
  flags multiple named locks because global-lock behavior may change callback
  assumptions.
- Storage arrays bounded by `max_value(uint256)` or the max-uint256 literal:
  `VY091` lowers top-level declarations to `HashMap[uint256, T]` while
  preserving public getter shape.
- Unreachable code validation is stricter: `VY092` removes code in a block
  after unconditional terminators and after exhaustive terminating if-chains.
- `_abi_encode` and `_abi_decode` renamed: `VY010` and `VY011`.
- Memory `DynArray` allocations cannot use effectively unbounded
  `max_value(int128)` lengths: `VY094` caps that legacy idiom at
  `max_value(uint32)` and applies the same cap to matching range loops.
- `@internal` became optional: no-op. Existing `@internal` source remains valid.
- External calls require keywords: `VY040` adds `extcall`; `VY041` adds
  `staticcall`. `VY042` parenthesizes keyworded calls before subscripting.
  `VY057` assigns ignored `staticcall` results to a generated local variable
  when the interface return type is known. `VYD003` flags calls whose mutability
  cannot be inferred.
- Integer division uses `//`: `VY050` rewrites proven integer division.
  `VYD004` flags ambiguous `/` expressions.
- Redundant integer `convert(...)` calls rejected by modern Vyper: `VY051`
  removes converts when source facts prove the value already has the target
  integer type, and also handles legacy `uint256` converts after division and
  in constant initializers.
- Fixed-size array equality remains unsupported for some element types:
  `VY213` expands comparisons against `empty(T[N])` into elementwise checks.
- Struct equality remains unsupported: `VY214` expands comparisons against
  `empty(Struct)` into field-wise checks.
- Signed constants in unsigned arithmetic: `VY052` converts known signed global
  constants, such as old `N_COINS: constant(int128)` values, inside `uint256`
  arithmetic expressions. It also casts signed operands of `unsafe_sub(...)`
  when the call is used as an unsigned array index.
- Dynamic `Bytes[N]` declarations and call arguments initialized from hex byte
  literals: `VY053` rewrites the literal to a byte string form accepted by
  modern Vyper.
- Exponentiation typing became stricter: `VY054` folds known integer constants
  used as operands in unsigned exponent expressions, and `VY055` rewrites
  dynamic `uint256 ** uint256` expressions to `pow_mod256(...)`. `VY054` also
  rewrites signed integer boundary literals behind signed annotations to
  `min_value(...)` or `max_value(...)`, and folds exponent max literals inside
  `bytes32` conversions.
- Range bounds are type-checked against annotated loop variables: `VY056`
  converts signed integer constants inside `range(...)` when the loop variable
  has an unsigned integer annotation, and adds a literal `bound=` when the
  original constant expression makes the iteration count provable.
- Struct literals require keyword arguments: `VY060`.
- Loop variables require type annotations: `VY070`.
- `enum` replaced by `flag`: `VY030` diagnoses by default and rewrites only
  with `--aggressive`.
- Builtin constants removed: `VY012` rewrites `MAX_UINT256`, integer min/max
  constants, `ZERO_ADDRESS`, and `EMPTY_BYTES32`.
- Immutable variables now collide with explicit same-name accessors: `VY013`
  renames the immutable backing variable and preserves the external getter name.
- Constant variables now collide with explicit same-name accessors: `VY016`
  renames the constant backing variable and preserves the external getter name.
- NatSpec validation is stricter: `VY058` removes stale `@param` lines whose
  names do not exist in the function signature or have no description, strips
  Solidity-style colons from valid `@param name:` tags, and rewrites unknown
  `@fork` tags to `@custom:fork`. It also rewrites duplicate singleton fields
  such as `@notice` and `@author` to `@custom:<field>`.
- Local interface mutability is checked more strictly: `VY014` changes
  `nonpayable` interface entries to `view` when the implementation is a view
  function or `public(...)` getter, and changes `@pure` implementations to
  `@view` when an implemented interface requires a view method.
- Pure functions may no longer read immutable state: `VY015` changes `@pure`
  functions to `@view` when source facts show the function body references a
  top-level `immutable(...)` binding.
- View functions may not emit events: `VY017` removes `@view` from functions
  whose body contains a `log` statement.
- Interface-typed storage assignments are checked more strictly: `VY019`
  rewrites mismatched constructor casts to the declared storage interface type.
- Dynamic `range` bounds: `VY071` adds inferred `bound=` for two-argument
  ranges. `VYD011` flags two-argument ranges where the bound is not inferable.
- Builtin ERC interface import path changed: `VY020` rewrites known imports and
  interface type names. Legacy built-in interfaces used in `implements` are
  preserved as local interfaces when modern `ethereum.ercs` definitions are
  stricter than the old source-era built-ins. `VYD003` flags unknown
  `vyper.interfaces` imports.
- Interface default functions are rejected: `VY123` removes `def __default__`
  entries from local interface declarations.
- Known dependency import paths changed: `VY018` rewrites the old snekmate
  `create2_address` helper module to `create2`.
- Module import and ownership declarations added: no-op. This is new module
  syntax, not a required rewrite.
- Module exports added: no-op. This is new opt-in syntax.
- Imported events auto-exported: no-op. This changes module composition rather
  than legacy source spelling.
- Decimal feature flag required: `VYD001`.
- `block.prevrandao` type changed: `VYD010`.

## v0.3.x

### v0.3.10

- `#pragma` directives added and version pragma parsing relaxed: `VY001`
  rewrites or adds `#pragma version` with the target version.
- Dynamic single-argument `range` bounds: `VYD014` flags `range(stop)` when
  `stop` is not literal and the target crosses `0.3.10`.

### v0.3.9

- No source syntax changes identified.

### v0.3.8

- `transient(...)` added: no-op. This is new opt-in storage syntax.
- Ternary operator added: no-op. Existing `if`/`return` source remains valid.
- Shift operators added: no-op for `0.3.8` targets; `VY111` handles later
  removal of `shift()` at `0.4.2`.
- `raw_revert()` added: no-op. Existing `raise` syntax remains valid.
- `send(..., gas=...)` added: no-op. Existing two-argument `send` remains valid.
- `custom:` NatSpec tags added: no-op.
- Unary plus disabled: `VY230`.
- Numeric boolean negation blocked: `VY231` rewrites integer `not x` to
  `x == 0`; `VYD013` flags unknown types.
- Enum values became valid mapping keys: no-op.

### v0.3.7

- `isqrt()` added: no-op. This is a new builtin, not a required migration.
- `epsilon()` added: no-op. This is a new builtin.
- `block.prevrandao` alias added: `VY220` rewrites `block.difficulty` to
  `block.prevrandao`.
- Public constants and immutables added: no-op. Existing declarations remain
  valid.

### v0.3.6

- No source syntax changes identified.

### v0.3.5

- `create_from_blueprint` accepts arbitrary data: no-op for source migration;
  `VY080` handles the later `0.4.0` default `code_offset` behavior change.
- `empty()` accepted in constants/default arguments: no-op. This only accepts a
  form previously rejected.

### v0.3.4

- `enum` custom type added: no-op for migrations into `0.3.4`; `VY030` handles
  the later `0.4.0` `enum` to `flag` migration.
- `flag` became a reserved declaration keyword: `VY093` renames a legacy
  public storage variable named `flag` and adds a getter preserving `flag()`.
- `_abi_decode()` added: no-op for `0.3.4`; `VY011` handles the later `0.4.0`
  rename to `abi_decode`.
- `create_from_blueprint()` and `create_copy_of()` added: no-op for `0.3.4`;
  `VY080` handles the later `0.4.0` review point.
- `default_return_value=` added: no-op. This is new optional syntax.
- `min_value()` and `max_value()` added: no-op for `0.3.4`; `VY012` uses these
  forms for the later builtin constant migration.
- `uint2str()` added: no-op. This is a new builtin.
- `msg.data` accepted directly in `raw_call`: no-op.
- `shift()` supports signed integers: no-op for `0.3.4`; `VY111` handles the
  later `0.4.2` removal.
- Dynamic arrays of strings enabled: no-op.

### v0.3.3

- `print()` debug builtin added: no-op.

### v0.3.2

- `convert()` semantics generalized: no-op.
- Hex and bytes literals restricted: `VYD210` flags mismatched `Bytes` and
  `String` literal forms when migrating legacy code.
- `DynArray[T, N]` added: no-op. The syntax history entry is a newly accepted
  type form, not a reliable array rewrite.
- Full ABI v2 integer and bytes types added: no-op.
- `<address>.code` added: no-op.
- `tx.gasprice` added: no-op.
- Struct constants accepted: no-op.
- `skip_contract_check=` added: no-op.
- `unsafe_*` builtins added: no-op.
- Lists of any type can be loop variables: no-op.

### v0.3.1

- `immutable(T)` added: no-op.
- `uint8` added: no-op.
- `block.gaslimit` and `block.basefee` added: no-op.
- Checkable `raw_call()` added: no-op.
- Non-constant revert strings accepted: no-op.
- Slices of complex expressions accepted: no-op.
- Lists of structs can be function arguments: no-op.

### v0.3.0

- ABI-encodable function argument and return types expanded: no-op.
- `create_minimal_proxy_to(..., salt=...)` added: no-op.

## v0.2.x

### v0.2.16

- `_abi_encode()` exposed: no-op for `0.2.16`; `VY010` handles the later
  `0.4.0` rename.
- Event arguments can be any ABI-encodable type: no-op.
- Interfaces can appear in lists, structs, and maps: no-op.
- `@nonreentrant` on constructors disallowed: `VY210`.

### v0.2.15

- No source syntax changes identified.

### v0.2.14

- No source syntax changes identified.

### v0.2.13

- `abs()` added: no-op.

### v0.2.12

- `int256` added: no-op.
- `msg.data` added: no-op.

### v0.2.11

- No source syntax changes identified.

### v0.2.10

- No source syntax changes identified.

### v0.2.9

- Reserved keyword checks updated: `VYD211` covers the concrete legacy
  parameter name case from `0.2.1`, including `value`.

### v0.2.8

- `not in` comparator added: `VY211`.
- `empty(...)` accepted as a function-call argument: no-op.
- Empty static arrays accepted in `log`: no-op.
- `Bytes` variables can be mapping keys: no-op.

### v0.2.7

- No source syntax changes identified.

### v0.2.6

- `uint256` implicit range-loop iterator type: no-op. The source spelling did
  not change; later `VY070` handles explicit loop-variable annotations for
  `0.4.0`.

### v0.2.5

- Local-variable scoping restrictions removed: no-op.

### v0.2.4

- No source syntax changes identified.

### v0.2.3

- No source syntax changes identified.

### v0.2.2

- No source syntax changes identified.

### v0.2.1

- `@public` and `@private` renamed: `VY201`.
- `@constant` renamed: `VY201`.
- Type units removed, and the legacy `timestamp` type became `uint256`:
  `VY202`.
- Event declaration syntax changed: `VY203`.
- `log` became a statement: `VY204`.
- Mapping declarations changed to `HashMap`: `VY205` handles both
  `map(key, value)` and older `value[key]` mapping syntax.
- Interfaces use `interface`; legacy signature mutability keywords `constant`
  and `modifying` become `view` and `nonpayable`: `VY206`. It also handles
  legacy `contract Foo():` headers, `address(Interface)` annotations,
  interface-typed storage variables lowered to `address` with call-site casts,
  and interface methods that must become `payable` because calls pass `value=`.
- Public fixed-array getters used `int128` indexes in pre-0.2.1 ABIs:
  `VY223` adds explicit compatibility getters and renames backing storage.
- Dynamic byte and string type names capitalized: `VY207`.
- Byte and string literals are no longer interchangeable: `VYD210`.
- `assert_modifiable()` and `as_unitless_number()` removed: `VY208`.
- `create_with_code_of()` renamed: `VY208` rewrites it to
  `create_copy_of()`.
- Function input name `value` reserved: `VY212` renames legacy inputs and
  updates local references. `VYD211` remains for cases the fixer cannot handle.
- Builtin names `min_value` and `max_value` reserved: `VY222` renames colliding
  function-local variables and updates local references.
- `slice()` start and length must be `uint256`: `VYD212`.
- `len()` returns `uint256`: `VYD213`.
- External-call `value=` and `gas=` kwargs must be `uint256`: `VYD214`.
- `raw_call` kwarg `outsize` renamed: `VY208`.
- `raw_call` delegate calls cannot also pass `value=`: `VY208` removes the
  value kwarg when `is_delegate_call=True`.
- `raw_call` calldata must be dynamic bytes: `VY208` rewrites
  `empty(bytes32)` calldata to `b""`.
- `raw_call` `max_outsize=` must be `uint256`: `VY208` folds
  `max_value(uintN)` bounds to integer literals.
- `extract32` kwarg `type` renamed: `VY208`.
- Public array getter indexes use `uint256`: no-op. This is ABI shape, not a
  source rewrite.
- Public struct getters return all values: no-op. This is ABI shape, not a
  source rewrite.
- `RLPList` removed: `VYD215`.
- `empty()` added: no-op.
- `@pure` added: no-op.
- `raise` can omit a reason string: no-op.
- `method_id()` type argument made optional: `VY209`.
- `raw_call` can perform `STATICCALL`: no-op.
- Interfaces can be split into generated `.vyi` files: `VY120`.

## v0.1.0 beta prereleases

### v0.1.0-beta.17

- Required `raw_call` and `slice` arguments became positional:
  `VY221` rewrites `slice(data, start=..., len=...)` to positional arguments.
  The `v0.2.1` coverage covers the later `raw_call` `outsize` to
  `max_outsize` spelling and `slice()` integer-width diagnostics.
- NatSpec comments added: no-op for beta targets. `VY058` handles a later
  `0.4.0` NatSpec tag syntax cleanup.

### v0.1.0-beta.15

- `chain.id` added: no-op. This is new opt-in environment syntax.
- `address.codehash` added: no-op. This is new opt-in environment syntax.
- Scientific notation for numeric literals accepted: no-op. This is newly
  accepted literal syntax.

### v0.1.0-beta.14

- `bytes[32]` implicitly rewrites to `bytes32`: `VY207` handles the later
  dynamic byte and string spelling cleanup for supported targets.
- Scientific notation rejected after previously parsing incorrectly: no-op.
  `v0.1.0-beta.15` accepts it again.
- `for ... else` disallowed: no-op. This is rejected source, not a migration
  target.

### v0.1.0-beta.13

- Environment variables and constants as default parameter values: no-op. This
  is newly accepted source syntax.

### v0.1.0-beta.12

- Relative imports added: no-op for beta targets. `VYD015` handles the later
  `0.4.1` import-resolution restriction.

### v0.1.0-beta.11

- `sha3()` removed: `VY217` rewrites it to `keccak256()`.
- String and dynamic bytes equality added: no-op. This is newly accepted
  expression syntax.

### v0.1.0-beta.10

- Unreachable assertions added: no-op. This is new opt-in assertion syntax.

### v0.1.0-beta.9

- List constants added: no-op. This is newly accepted constant syntax.
- `sha256()` added: no-op. This is new opt-in builtin syntax.
- `create_with_code_of()` renamed to `create_forwarder_to()`: `VY208` handles
  the modern `create_copy_of()` spelling used by supported targets.
- `@nonreentrant` added: no-op for beta targets. Later reentrancy syntax
  migrations are covered by `VY090`, `VY210`, and `VYD002`.

### v0.1.0-beta.8

- `string[N]` type support added: `VY207` handles the later `string[N]` to
  `String[N]` rename.
- String support in builtins and expressions added: no-op. This is newly
  accepted source syntax.
- Source interfaces added: `VY206` handles the later `contract` to `interface`
  spelling change.

### v0.1.0-beta.7

- Constants in function and event signatures added: no-op. This is newly
  accepted source syntax.
- Implicit assignment conversions disallowed: validation-only.
- Side effects inside `assert` disallowed: validation-only.

### v0.1.0-beta.6

- Subscript mapping syntax changed to `map(...)`: `VY205` rewrites both this
  form and `map(...)` to modern `HashMap[...]`.
- Struct definitions and constructors added: no-op for beta targets. Later
  struct literal syntax is covered by `VY060`.
- `clear()` replaced `reset()` and `del` was disallowed: `VY219` rewrites
  `reset(...)` and simple `del x` statements to `clear(...)`.
- `EMPTY_BYTES32` added: `VY012` handles the later builtin-constant removal.

### v0.1.0-beta.5

- Unit annotations in signatures added: `VY202` handles later type-unit
  removal.
- Additional `convert()` target types added: no-op. This is newly accepted
  builtin syntax.

### v0.1.0-beta.4

- `convert(x, "T")` string type arguments changed to `convert(x, T)`: `VY218`.
- Custom constants added: no-op for beta targets. Later constant accessor
  collisions are covered by `VY016`.
- `if` and `assert` became stricter about boolean expressions:
  validation-only.

### v0.1.0-beta.3

- Default function arguments added: no-op. This is newly accepted source
  syntax.
- `assert` reason strings added: no-op. This is newly accepted assertion
  syntax.
- `not` restricted to boolean values: `VY231` and `VYD013` handle later numeric
  `not` cleanup.
- `num128` replaced by `int128`: `VY216`.

### v0.1.0-beta.2

- Function docblocks added: no-op for beta targets. Later docstring-only
  function bodies are covered by the `v0.5.0a2` syntax history entry.
- Builtin constants added: `VY012` handles later builtin-constant removal.

### v0.1.0-beta.1

- Initial beta source forms are validation-only where they predate later
  supported rewrite rules. Later target-floor rules cover major surviving forms:
  `VY201`, `VY202`, `VY203`, `VY204`, `VY206`, `VY207`, `VY208`, `VY209`,
  `VY216`, and `VY221`.

## Global Diagnostics

- `VYD005`: source has no version pragma and no `--source-version`.
- `VYD006`: source compilation failed under the declared or inferred source
  compiler.
- `VYD007`: ABI or method identifiers changed after migration.
- `VYD008`: storage layout changed after migration.
- `VYD009`: target compiler default EVM version differs from source-era default.
- `VYD016`: source version resolves to a compiler newer than the requested
  target.
