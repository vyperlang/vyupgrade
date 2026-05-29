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

**Two modes, sharply separated by whether the source compiles.**

- **Validated mode (default).** Source compiles at its source version →
  AST-first rewrite → target validated (compiles, ABI/method-id/layout diffed
  against the source baseline). This is the only path whose output is counted as
  a *validated* result, and it is where "validate both before and after" holds
  literally.
- **Degraded mode (labeled, best-effort).** Source does not compile → there is
  no baseline to diff against, so rewrites still run but are explicitly marked
  unvalidated. They are never folded into validated counts and never claim the
  before/after guarantee.

This replaces the earlier "non-compiling source is a non-starter / hard refusal"
framing. Degraded rewrites produce real value on the corpus (337 contracts have
no compiling baseline), so the capability stays — what changes is honesty of
labeling, not removal. The validation promise is scoped to validated mode, not
abandoned for it.

## Verdict

**The skeleton is right; the load-bearing analysis layer leans on the wrong
source of truth.** Regex type inference is at the end of what it can do cleanly,
but the fix is to change the *analysis source of truth* (compiler facts first,
regex as fallback) — not to rip out the architecture. The span-edit +
compiler-validation + corpus loop is working and should be kept.

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

Change the analysis *source of truth*, not the architecture. Introduce an
`AnalysisFacts` layer that carries **provenance** for every fact it exposes
(`annotated_ast` | `plain_ast` | `regex`), and have the rules consume facts
through it rather than calling `parse_source_facts` / `infer_expr_type`
directly.

1. **Facts come from the best available source, per file, with provenance.**
   - `annotated_ast` from the source compiler at the source version → real node
     types (int-vs-decimal, call mutability, loop-var types). Available 0.2.9 →
     0.4.3 (see boundary below).
   - plain `ast` from the source compiler → exact structure + spans (interfaces,
     structs, function bounds, decorators, calls) for any file that *parses*,
     even when it doesn't type-check.
   - regex facts (`parse_source_facts`) → fallback only where the compiler
     can't help: 0.2.1–0.2.8 (no `annotated_ast`) and degraded-mode files that
     don't parse/compile.
2. **Request `annotated_ast`** (today only `ast` is requested) and map AST `src`
   spans → original-text edits via the existing `node_span` / `source_segment`
   primitives in `ast_facts.py`. Keep span edits exactly as they are.
3. **Migrate the high-churn, type-sensitive rules first** — integer division,
   signed/unsigned casts, call mutability (extcall/staticcall), loop-var typing.
   These are the buckets generating the `fix: cast … / convert …` commit churn
   and the passed→failed regressions.
4. **Retire regex facts only as typed coverage is proven.** Do not delete
   `analysis.py` up front. Each rule moved to `AnalysisFacts` must hold or
   improve its corpus class (no new passed→failed) before its regex path is
   removed. The regex engine shrinks as a *consequence* of proven replacement,
   not as a precondition.
5. **Stop adding narrow regex patches** unless a patch clears a whole corpus
   class *and* is obviously lexical (token rename, pragma spelling). Type-shaped
   patches go through the typed-facts path instead.

Net effect: the safety story for *validated mode* tightens to a clean before/after
pair (source AST + artifacts vs target AST + artifacts, diffed against a
known-good baseline), while degraded mode keeps the regex fallback it genuinely
needs.

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

So the rule is sharper than "source compiles": pull `annotated_ast` from the
source compiler **run at the source version**, get source-language types, then
validate the rewritten output against the target compiler. Before and after,
both typed, both proven — in validated mode.

### Availability boundary (checked against the vyper source)

`annotated_ast` is **not** universal across the supported range:

- **0.2.1–0.2.8: unsupported.** `vyper -f annotated_ast` returns `Unsupported
  format type 'annotated_ast'`. The format was introduced in **v0.2.9**
  (confirmed via `git log -S annotated_ast` in `~/dev/vyperlang/vyper`; verified
  empirically — 0.2.1 rejects it, 0.2.16 emits it).
- **0.2.9 → 0.4.3: supported**, and carries node `type`.

This is why regex facts cannot be deleted outright: the oldest sources
(0.2.1–0.2.8) have no typed AST, and degraded-mode files have no compiling AST
at all. Typed facts are the default *where the compiler supports them*; regex is
the genuinely-required floor everywhere else.

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

The corpus (codeslaw / etherscan / fiesta) serves two distinct roles, split by
mode:

- **Validated-mode conformance suite.** Every file that compiles under its
  source compiler must round-trip through the typed pipeline and produce a
  target that also compiles with unchanged ABI / method IDs / layout. Regressions
  here (passed→failed) are bugs.
- **Degraded-mode reach metric.** Files with no compiling baseline still exercise
  the regex fallback; their results are tracked as best-effort, not as validated
  conformance. The source-compile rate measures how much of the corpus reaches
  validated mode vs degraded mode.

So the source-compile rate is a *coverage/mode-split* metric, not an
*architectural* one — it tells you how much of the corpus gets the strong
guarantee, without discarding the rest.

## Current progress (full corpus smoke)

source-compile → target-compile, after migration (1,150 contracts):

| before → after | count | mode | meaning |
| --- | ---: | --- | --- |
| passed → passed | 794 | validated | migrated and validated — the conformance baseline |
| failed → failed | 301 | degraded | source never compiled; best-effort output still broken |
| failed → passed | 36 | degraded | source never compiled; output compiles but is *unvalidated* (no baseline ABI/layout to diff) |
| passed → failed | **19** | validated | **regressions: the tool broke compiling source** |

The only bucket that is unambiguously a *defect* is **passed → failed (19)** —
working contracts the tool turned into non-compiling output. Driving this to
zero is the headline correctness target, and it is exactly the bucket
typed-AST rewrites should shrink: most of these are heuristic type
mis-inferences (wrong `//` vs `/`, wrong cast, wrong `extcall`/`staticcall`)
that the source-version `annotated_ast` would decide correctly.

The 36 failed → passed are real best-effort value (a human gets a compiling
starting point) but must *not* be banked as validated: with no compiling
baseline there is nothing to diff against, so "it compiles now" carries no
ABI/layout guarantee. They belong to degraded mode and should be reported with
that label, never counted alongside the 794.

So the honest scorecard:

- **Validated mode:** 813 source-compiling files → **794 validated migrations +
  19 regressions to fix**. Validated-mode correctness = 794 / 813 = **97.7%**,
  with the 2.3% being bugs (not missing features) and the prime target for the
  typed-facts migration.
- **Degraded mode:** 337 non-compiling files → 36 now compile (best-effort,
  unvalidated), 301 still broken. Tracked as reach, not conformance.

## Migration path

1. Add `annotated_ast` to `SOURCE_FORMATS` (it is already collected for `ast`;
   add the typed format) and tag each file validated vs degraded by whether the
   source compiles. No hard refusal — degraded files fall through to the regex
   path and are labeled.
2. Introduce the `AnalysisFacts` layer with provenance (`annotated_ast` |
   `plain_ast` | `regex`); route rules through it instead of calling
   `parse_source_facts` / `infer_expr_type` directly.
3. Reroute the high-churn type-dependent rules first (VY050 division, VY040/041
   call mutability, VY070 loop typing, signed/unsigned casts) onto typed node
   lookups via the existing `ast_facts.py` primitives. Each must hold-or-improve
   its corpus class (no new passed→failed) — verify against the smoke run.
4. Shrink `analysis.py` *as coverage is proven*, not before. Regex stays as the
   floor for 0.2.1–0.2.8 and degraded-mode files. Collapse the redundant
   per-rule re-parsing once facts are computed once and shared.
5. Keep the span-edit and compiler-validation layers as-is — they are the parts
   worth keeping.
