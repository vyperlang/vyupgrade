# Compiler-backed rewrites

Each source compilation produces evidence about one captured file. The rewrite
pipeline can use that evidence to choose external call syntax and to name
custom error arguments. The target compiler still validates the exact output
and the normal ABI, method identifier, and storage checks still govern writes.

## Architecture decision

Use the compiler that accepted a contract to answer questions about types and
declarations. Apply its answers as small edits to the captured source. For
example, choosing `staticcall` or `extcall` requires knowing which interface a
branch-local variable refers to. A lossless syntax tree could locate the call
and preserve its comments, but would still need that binding. Rebuilding the
contract through a target printer would also require modeling the historical
compiler's literal encodings and evaluation order.

The maintenance cost in this design is matching original compiler locations
after earlier rules edit a function. The external call planner already shares
the result of that work across call mode, parentheses, and discarded returns.
If another rule family needs the same history, move the mapping into the
shared edit machinery before adding another rule-specific matcher.

Delimiter lookups now share the balanced pairs found in each source revision.
Replacing the per-lookup scan with this index reduced median rewrite time
on Yearn's `Bootstrap.vy` from 2.36 s to
0.32 s and on Curve's `ControllerFactory.vy` from 0.93 s to 0.17 s. Both
measurements used three cold-index runs per matcher, excluded compilation,
and produced identical source, fixes and diagnostics.

## Results on pinned contracts

We compared the prototype (`2086343`) with the integrated rules (`51a7e2a`)
on 131 real contracts. The sample includes 124 Vyper 0.3.10 inputs and seven
from other releases. Both runs used the same source bytes, resolved imports,
compiler versions, target (0.4.3), and default write policy. The selection manifest's
SHA-256 is `a3becf9ffb53ee14bc32dbb0519a53c9fdb1c7d3b2b1ef63f3a7a0b9cc4e3263`.

| Measurement | Prototype | Integrated rules |
| --- | ---: | ---: |
| Real contracts allowed to write | 102 of 131 | 102 of 131 |
| 0.3.10 call keywords supplied by the compiler | 172 of 713 sites | 612 of 713 sites |
| 0.3.10 contracts using at least one compiler-supplied keyword | 39 of 124 | 79 of 124 |
| `VYD003` warnings across the 124 inputs | 675 | 7 |
| Additional source compiler invocations for private call facts | 84 | 0 |

All 131 candidate source hashes, source and target compile outcomes, and
ABI, method ID and storage comparison results matched. The seven remaining
warnings came from three inputs whose imported interfaces were missing. Both
compilers rejected them. On the original seven-contract 0.3.10 sample, the
compiler supplied all 42 call keywords, up from 12. On the wider sample,
the source-analysis fallback handled functions where the planner could not
verify the call against both revisions.

The 29 blocked real contracts included 21 with storage-layout differences,
eight with failed source compilation and four with failed target compilation.
These counts overlap. Determining whether those storage differences preserve
deployed state requires evidence about the layouts themselves.

Five execution controls were measured separately. The final rules preserved
the two branch-local call modes and the exact custom error and French revert
payloads. The default write policy blocked both cases where the prototype
changed whether a call to a storage-writing peer succeeded.

Two additional controls exercise a named constant used as a tuple index.
For a tuple containing a dynamic array and an interface, selecting the
interface makes its `append` an external call. For `(uint256, int256)`,
selecting the signed member means `convert(pair[I], uint256)` must still
revert on `-1`. Guessing the first tuple member let the prototype remove that
checked conversion. The target then returned `false` despite passing all
three artifact comparisons. A separate execution regression covers the
opposite mistake on assignment. Treating a Boolean tuple member as unsigned
introduced a checked conversion of a negative loop counter and made a
previously successful transaction revert. The shared lookup now requires
the actual tuple position to distinguish different member types.

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
| Managed Vyper 0.4.3 | Annotated syntax trees for the compiled roots and imported modules, matched to the captured files by overlay path and source digest | Exceeded interface return bounds and call sites in the captured closure |
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

`VY125` builds one plan from the original managed 0.4.3 compiler annotations
before the per-file rules start. It joins each external call to its interface
method by the compiler's source ID and declaration node ID, then maps that
identity to the captured file. The annotation of a direct return or typed
initialization must match the original declared type, including each bound.
The call's own type metadata can reflect a wider context in 0.4.3, so that
metadata alone cannot prove its old runtime decode limit. Wider and untyped
calls, unresolved static or dynamic call targets, and unavailable caller
evidence retain the original bound. Bounds can use same-file integer
constants that the compiler evidence or bounded evaluator resolves.

For an external call, the planner aligns the original and current logical
statements in the enclosing function. It checks statement order, indentation,
token boundaries, bindings, and the relevant interface declaration. Recorded
interface and immutable renames can explain identifier changes. A recognized
whole-statement replacement can be skipped, but the rule takes no source spans
from its changed interior. Nested argument calls and calls on returned
receivers get keywords and parentheses at their own boundaries. When a
function cannot be matched, the current-source rules take over.

`VYD025` flags an interface edit that could turn a read-only external call
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
