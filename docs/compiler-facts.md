# Compiler-backed rewrites

Each source compilation produces evidence about one captured file. The rewrite
pipeline can use that evidence to choose external call syntax and to name
custom error arguments. The target compiler still validates the exact output
and the normal ABI, method identifier, and storage checks still govern writes.

## Which facts a rule can use

`RuleContext.compiler_facts` describes the original source. The engine passes
it explicitly to `apply_rules` and keeps it separate from user configuration.
`RuleContext.facts` describes the current revision. Its block-scoped bindings
distinguish identically named variables in disjoint branches and handle
continuation lines, function parameters, and loop variables. External call,
shift, and numeric conversion rules use the same lookup. A rule can consult
`prior_fixes` to confirm a transformation from an earlier pass.

| Source compiler | Evidence | Use |
| --- | --- | --- |
| Managed Vyper 0.3.10 | Folded, type-checked call sites joined to the original syntax tree by node ID, span and method | Call mode, discarded return type, and the distinction between native array members and external methods |
| Vyper 0.5.0a3 and 0.5.0b1 | Public annotated syntax tree, including imported Vyper declarations | Ordered field names for a positional custom error when targeting 0.5.0b2 or later |
| Other source versions, explicit 0.3.10 executables, or missing optional evidence | Current-source bindings and existing migration rules | Local rewrites and diagnostics for unresolved cases |

The managed worker obtains optional metadata from the same compiler object
used for the required source compile. It records the required result first.
Optional collection can fail or time out without hiding a compiler failure
or changing a successful attestation. An explicit executable can supply the
public annotated format in a separate query. Both queries must agree on the
source snapshot, compiler identity and dependency context.

## Applying a fact after the source changes

Compiler positions belong to the original file. `CompilerSource` maps
positions in the compiler's universal-newline text to the original source and
checks the source ID and bounds. For annotated custom errors it also checks the
UTF-8 byte columns at both ends of the span. The capture and write attestations
use the original byte hash, including its line endings.

A custom error call refers to its declaration by source ID and node ID. The
rule checks the callee, arity and argument boundaries before inserting each
field name. Comments, parentheses and argument evaluation order are preserved.

For an external call, the planner aligns the original and current logical
statements in the enclosing function. It checks statement order, indentation,
token boundaries, bindings, and the relevant interface declaration. Recorded
interface and immutable renames can explain identifier changes. A recognized
whole-statement replacement can be skipped, but the rule takes no source spans
from its changed interior. Nested argument calls and calls on returned
receivers get keywords and parentheses at their own boundaries. When a
function cannot be matched, the current-source rules take over.

`VYD023` flags an interface edit that could turn a read-only external call
into a state-changing call, reject a previously permitted call, or make an
entry point accept ETH. Changing a local declaration purely for conformance
is possible when the affected method has no outgoing use. The riskier edits
require `--aggressive` and remain subject to the normal write policy.

## Verification

The differential tests compare contract creation, return and revert bytes,
and storage effects. Two controls call a state-writing peer through an
interface whose mutability changes during migration. They distinguish a
successful transaction from a revert even when the artifacts match. When
working with dynamic return values, compare explicitly assigned calls as a
control. Different Vyper releases can enforce different decoding checks
independently of a rewrite.

Run the compiler and execution checks together.

```sh
uv run --locked pytest tests/test_semantic_compiler.py tests/test_semantic_migrations.py \
  tests/test_interface_call_safety.py tests/test_string_literal_differential.py tests/test_differential.py
```
