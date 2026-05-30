# Vyper Syntax History

This document tracks Vyper source syntax changes from `v0.5.0a2` back through
`v0.2.1`, the first post-beta release. It is intended as upgrade source
material for `vyupgrade`.

Scope: this is about source-visible syntax and spelling: decorators, keywords,
declarations, type spellings, builtin names or signatures, call syntax, import
syntax, pragmas, and newly accepted source forms. It intentionally excludes
backend-only, ABI-layout-only, optimizer-only, EVM-default, CLI-only, and pure
runtime semantic changes unless they require source text to change.

## v0.5.x prereleases

### v0.5.0a2

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.5.0a2>

- Docstring-only function bodies are rejected; use an explicit `pass` body. [#4972](https://github.com/vyperlang/vyper/pull/4972)
  Before:
  ```vyper
  @internal
  def hook():
      """
      Optional override point.
      """
  ```
  After:
  ```vyper
  @internal
  def hook():
      """
      Optional override point.
      """
      pass
  ```
- Wildcard interface lengths and unbounded length spellings added for dynamic
  reference types, and interface return-type bounds now check covariantly.
  [#4967](https://github.com/vyperlang/vyper/pull/4967)
  Before:
  ```vyper
  interface Metadata:
      def tokenURI(id: uint256) -> String[1]: view
  ```
  After:
  ```vyper
  interface Metadata:
      def tokenURI(id: uint256) -> String[...]: view

  # Unbounded length spellings:
  # Bytes[INF]
  # String[INF]
  # DynArray[uint256, INF]
  ```

### v0.5.0a1

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.5.0a1>

- `isqrt` moved into the `math` stdlib module. [#4923](https://github.com/vyperlang/vyper/pull/4923)
  Before:
  ```vyper
  return isqrt(x)
  ```
  After:
  ```vyper
  import math
  return math.isqrt(x)
  ```
- Interface imports can import multiple names from the same module. [#4762](https://github.com/vyperlang/vyper/pull/4762)
  Before:
  ```vyper
  from interfaces import IERC20
  from interfaces import IERC4626
  ```
  After:
  ```vyper
  from interfaces import IERC20, IERC4626
  ```
- Multiple interfaces can be declared in a single `implements` statement, and
  duplicate `implements` declarations are rejected. [#4772](https://github.com/vyperlang/vyper/pull/4772), duplicate rejection [#4775](https://github.com/vyperlang/vyper/pull/4775)
  Before:
  ```vyper
  implements: IERC20
  implements: IERC4626
  ```
  After:
  ```vyper
  implements: (IERC20, IERC4626)
  ```
- Interface default parameter values can be written as `...`; concrete default
  values in interfaces are deprecated. [#4813](https://github.com/vyperlang/vyper/pull/4813)
  Before:
  ```vyper
  interface Vault:
      def deposit(amount: uint256 = 0): nonpayable
  ```
  After:
  ```vyper
  interface Vault:
      def deposit(amount: uint256 = ...): nonpayable
  ```
- Numeric literals accept underscores as visual separators. [#3665](https://github.com/vyperlang/vyper/pull/3665)
  Before:
  ```vyper
  FEE_DENOMINATOR: constant(uint256) = 10000000000
  ```
  After:
  ```vyper
  FEE_DENOMINATOR: constant(uint256) = 10_000_000_000
  ```
- Abstract module methods added with `@abstract`, `@override`, and ellipsis
  bodies. [#4875](https://github.com/vyperlang/vyper/pull/4875)
  Before:
  ```vyper
  # No abstract module method syntax.
  ```
  After:
  ```vyper
  @abstract
  def quote(amount: uint256) -> uint256: ...

  @override
  def quote(amount: uint256) -> uint256:
      return amount
  ```
- Event fields cannot be module types. [#4768](https://github.com/vyperlang/vyper/pull/4768)
  Before:
  ```vyper
  import math

  event Loaded:
      value: math
  ```
  After:
  ```vyper
  event Loaded:
      value: uint256
  ```

## v0.4.x

### v0.4.3

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.4.3>

- `@raw_return` added for returning raw bytes without ABI encoding. [#4568](https://github.com/vyperlang/vyper/pull/4568), interface restriction [#4700](https://github.com/vyperlang/vyper/pull/4700)
  Before:
  ```vyper
  def proxy() -> Bytes[1024]:
      return raw_call(target, msg.data, max_outsize=1024)
  ```
  After:
  ```vyper
  @external
  @raw_return
  def proxy() -> Bytes[1024]:
      return raw_call(target, msg.data, max_outsize=1024)
  ```

### v0.4.2

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.4.2>

- Decimal `sqrt` moved into the `math` stdlib module. [#4520](https://github.com/vyperlang/vyper/pull/4520)
  Before:
  ```vyper
  return sqrt(x)
  ```
  After:
  ```vyper
  import math
  return math.sqrt(x)
  ```
- Deprecated bitwise builtins removed. [#4552](https://github.com/vyperlang/vyper/pull/4552)
  Before:
  ```vyper
  bitwise_and(x, y)
  bitwise_or(x, y)
  bitwise_xor(x, y)
  bitwise_not(x)
  shift(x, n)
  ```
  After:
  ```vyper
  x & y
  x | y
  x ^ y
  ~x
  x << n
  x >> n
  ```
- `raw_create()` builtin added for deploying arbitrary initcode. [#4204](https://github.com/vyperlang/vyper/pull/4204)
  Before:
  ```vyper
  # No direct raw initcode deployment builtin.
  ```
  After:
  ```vyper
  created: address = raw_create(initcode, arg, salt=salt, value=msg.value)
  ```
- File-level nonreentrancy pragma, `@reentrant`, and `public(reentrant(...))` added. [#4563](https://github.com/vyperlang/vyper/pull/4563)
  Before:
  ```vyper
  # Per-function opt-in only:
  @external
  @nonreentrant
  def f(): ...
  ```
  After:
  ```vyper
  #pragma nonreentrancy on
  x: public(reentrant(uint256))
  @external
  @reentrant
  def callback(): ...
  ```

### v0.4.1

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.4.1>

- `@external` became optional in `.vyi` interface files. [#4178](https://github.com/vyperlang/vyper/pull/4178)
  Before:
  ```vyper
  @external
  def totalSupply() -> uint256: view
  ```
  After:
  ```vyper
  def totalSupply() -> uint256: view
  ```
- Imported modules can be cast to interfaces with `module.__at__()`. [#4090](https://github.com/vyperlang/vyper/pull/4090)
  Before:
  ```vyper
  token: IERC20 = IERC20(addr)
  ```
  After:
  ```vyper
  token: erc20 = erc20.__at__(addr)
  ```
- Event instantiation accepts keyword arguments. [#4257](https://github.com/vyperlang/vyper/pull/4257)
  Before:
  ```vyper
  log Transfer(msg.sender, to, amount)
  ```
  After:
  ```vyper
  log Transfer(sender=msg.sender, receiver=to, value=amount)
  ```
- Native hex string literals added. [#4271](https://github.com/vyperlang/vyper/pull/4271)
  Before:
  ```vyper
  data: Bytes[2] = b"\x12\x34"
  ```
  After:
  ```vyper
  data: Bytes[2] = x"1234"
  ```
- `mana` added as an alias for `gas` in call keyword arguments. [#3713](https://github.com/vyperlang/vyper/pull/3713)
  Before:
  ```vyper
  raw_call(to, data, gas=50_000)
  ```
  After:
  ```vyper
  raw_call(to, data, mana=50_000)
  ```
- Absolute relative imports disallowed. [#4268](https://github.com/vyperlang/vyper/pull/4268)
  Before:
  ```vyper
  # Ambiguous absolute-relative import form accepted by older compilers.
  ```
  After:
  ```vyper
  from . import token
  ```

### v0.4.0

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.4.0>

- Constructor visibility changed to `@deploy`. Implemented as part of the module/ownership work. [#3729](https://github.com/vyperlang/vyper/pull/3729)
  Before:
  ```vyper
  @external
  def __init__(): ...
  ```
  After:
  ```vyper
  @deploy
  def __init__(): ...
  ```
- Named reentrancy locks removed. [#3769](https://github.com/vyperlang/vyper/pull/3769)
  Before:
  ```vyper
  @nonreentrant("lock")
  def withdraw(): ...
  ```
  After:
  ```vyper
  @nonreentrant
  def withdraw(): ...
  ```
- `_abi_encode` and `_abi_decode` renamed. [#4097](https://github.com/vyperlang/vyper/pull/4097)
  Before:
  ```vyper
  _abi_encode(a, b, method_id=method_id("f()"))
  _abi_decode(data, uint256)
  ```
  After:
  ```vyper
  abi_encode(a, b, method_id=method_id("f()"))
  abi_decode(data, uint256)
  ```
- `@internal` decorator became optional. [#4040](https://github.com/vyperlang/vyper/pull/4040)
  Before:
  ```vyper
  @internal
  def _helper(): ...
  ```
  After:
  ```vyper
  def _helper(): ...
  ```
- External calls require `extcall`, `staticcall`, or `delegatecall` keyword syntax. [#2938](https://github.com/vyperlang/vyper/pull/2938)
  Before:
  ```vyper
  token.transfer(to, amount)
  balance: uint256 = token.balanceOf(self)
  checker.ping(msg.sender)
  ```
  After:
  ```vyper
  extcall token.transfer(to, amount)
  balance: uint256 = staticcall token.balanceOf(self)
  _unused: uint256 = staticcall checker.ping(msg.sender)
  ```
- Integer division uses `//`; `/` is banned for integers. [#2937](https://github.com/vyperlang/vyper/pull/2937)
  Before:
  ```vyper
  half: uint256 = amount / 2
  ```
  After:
  ```vyper
  half: uint256 = amount // 2
  ```
- Struct literals require keyword arguments. [#3777](https://github.com/vyperlang/vyper/pull/3777), docs fix [#3792](https://github.com/vyperlang/vyper/pull/3792)
  Before:
  ```vyper
  p: Point = Point(1, 2)
  ```
  After:
  ```vyper
  p: Point = Point(x=1, y=2)
  ```
- Loop variables require type annotations. [#3596](https://github.com/vyperlang/vyper/pull/3596)
  Before:
  ```vyper
  for i in range(10):
  ```
  After:
  ```vyper
  for i: uint256 in range(10):
  ```
- `enum` keyword replaced by `flag`. [#3697](https://github.com/vyperlang/vyper/pull/3697)
  Before:
  ```vyper
  enum Role:
      ADMIN
      USER
  ```
  After:
  ```vyper
  flag Role:
      ADMIN
      USER
  ```
- Builtin constants removed. [#3350](https://github.com/vyperlang/vyper/pull/3350)
  Before:
  ```vyper
  MAX_UINT256
  ZERO_ADDRESS
  EMPTY_BYTES32
  ```
  After:
  ```vyper
  max_value(uint256)
  empty(address)
  empty(bytes32)
  ```
- Two-argument `range` with `bound=` added for dynamic ranges. [#3679](https://github.com/vyperlang/vyper/pull/3679), earlier `bound=` support [#3537](https://github.com/vyperlang/vyper/pull/3537), [#3551](https://github.com/vyperlang/vyper/pull/3551)
  Before:
  ```vyper
  for i in range(stop):
  ```
  After:
  ```vyper
  for i: uint256 in range(start, stop, bound=MAX):
  ```
- Builtin ERC interface import path changed and interface names gained `I` prefixes. [#3741](https://github.com/vyperlang/vyper/pull/3741), [#3804](https://github.com/vyperlang/vyper/pull/3804)
  Before:
  ```vyper
  from vyper.interfaces import ERC20
  token: ERC20
  ```
  After:
  ```vyper
  from ethereum.ercs import IERC20
  token: IERC20
  ```
- Module import and ownership declarations added. [#3655](https://github.com/vyperlang/vyper/pull/3655), [#3663](https://github.com/vyperlang/vyper/pull/3663), [#3729](https://github.com/vyperlang/vyper/pull/3729)
  Before:
  ```vyper
  # No reusable .vy module import syntax.
  ```
  After:
  ```vyper
  import ownable
  initializes: ownable
  uses: ownable
  ```
- Module function and interface exports added. [#3786](https://github.com/vyperlang/vyper/pull/3786), [#3919](https://github.com/vyperlang/vyper/pull/3919), transitive fix [#3888](https://github.com/vyperlang/vyper/pull/3888)
  Before:
  ```vyper
  # Imported external module functions were not exported through the importer ABI.
  ```
  After:
  ```vyper
  exports: ownable.transfer_ownership
  exports: token.__interface__
  ```
- Imported events are auto-exported in the ABI. [#3808](https://github.com/vyperlang/vyper/pull/3808)
  Before:
  ```vyper
  event Transfer:
      sender: indexed(address)
      receiver: indexed(address)
      value: uint256
  ```
  After:
  ```vyper
  import erc20

  @external
  def transfer(to: address, value: uint256):
      log erc20.Transfer(msg.sender, to, value)
  ```
- Decimal use requires the decimal feature flag. [#3930](https://github.com/vyperlang/vyper/pull/3930)
  Before:
  ```vyper
  x: decimal
  ```
  After:
  ```vyper
  x: decimal  # compile with decimals enabled
  ```
- `block.prevrandao` type/signature changed. [#3879](https://github.com/vyperlang/vyper/pull/3879)
  Before:
  ```vyper
  seed: bytes32 = block.prevrandao
  ```
  After:
  ```vyper
  seed: uint256 = block.prevrandao
  ```

## v0.3.x

### v0.3.10

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.10>

- Vyper-specific `#pragma` directives added for optimization and EVM version. [#3493](https://github.com/vyperlang/vyper/pull/3493)
  Before:
  ```vyper
  # @version ^0.3.0
  ```
  After:
  ```vyper
  #pragma version ^0.3.0
  #pragma optimize codesize
  #pragma evm-version shanghai
  ```
- Version pragma parsing relaxed. [#3511](https://github.com/vyperlang/vyper/pull/3511)
  Before:
  ```vyper
  # @version ^0.3.0
  ```
  After:
  ```vyper
  #pragma version 0.3.10
  ```
- `range(..., bound=...)` added for dynamic loop bounds. [#3537](https://github.com/vyperlang/vyper/pull/3537), [#3551](https://github.com/vyperlang/vyper/pull/3551)
  Before:
  ```vyper
  for i in range(n):
      pass
  ```
  After:
  ```vyper
  for i in range(n, bound=MAX):
  ```

### v0.3.9

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.9>

No source syntax changes identified in the release notes.

### v0.3.8

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.8>

- `transient(...)` storage qualifier added. [#3373](https://github.com/vyperlang/vyper/pull/3373)
  Before:
  ```vyper
  balances: HashMap[address, uint256]
  ```
  After:
  ```vyper
  balances: transient(HashMap[address, uint256])
  ```
- Ternary operator added. [#3398](https://github.com/vyperlang/vyper/pull/3398)
  Before:
  ```vyper
  if ok:
      return a
  return b
  ```
  After:
  ```vyper
  return a if ok else b
  ```
- Shift operators added. [#3019](https://github.com/vyperlang/vyper/pull/3019)
  Before:
  ```vyper
  shift(x, 3)
  shift(x, -3)
  ```
  After:
  ```vyper
  x << 3
  x >> 3
  ```
- `raw_revert()` builtin added. [#3136](https://github.com/vyperlang/vyper/pull/3136)
  Before:
  ```vyper
  raise "reason"
  ```
  After:
  ```vyper
  raw_revert(data)
  ```
- `send()` gas stipend can be configured. [#3158](https://github.com/vyperlang/vyper/pull/3158)
  Before:
  ```vyper
  send(receiver, amount)
  ```
  After:
  ```vyper
  send(receiver, amount, gas=50_000)
  ```
- `custom:` NatSpec tags added. [#3403](https://github.com/vyperlang/vyper/pull/3403), docs [#3404](https://github.com/vyperlang/vyper/pull/3404)
  Before:
  ```vyper
  # @notice Transfer tokens
  ```
  After:
  ```vyper
  # @custom:oz-upgrades-unsafe-allow constructor
  ```
- Unary plus disabled. [#3174](https://github.com/vyperlang/vyper/pull/3174)
  Before:
  ```vyper
  x: int128 = +y
  ```
  After:
  ```vyper
  x: int128 = y
  ```
- Numeric boolean negation blocked. [#3231](https://github.com/vyperlang/vyper/pull/3231)
  Before:
  ```vyper
  not amount
  ```
  After:
  ```vyper
  amount == 0
  ```
- Enum values became valid mapping keys. [#3256](https://github.com/vyperlang/vyper/pull/3256)
  Before:
  ```vyper
  flags: HashMap[Role, bool]
  ```
  After:
  ```vyper
  flags: HashMap[Role, bool]
  ```

### v0.3.7

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.7>

- `isqrt()` builtin added. [#3074](https://github.com/vyperlang/vyper/pull/3074), [#3069](https://github.com/vyperlang/vyper/pull/3069)
  Before:
  ```vyper
  # Hand-written integer square root helper.
  ```
  After:
  ```vyper
  root: uint256 = isqrt(x)
  ```
- `epsilon()` builtin added for decimals. [#3057](https://github.com/vyperlang/vyper/pull/3057)
  Before:
  ```vyper
  EPSILON: constant(decimal) = 0.0000000001
  ```
  After:
  ```vyper
  eps: decimal = epsilon(decimal)
  ```
- `block.prevrandao` alias added for `block.difficulty`. [#3085](https://github.com/vyperlang/vyper/pull/3085)
  Before:
  ```vyper
  block.difficulty
  ```
  After:
  ```vyper
  block.prevrandao
  ```
- Constants and immutables can be declared public. [#3024](https://github.com/vyperlang/vyper/pull/3024)
  Before:
  ```vyper
  SUPPLY: constant(uint256) = 1_000
  ```
  After:
  ```vyper
  SUPPLY: public(constant(uint256)) = 1_000
  ```

### v0.3.6

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.6>

No source syntax changes identified in the release notes.

### v0.3.5

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.5> (pulled)

- `create_from_blueprint` accepts arbitrary data. [#2996](https://github.com/vyperlang/vyper/pull/2996)
  Before:
  ```vyper
  create_from_blueprint(blueprint, code_offset=3)
  ```
  After:
  ```vyper
  create_from_blueprint(blueprint, raw_args, code_offset=3)
  ```
- `empty()` accepted in constants and default argument positions. [#3008](https://github.com/vyperlang/vyper/pull/3008)
  Before:
  ```vyper
  # empty(T) in constants/default arguments could be rejected.
  ```
  After:
  ```vyper
  DEFAULT: constant(Bytes[32]) = empty(Bytes[32])
  ```

### v0.3.4

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.4>

- `enum` custom type added. [#2874](https://github.com/vyperlang/vyper/pull/2874), [#2915](https://github.com/vyperlang/vyper/pull/2915), [#2925](https://github.com/vyperlang/vyper/pull/2925), [#2977](https://github.com/vyperlang/vyper/pull/2977)
  Before:
  ```vyper
  ROLE_ADMIN: constant(uint256) = 1
  ROLE_USER: constant(uint256) = 2
  ```
  After:
  ```vyper
  enum Role:
      ADMIN
      USER
  ```
- `_abi_decode()` builtin added. [#2882](https://github.com/vyperlang/vyper/pull/2882)
  Before:
  ```vyper
  # Decode with external helper code or manual slicing.
  ```
  After:
  ```vyper
  x: uint256 = _abi_decode(data, uint256, unwrap_tuple=False)
  ```
- `create_from_blueprint()` and `create_copy_of()` builtins added. [#2895](https://github.com/vyperlang/vyper/pull/2895)
  Before:
  ```vyper
  clone: address = create_minimal_proxy_to(template)
  ```
  After:
  ```vyper
  clone: address = create_copy_of(template)
  deployed: address = create_from_blueprint(blueprint, arg)
  ```
- `default_return_value=` external-call kwarg added. [#2839](https://github.com/vyperlang/vyper/pull/2839)
  Before:
  ```vyper
  token.transfer(to, amount)
  ```
  After:
  ```vyper
  token.transfer(to, amount, default_return_value=True)
  ```
- `min_value()` and `max_value()` builtins added for numeric types. [#2935](https://github.com/vyperlang/vyper/pull/2935)
  Before:
  ```vyper
  MAX_UINT256
  -2**127
  ```
  After:
  ```vyper
  max_value(uint256)
  min_value(int128)
  ```
- `uint2str()` builtin added. [#2879](https://github.com/vyperlang/vyper/pull/2879)
  Before:
  ```vyper
  # Custom uint-to-string helper.
  ```
  After:
  ```vyper
  return uint2str(token_id)
  ```
- `msg.data` can be passed directly to `raw_call`. [#2902](https://github.com/vyperlang/vyper/pull/2902)
  Before:
  ```vyper
  raw_call(to, slice(msg.data, 0, len(msg.data)))
  ```
  After:
  ```vyper
  raw_call(to, msg.data)
  ```
- `shift()` supports signed integers. [#2964](https://github.com/vyperlang/vyper/pull/2964)
  Before:
  ```vyper
  shift()
  ```
  After:
  ```vyper
  shift(x_int256, n)
  ```
- Dynamic arrays of strings enabled. [#2922](https://github.com/vyperlang/vyper/pull/2922)
  Before:
  ```vyper
  DynArray[String[32], 5]
  ```
  After:
  ```vyper
  names: DynArray[String[32], 5]
  ```

### v0.3.3

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.3>

- `print()` debug builtin added. [#2818](https://github.com/vyperlang/vyper/pull/2818)
  Before:
  ```vyper
  # No Vyper-level debug print builtin.
  ```
  After:
  ```vyper
  print("value", x)
  ```

### v0.3.2

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.2>

- `convert()` semantics generalized. [#2694](https://github.com/vyperlang/vyper/pull/2694)
  Before:
  ```vyper
  # Several conversions were unavailable or special-cased.
  ```
  After:
  ```vyper
  y: bytes32 = convert(x, bytes32)
  ```
- Hex and bytes literals restricted. [#2736](https://github.com/vyperlang/vyper/pull/2736), [#2782](https://github.com/vyperlang/vyper/pull/2782)
  Before:
  ```vyper
  data: Bytes[5] = "hello"
  ```
  After:
  ```vyper
  data: Bytes[5] = b"hello"
  ```
- `DynArray[T, N]` dynamic array type added. [#2556](https://github.com/vyperlang/vyper/pull/2556), [#2606](https://github.com/vyperlang/vyper/pull/2606), [#2615](https://github.com/vyperlang/vyper/pull/2615)
  Before:
  ```vyper
  uint256[5]
  ```
  After:
  ```vyper
  items: DynArray[uint256, 5]
  ```
- Full ABIv2 integer and bytes types added. [#2705](https://github.com/vyperlang/vyper/pull/2705)
  Before:
  ```vyper
  x: uint256
  y: int128
  z: bytes32
  ```
  After:
  ```vyper
  x: uint8
  y: int256
  z: bytes1
  ```
- `<address>.code` attribute added. [#2583](https://github.com/vyperlang/vyper/pull/2583)
  Before:
  ```vyper
  # Use external tooling or raw_call-based patterns.
  ```
  After:
  ```vyper
  runtime: Bytes[1024] = slice(target.code, 0, target.codesize)
  ```
- `tx.gasprice` environment variable added. [#2624](https://github.com/vyperlang/vyper/pull/2624)
  Before:
  ```vyper
  # No direct tx.gasprice source expression.
  ```
  After:
  ```vyper
  price: uint256 = tx.gasprice
  ```
- Struct constants accepted. [#2617](https://github.com/vyperlang/vyper/pull/2617)
  Before:
  ```vyper
  # Structs could not be used as constant values.
  ```
  After:
  ```vyper
  POINT: constant(Point) = Point({x: 1, y: 2})
  ```
- `skip_contract_check=` external-call kwarg added. [#2551](https://github.com/vyperlang/vyper/pull/2551)
  Before:
  ```vyper
  Foo(addr).bar()
  ```
  After:
  ```vyper
  Foo(addr).bar(skip_contract_check=True)
  ```
- `unsafe_add`, `unsafe_sub`, `unsafe_mul`, `unsafe_div` builtins added. [#2629](https://github.com/vyperlang/vyper/pull/2629)
  Before:
  ```vyper
  x + y
  ```
  After:
  ```vyper
  unsafe_add(x, y)
  ```
- Lists of any type can be loop variables. [#2616](https://github.com/vyperlang/vyper/pull/2616)
  Before:
  ```vyper
  # Loop variables over some list element types were rejected.
  ```
  After:
  ```vyper
  for item in list_of_structs:
  ```

### v0.3.1

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.1>

- `immutable(T)` declarations added. [#2466](https://github.com/vyperlang/vyper/pull/2466)
  Before:
  ```vyper
  OWNER: constant(address) = 0x0000000000000000000000000000000000000000
  ```
  After:
  ```vyper
  OWNER: immutable(address)
  def __init__():
      OWNER = msg.sender
  ```
- `uint8` type added. [#2477](https://github.com/vyperlang/vyper/pull/2477)
  Before:
  ```vyper
  decimals: uint256
  assert decimals <= 255
  ```
  After:
  ```vyper
  decimals: uint8
  ```
- `block.gaslimit` and `block.basefee` added. [#2495](https://github.com/vyperlang/vyper/pull/2495)
  Before:
  ```vyper
  # No direct source expression.
  ```
  After:
  ```vyper
  limit: uint256 = block.gaslimit
  fee: uint256 = block.basefee
  ```
- Checkable `raw_call()` added. [#2482](https://github.com/vyperlang/vyper/pull/2482)
  Before:
  ```vyper
  response: Bytes[32] = raw_call(to, data, max_outsize=32)
  ```
  After:
  ```vyper
  success: bool = raw_call(to, data, revert_on_failure=False)
  success, response = raw_call(to, data, max_outsize=32, revert_on_failure=False)
  ```
- Non-constant revert reason strings accepted. [#2509](https://github.com/vyperlang/vyper/pull/2509)
  Before:
  ```vyper
  raise "literal"
  ```
  After:
  ```vyper
  raise reason
  ```
- Slices of complex expressions accepted. [#2500](https://github.com/vyperlang/vyper/pull/2500)
  Before:
  ```vyper
  slice(tmp, start, len)
  ```
  After:
  ```vyper
  slice(raw_call(to, data, max_outsize=64), 0, 32)
  ```
- Lists of structs can be function arguments. [#2515](https://github.com/vyperlang/vyper/pull/2515)
  Before:
  ```vyper
  def f(xs: MyStruct[3]): ...
  ```
  After:
  ```vyper
  def f(xs: MyStruct[3]): ...
  ```

### v0.3.0

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.3.0>

- Any ABI-encodable type can be used as function arguments and return types. [#2154](https://github.com/vyperlang/vyper/issues/2154), [#2190](https://github.com/vyperlang/vyper/issues/2190)
  Before:
  ```vyper
  # Many complex ABI types were rejected at function boundaries.
  ```
  After:
  ```vyper
  def get() -> (uint256, DynArray[Bytes[32], 5]): ...
  ```
- Minimal proxy deterministic deployment supports CREATE2 salt. [#2460](https://github.com/vyperlang/vyper/pull/2460)
  Before:
  ```vyper
  create_minimal_proxy_to(target)
  ```
  After:
  ```vyper
  create_minimal_proxy_to(target, salt=salt)
  ```

## v0.2.x

### v0.2.16

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.16>

- `_abi_encode()` exposed as user-facing builtin. [#2401](https://github.com/vyperlang/vyper/pull/2401)
  Before:
  ```vyper
  # Manual ABI packing or external helper code.
  ```
  After:
  ```vyper
  payload: Bytes[68] = _abi_encode(to, amount, method_id=method_id("transfer(address,uint256)"))
  ```
- Event arguments can be any ABI-encodable type. [#2403](https://github.com/vyperlang/vyper/pull/2403)
  Before:
  ```vyper
  # Event argument types were more restricted.
  ```
  After:
  ```vyper
  event Batch:
      values: uint256[8]
  ```
- Interfaces can appear in lists, structs, and maps. [#2397](https://github.com/vyperlang/vyper/pull/2397)
  Before:
  ```vyper
  # HashMap[address, ERC20] and related forms rejected.
  ```
  After:
  ```vyper
  tokens: ERC20[8]
  ```
- `@nonreentrant` on constructors disallowed. [#2426](https://github.com/vyperlang/vyper/pull/2426)
  Before:
  ```vyper
  @nonreentrant("lock")
  def __init__(): ...
  ```
  After:
  ```vyper
  def __init__():
      pass
  ```

### v0.2.15

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.15>

No source syntax changes identified in the release notes.

### v0.2.14

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.14> (pulled)

No source syntax changes identified in the release notes.

### v0.2.13

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.13> (pulled)

- `abs()` builtin added. [#2356](https://github.com/vyperlang/vyper/pull/2356)
  Before:
  ```vyper
  x if x >= 0 else -x
  ```
  After:
  ```vyper
  abs(x)
  ```

### v0.2.12

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.12>

- `int256` signed integer type added. [#2351](https://github.com/vyperlang/vyper/pull/2351)
  Before:
  ```vyper
  x: int128
  ```
  After:
  ```vyper
  x: int256
  ```
- `msg.data` environment variable added. [#2343](https://github.com/vyperlang/vyper/pull/2343)
  Before:
  ```vyper
  # No direct source expression for calldata bytes.
  ```
  After:
  ```vyper
  payload: Bytes[4096] = msg.data
  ```

### v0.2.11

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.11>

No source syntax changes identified in the release notes.

### v0.2.10

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.10> (pulled)

No source syntax changes identified in the release notes.

### v0.2.9

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.9> (pulled)

- Reserved keyword checks updated. [#2286](https://github.com/vyperlang/vyper/pull/2286)
  Before:
  ```vyper
  def f(value: uint256):
      pass
  ```
  After:
  ```vyper
  def f(amount: uint256):
      pass
  ```

### v0.2.8

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.8>

- `not in` comparator added. [#2232](https://github.com/vyperlang/vyper/pull/2232)
  Before:
  ```vyper
  not (x in values)
  ```
  After:
  ```vyper
  x not in values
  ```
- `empty(...)` accepted as a function-call argument. [#2234](https://github.com/vyperlang/vyper/pull/2234)
  Before:
  ```vyper
  x: Bytes[32] = empty(Bytes[32])
  foo(x)
  ```
  After:
  ```vyper
  foo(empty(Bytes[32]))
  ```
- Empty static arrays accepted in `log` statements. [#2235](https://github.com/vyperlang/vyper/pull/2235)
  Before:
  ```vyper
  xs: uint256[3] = empty(uint256[3])
  log Values(xs)
  ```
  After:
  ```vyper
  log Values(empty(uint256[3]))
  ```
- `Bytes` variables can be mapping keys. [#2239](https://github.com/vyperlang/vyper/pull/2239)
  Before:
  ```vyper
  HashMap[Bytes[32], uint256]
  ```
  After:
  ```vyper
  values: HashMap[Bytes[32], uint256]
  ```

### v0.2.7

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.7>

No source syntax changes identified in the release notes.

### v0.2.6

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.6> (pulled)

- `uint256` can be used implicitly as the iterator type in range loops. [#2180](https://github.com/vyperlang/vyper/pull/2180)
  Before:
  ```vyper
  for i in range(10):
  ```
  After:
  ```vyper
  for i in range(10):
  uint256
  ```

### v0.2.5

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.5>

- Excessive local-variable scoping rules removed. [#2166](https://github.com/vyperlang/vyper/pull/2166)
  Before:
  ```vyper
  # Some local declarations could not be reused naturally.
  ```
  After:
  ```vyper
  if ok:
      x: uint256 = 1
  else:
      x: uint256 = 2
  ```

### v0.2.4

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.4>

No source syntax changes identified in the release notes.

### v0.2.3

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.3>

No source syntax changes identified in the release notes.

### v0.2.2

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.2>

No source syntax changes identified in the release notes.

### v0.2.1

Release: <https://github.com/vyperlang/vyper/releases/tag/v0.2.1>

`v0.2.0` was skipped on PyPI; `v0.2.0` and `v0.2.1` tags are identical in the
upstream release notes.

- `@public` and `@private` renamed. [VIP #2065](https://github.com/vyperlang/vyper/issues/2065)
  Before:
  ```vyper
  @public
  def f(): ...

  @private
  def _g(): ...
  ```
  After:
  ```vyper
  @external
  def f(): ...

  @internal
  def _g(): ...
  ```
- `@constant` renamed to `@view`. [VIP #2040](https://github.com/vyperlang/vyper/issues/2040)
  Before:
  ```vyper
  @constant
  @public
  def balance() -> uint256: ...
  ```
  After:
  ```vyper
  @view
  @external
  def balance() -> uint256: ...
  ```
- Type units removed. [VIP #1881](https://github.com/vyperlang/vyper/issues/1881)
  Before:
  ```vyper
  amount: uint256(wei)
  price: uint256(currency_value)
  ```
  After:
  ```vyper
  amount: uint256
  price: uint256
  ```
- Event declaration syntax changed to struct-like blocks. [VIP #1864](https://github.com/vyperlang/vyper/issues/1864)
  Before:
  ```vyper
  Transfer: event({_from: indexed(address), _to: indexed(address), _value: uint256})
  ```
  After:
  ```vyper
  event Transfer:
      _from: indexed(address)
      _to: indexed(address)
      _value: uint256
  ```
- `log` became a statement. [VIP #1864](https://github.com/vyperlang/vyper/issues/1864)
  Before:
  ```vyper
  log.Transfer(msg.sender, to, amount)
  ```
  After:
  ```vyper
  log Transfer(msg.sender, to, amount)
  ```
- Mapping declarations changed to `HashMap[...]`. [VIP #1969](https://github.com/vyperlang/vyper/issues/1969)
  Before:
  ```vyper
  balances: map(address, uint256)
  ```
  After:
  ```vyper
  balances: HashMap[address, uint256]
  ```
- Interfaces use `interface` instead of `contract`. [VIP #1825](https://github.com/vyperlang/vyper/issues/1825)
  Before:
  ```vyper
  contract ERC20:
      def balanceOf(a: address) -> uint256: view
  ```
  After:
  ```vyper
  interface ERC20:
      def balanceOf(a: address) -> uint256: view
  ```
- Dynamic byte and string type names capitalized. [#2080](https://github.com/vyperlang/vyper/pull/2080)
  Before:
  ```vyper
  payload: bytes[100]
  name: string[32]
  ```
  After:
  ```vyper
  payload: Bytes[100]
  name: String[32]
  ```
- Byte and string literals are no longer interchangeable. [VIP #1876](https://github.com/vyperlang/vyper/issues/1876)
  Before:
  ```vyper
  b: bytes[5] = "hello"
  s: string[5] = b"hello"
  ```
  After:
  ```vyper
  b: Bytes[5] = b"hello"
  s: String[5] = "hello"
  ```
- `assert_modifiable()` removed. [#2050](https://github.com/vyperlang/vyper/pull/2050)
  Before:
  ```vyper
  assert_modifiable(token.transfer(to, amount))
  ```
  After:
  ```vyper
  assert token.transfer(to, amount)
  ```
- Function input name `value` reserved. [VIP #1877](https://github.com/vyperlang/vyper/issues/1877)
  Before:
  ```vyper
  def pay(value: uint256): ...
  ```
  After:
  ```vyper
  def pay(amount: uint256): ...
  ```
- `slice()` start and length arguments must be `uint256`. [VIP #1986](https://github.com/vyperlang/vyper/issues/1986)
  Before:
  ```vyper
  slice(data, 0, len)
  0
  len
  int128
  ```
  After:
  ```vyper
  slice(data, convert(start, uint256), convert(length, uint256))
  ```
- `len()` returns `uint256`. [VIP #1979](https://github.com/vyperlang/vyper/issues/1979)
  Before:
  ```vyper
  n: int128 = len(data)
  ```
  After:
  ```vyper
  n: uint256 = len(data)
  ```
- External-call `value=` and `gas=` kwargs must be `uint256`. [VIP #1878](https://github.com/vyperlang/vyper/issues/1878)
  Before:
  ```vyper
  foo.pay(value=as_wei_value(1, "wei"), gas=50000)
  uint256
  ```
  After:
  ```vyper
  foo.pay(value=convert(v, uint256), gas=convert(g, uint256))
  ```
- `raw_call` kwarg `outsize` renamed to `max_outsize`. [#1977](https://github.com/vyperlang/vyper/pull/1977)
  Before:
  ```vyper
  raw_call(to, data, outsize=32)
  ```
  After:
  ```vyper
  raw_call(to, data, max_outsize=32)
  ```
- `extract32` kwarg `type` renamed to `output_type`. [#2036](https://github.com/vyperlang/vyper/pull/2036)
  Before:
  ```vyper
  extract32(data, 0, type=uint256)
  ```
  After:
  ```vyper
  extract32(data, 0, output_type=uint256)
  ```
- Public array getter indexes use `uint256`. [VIP #1983](https://github.com/vyperlang/vyper/issues/1983)
  Before:
  ```vyper
  # Getter ABI used the older integer index type.
  ```
  After:
  ```vyper
  uint256
  uint256
  ```
- Public struct getters return all struct values. [#2064](https://github.com/vyperlang/vyper/pull/2064)
  Before:
  ```vyper
  # Public struct getter returned a narrower shape.
  ```
  After:
  ```vyper
  # Public struct getter returns every member in declaration order.
  ```
- `RLPList` removed. [VIP #1866](https://github.com/vyperlang/vyper/issues/1866)
  Before:
  ```vyper
  RLPList(...)
  ```
  After:
  ```vyper
  # No replacement source syntax in Vyper.
  ```
- `empty()` builtin added. [#1676](https://github.com/vyperlang/vyper/pull/1676)
  Before:
  ```vyper
  xs: uint256[3] = [0, 0, 0]
  ```
  After:
  ```vyper
  empty(uint256[3])
  ```
- `@pure` decorator added. [VIP #2041](https://github.com/vyperlang/vyper/issues/2041)
  Before:
  ```vyper
  @view
  ```
  After:
  ```vyper
  @pure
  @external
  def add(a: uint256, b: uint256) -> uint256:
  ```
- `raise` can omit a reason string. [VIP #1902](https://github.com/vyperlang/vyper/issues/1902)
  Before:
  ```vyper
  raise "failed"
  ```
  After:
  ```vyper
  raise
  ```
- `method_id()` type argument made optional. [VIP #1980](https://github.com/vyperlang/vyper/issues/1980)
  Before:
  ```vyper
  method_id("transfer(address,uint256)", output_type=bytes4)
  ```
  After:
  ```vyper
  method_id("transfer(address,uint256)")
  ```
- `raw_call` can perform `STATICCALL`. [#1973](https://github.com/vyperlang/vyper/pull/1973)
  Before:
  ```vyper
  # No raw static call kwarg.
  ```
  After:
  ```vyper
  raw_call(to, data, max_outsize=32, is_static_call=True)
  ```
