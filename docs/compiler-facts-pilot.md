# Compiler facts pilot

Keep the validation and write pipeline. Use the source compiler to answer
specific questions that are difficult to answer from text. Of the two rules
trialed here, the custom error migration is the better first adoption.

## What worked

For `0.5.0b1` to `0.5.0b2`, `VY127` follows the public annotated AST
from a positional custom error call to its declaration. It inserts the field
names into the original source, including when the declaration is imported.
The regression fixture executes both contracts and compares the full revert
payload. This path also works with an explicit source compiler that provides
the public output format.

For managed `0.3.10`, the ordinary CLI AST identifies call nodes but omits
their resolved mutability. An optional query in the same compiler environment
joins its folded, type-checked calls to original AST nodes by ID, span, and
method. This resolved two uses of `oracle.probe()` to different call modes
in disjoint branches. A second query incurs compiler startup and analysis
cost. An explicit `0.3.10` binary uses the existing rewrite path.

Both queries operate on the captured source. Their results must agree with
the validated source hash and compiler identity. A failed query supplies no
rewrite evidence. Required compilation and artifact comparisons still decide
whether a candidate can be written.

## What the comparisons showed

The fixed and prototype implementations ran in separate processes over the
same 14 unmodified corpus inputs, with exact source compiler versions, target
`0.4.3`, and no waivers. Ten passed under each. Two had storage-layout
differences, one beta compiler rejected the layout format, and one archive
input contained an invalid dataset banner. The prototype and fixes produced
identical candidate hashes for all 14.

Seven inputs came from `0.3.10` and exercised 42 external-call keyword edits.
The tooling-oriented unfolded AST produced facts for 12 sites in three files.
The tree used by normal compilation supplied all 42 in all seven. Obtaining
a fact and having a safe place to apply it turned out to be separate problems.

| Where the call mode was applied | Result on the seven inputs | Result when another rule changed the target interface |
| --- | --- | --- |
| After earlier rules, requiring the whole file to match except its version pragma | No file used the facts | Safely fell back |
| Before all text rules, giving the source call mode priority | Used 16 sites in five files | Two valid migrations failed target compilation |
| After interface edits, matching exact named functions and checking current mutability | Used 12 sites in three files. `VYD003` warnings fell from 68 to 52 | Both migrations passed |

The last approach is on the prototype branch. In the two interaction cases,
`VY014` changed a local interface from `view` to `nonpayable` or vice
versa. The correct keyword depended on the **target** declaration. The
prototype also declines nested call spans or functions with incomplete
evidence. Existing rules handle those functions.

Two deliberately constructed examples were counted separately. The branch
binding and positional custom error migrations both went from blocked on
the fixes branch to passing compilation and all three artifact comparisons.
A separate execution test on the corpus ERC-20 contract with only its invalid
dataset banner removed confirmed that the string fix preserved its French
revert payload byte for byte.

## Adoption decision

Keep the behavior fixes independent. Adopt the public custom error rule first.
Keep the private `0.3.10` adapter experimental until a larger pinned corpus
shows enough additional successful migrations or diagnostic improvement to
justify its extra compiler query and version-specific code. A whole-program
source rewrite would also have to solve the target-interface interaction.
These results support retaining the existing writer and migrating one family
of rules at a time.

The real inputs were pinned by source SHA-256. Their prefixes, grouped by the
compiler used to validate the source, identify the cohort in the local corpus.

- `0.1.0b17` — `13e2cb06d8c6`
- `0.2.16` — `7e55804a6e65`
- `0.3.7` — `cee272d5ea57`, `61ba90a18c06`, `a5572932496c`
- `0.3.10` — `54b8e80d6843`, `0df4539e58f1`, `1d67068bfdfe`, `0109d8bbbc9a`, `32c7ba3ed8db`, `0126934971f2`, `3d56b50f60c2`
- `0.4.3` — `890eaa7c05b8`, `194e774b1924`
