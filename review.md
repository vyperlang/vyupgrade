# vyupgrade architecture review

## What the tool actually is

`vyupgrade` is a Vyper codemod that upgrades contracts across language versions
(0.2.x → 0.4.3). The implementation has three layers:

1. **Analysis** (`analysis.py`, ~595 lines) — a hand-rolled, regex-based parser
   (`parse_source_facts`) that reconstructs interfaces, structs, function
   boundaries, decorators, locals, loop vars, and a heuristic type-inference
   engine (`infer_expr_type`).
2. **Rewriting** (`rules.py`, ~3590 lines) — ~49 rule functions doing
   span-based text edits, masking out comments/strings via
   `code_mask`/`span_is_code`.
3. **Validation** (`compiler.py`) — runs the real source and target compilers
   via isolated `uv run --with vyper==X`, gates writes on target-compile
   success, and diffs ABI / method IDs / storage layout.

## Design decision (settled)

**Non-compiling / broken source is a non-starter.** The tool must be able to
validate both the before *and* the after — meaning a clean source compile is a
hard precondition, not a best-effort nicety. A file whose source does not
compile under its declared/inferred compiler is refused (diagnostic + exit 3),
not rewritten on a degraded basis.

This decision collapses the central trade-off below: if source must compile,
the compiler's typed AST is *always* available, and the regex type-inference
engine has no remaining justification — even as a fallback.

## Verdict

**The skeleton is right; the load-bearing analysis layer is built on the wrong
foundation — and given the decision above, the wrong foundation is now also an
unnecessary one.**

## What's genuinely well-chosen

- **Span-based text edits over AST-rewrite-and-reprint.** Correct, and matches
  how pyupgrade itself works — it's the only way to preserve comments and
  formatting. The `code_mask` machinery is the right primitive.
- **Compiler-as-oracle validation.** The target-compile gate plus
  ABI/method-id/layout diffing is the strongest part of the design and
  faithfully implements the plan's core promise ("never silently apply a
  rewrite it cannot validate"). Because Vyper 0.4 turns most type errors into
  *compile* errors (`/` on ints, `staticcall` on a nonpayable fn), a wrong
  rewrite usually fails to compile and is blocked. The architecture is honest
  about what it can't prove (semantic equivalence).
- **Isolated compiler management** via `uv run --no-project --with vyper==…`
  directly addresses the plan's #1 risk (old compilers won't run on modern
  Python).
- **Version-gated rules** (`RULE_CHANGES` + `crosses`/`target_at_least`) is a
  clean way to make one rule set span 0.2 → 0.4.

## The central architectural problem

The plan (§9.3) explicitly said: *"Use a hybrid parser… Use Vyper compiler
outputs for AST/type info… This avoids the trap of trying to maintain a second
Vyper parser."*

**The implementation built the second parser anyway** — and then under-uses the
compiler AST it already pays to collect:

- `compile_source_file` already requests `ast` and feeds it into config. It is
  used by **exactly one rule** (integer constants). `annotated_ast` — the thing
  that carries *types*, which is what VY040/041/050/070 actually need — **is
  never requested at all**.
- Meanwhile the regex parser is called **17 times** across the rules, and the
  type-dependent rules lean entirely on `infer_expr_type`, a regex heuristic.
- `ast_facts.py` already contains the right primitives (`SourceSpan`,
  `node_span`, `source_segment`, `calls`, `iter_nodes`) — the foundation for
  the principled approach is *sitting there built*, and rules route around it.

The git log is the tell:

```
fix: convert signed negations assigned to uint
fix: cast signed values in uint array indices
fix: cast constants in nested uint arguments
fix: cast constants in signed comparisons
fix: respect nearest loop types for call casts
```

This is the textbook signature of a heuristic type system being patched
case-by-case — re-deriving, badly, the type lattice the compiler hands you for
free in `annotated_ast`. Each fix is another special case bolted onto regex
inference. That's an unbounded tail of edge cases, and it's why `rules.py` is
3,590 lines.

## Recommended direction adjustment

Because clean source compilation is now a precondition, drop the "regex-primary
with compiler fallback" framing entirely. The target is **AST-first, full
stop**:

1. **Source must compile or the file is refused.** Make a successful source
   compile the gate that *precedes* any rewrite, mirroring the existing target
   gate. No source AST → no rewrite → diagnostic (`VYD006`, exit 3). This is
   what makes "validate both before and after" literally true.
2. **Drive every type-dependent decision from the *source-version*
   `annotated_ast`.** Request it (today only `ast` is requested), and resolve
   int-vs-decimal division, extcall/staticcall mutability, and loop-var types
   from real node types. Map AST `src` spans → original-text edits (you already
   have `node_span`/`source_segment`). This deletes essentially all of
   `infer_expr_type` and the long tail of per-rule casting special-cases. See
   "ast vs annotated_ast" below for why this must come from the source compiler,
   not the target.
3. **Use the typed AST for structure too** (interfaces, structs, function
   bounds, decorators, calls), replacing `parse_source_facts`. The structural
   regex parser exists only to recover what the compiler already hands you.

The regex parser's *one and only* advantage was working when the compiler
can't. The decision above removes that case from scope, so the regex analysis
engine becomes pure liability: ~600 lines of `analysis.py` plus a large
fraction of `rules.py` re-deriving — worse — information the compiler emits for
free. It should be deleted, not demoted.

This also tightens the safety story to a clean before/after pair: source AST +
artifacts (proven to compile) on one side, target AST + artifacts on the other,
with ABI / method-id / layout diffed across a known-good baseline rather than a
possibly-degraded one.

## ast vs annotated_ast (the mechanism this rests on)

Both `-f ast` and `-f annotated_ast` dump the *same* tree — identical node
types, identical `src` spans, identical structure. They differ in two ways that
decide how the pipeline must be wired. (Verified by compiling a sample contract
with vyper 0.4.3.)

### 1. When they're available

- **`-f ast`** only requires the file to **parse**. A contract containing
  `amount / 2` (illegal on uint256 in 0.4.x) still produces a full AST, because
  `/`-on-uint256 is a *type* error, not a *syntax* error.
- **`-f annotated_ast`** requires the file to **fully type-check**. The same
  broken file emits no AST at all — just `Error compiling: … (hint: did you mean
  amount // 2?)`. It only succeeds once the contract is already valid under the
  compiler being run.

### 2. What the annotated nodes carry

Once the file compiles, every expression node gains a resolved `type` (plus
reference tracking like `variable_reads`). Same `BinOp` node, two outputs:

| | plain `ast` | `annotated_ast` |
| --- | --- | --- |
| node keys | `ast_type, op, left, right, src, lineno…` | …same… **+ `type`** |
| `left` operand keys | `ast_type, id, src…` | …same… **+ `type`, `variable_reads`** |
| `left.type` | `None` | `{name: "uint256", typeclass: "integer", bits: 256, is_signed: false}` |

That `type` field is exactly what `infer_expr_type` tries to reconstruct by
hand: "this operand is uint256," "this call target is `view`," "this loop
iterates uint256."

### Why types must come from the *source* compiler

There is a chicken-and-egg trap. `annotated_ast` gives the types needed to
*decide* a rewrite (`/`→`//`, `extcall`/`staticcall`)… but it won't emit for
0.3.x source under the *target* 0.4.x compiler, because pre-migration code does
not type-check there. So the split is:

- **Plain `ast` from the source compiler** — always available for any parseable
  file; gives exact structure + spans. Replaces nearly all of
  `parse_source_facts`.
- **`annotated_ast` from the source compiler, run at the *source* version**
  (e.g. 0.3.10) — a 0.3.10 contract type-checks fine under 0.3.10, and there
  `amount / 2` resolves to `uint256 / uint256`, which *is* the signal to rewrite
  to `//`. The typed oracle is available precisely because it is compiled at its
  own version, not the target.

So the precondition is sharper than "source compiles": it is **source compiles
at its source version**, pull `annotated_ast` from that compiler for
source-language types, then validate the rewritten output against the target
compiler. Before and after, both typed, both proven.

## Smaller observations

- **Redundant re-parsing.** Each of ~49 rules re-scans the mutating buffer
  (`parse_source_facts` ×17, `code_mask` rebuilt repeatedly). Correct but
  O(rules × filesize). Fine for single files; with the corpus tooling
  (thousands of contracts) it compounds on top of 2 compiler subprocesses/file.
- **`infer_pragma` is called 3–4× per file** (CLI, compiler, rules) — minor,
  but the source version should be resolved once and threaded through.
- **Validation gate is all-or-nothing per run.** `--write` is blocked if *any*
  file's target compile fails (`any_target_failed`), so one bad file in a
  directory blocks writing the good ones. Per-file write-back (revert only the
  failures) would match the plan's "reverted writes; see report.json" intent
  better.
- **No idempotency/round-trip test** is visible despite acceptance criterion #4.
  Worth a corpus-level invariant: running twice yields no second diff.

## Consequence: what the corpus is actually for

With non-compiling source out of scope, the corpus (codeslaw / etherscan /
fiesta) stops being an argument for the regex fallback and becomes a
**conformance suite**: every file that *does* compile under its source compiler
must round-trip through the AST-first pipeline and produce a target that also
compiles with unchanged ABI / method IDs / layout. Source-compile failures are
reported and excluded, not patched around.

The source-compile success rate is now a *coverage* metric (how much of the
corpus the tool can act on), not an *architectural* one. It no longer decides
the design — it measures reach.

## Current progress (full corpus smoke)

source-compile → target-compile, after migration (1,150 contracts):

| before → after | count | meaning |
| --- | ---: | --- |
| passed → passed | 794 | in-scope, migrated successfully — the conformance baseline |
| failed → failed | 301 | out of scope (source never compiled); refuse + exclude |
| failed → passed | 36 | source never compiled, so before/after can't both be validated — out of scope under the decision, despite the apparent "improvement" |
| passed → failed | **19** | **regressions: the tool broke compiling source** |

The only bucket that is unambiguously a *defect* is **passed → failed (19)** —
working contracts the tool turned into non-compiling output. Driving this to
zero is the headline correctness target, and it is exactly the bucket
typed-AST rewrites should shrink: most of these are heuristic type
mis-inferences (wrong `//` vs `/`, wrong cast, wrong `extcall`/`staticcall`)
that the source-version `annotated_ast` would decide correctly.

The 36 failed → passed are *not* wins to bank: with a non-compiling baseline
there is no ABI/layout to diff against, so "it compiles now" is unvalidated.
Under the settled decision these are reclassified as out of scope alongside the
301.

So the honest scorecard against the "validate both before and after" promise is
**794 validated migrations, 19 known regressions to fix, 337 out of scope.**
Coverage = 794 / (794 + 19) = 97.7% of the in-scope (source-compiling) set,
with the remaining 2.3% being correctness bugs rather than missing features.

## Migration path

1. Add `annotated_ast` to `SOURCE_FORMATS` and make a successful source compile
   a hard precondition for rewriting (it is already collected; just gate on it).
2. Reroute the type-dependent rules (VY040/041, VY050, VY070, and the casting
   special-cases) onto typed AST node lookups via the existing `ast_facts.py`
   primitives.
3. Once those rules no longer call `infer_expr_type` / `parse_source_facts`,
   delete the regex analysis engine and collapse the redundant per-rule
   re-parsing.
4. Keep the span-edit and compiler-validation layers as-is — they are the parts
   worth keeping.
