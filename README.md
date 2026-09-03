# RepoAtlas

A universal code index for LLM coding agents, built so its accuracy can be
measured rather than asserted.

**Status: early but measured. Against a real `scip-typescript` index the
extractor scores 1.00 on definitions and 0.84 on resolved references, and
every confidence rung is calibrated to within five points of what it
claims.** The evaluation harness was built first, and the next section
explains why.

## Why this exists, and why it starts with tests

Code-graph tools for coding agents arrived in force during 2025 and 2026,
several with tens of thousands of stars. They report token savings and answer
quality. Almost none report whether the edges they emit are correct.

That gap matters more than it sounds. An index that claims a call edge which
does not exist is worse than no index at all, because the agent follows it and
spends a turn in the wrong file. Meanwhile the evidence on whether such an
index helps is split, and splits along one line:

- Where a graph **replaced** reading code, quality fell. One 2026 system
  measured 0.83 against 0.92 for a plain grep-and-read agent, while using ten
  times fewer tokens.
- Where a structural index **fed** an agent that still read code, quality rose
  and cost fell: file Acc@5 of 84.5% against 44.3%, resolve rate up 7.9 points
  at p=0.003, and a lower cost per solved task.

So RepoAtlas is a layer over grep and file reading, never a replacement. The
goal is not that the model sees less code. It is that the model reads the
right code first, with fewer tokens as a consequence rather than a target.

Which means accuracy is the product, and accuracy has to be measurable from
day one. Hence: oracle harness first, extractor second.

## What works today

Parse a repository and score it against a compiler-backed oracle, in one
command:

```bash
pip install -e ".[parse]"
repoatlas compare tests/fixtures/tsdemo tests/fixtures/tsdemo/index.scip
```

```
# Index accuracy: repoatlas 0.1.0 (tree-sitter) vs scip-typescript 0.4.0

Compared 2 files.

Symbol kinds in scope: class, constant, constructor, enum, field, function,
interface, macro, method, property, trait, type_alias, variable.
Not scored, because the oracle emits no such edge: 8 edges of kind contains.

## Headline
| kind | precision | recall | F1 | tp | fp | fn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| definitions | 1.000 | 1.000 | 1.000 | 13 | 0 | 0 |
| references | 0.900 | 0.783 | 0.837 | 18 | 2 | 5 |

## Confidence calibration

Expected calibration error 0.031, worst bin 0.050, over 18 edges.

| confidence bin | edges | claimed | observed | gap |
| --- | ---: | ---: | ---: | ---: |
| 0.45 to 0.65 | 2 | 0.550 | 0.500 | +0.050 |
| 0.88 to 0.93 | 9 | 0.900 | 0.889 | +0.011 |
| 0.93 to 0.97 | 7 | 0.950 | 1.000 | -0.050 |
```

That calibration table is the part to read. Resolving a name without a
compiler is guesswork, and guesswork is fine as long as the guess says how
sure it is. An edge claiming 0.95 is right every time here; one claiming
0.55 is right about half the time. Both are honest, and an agent can weigh
them. The `index.scip` in that fixture is genuine `scip-typescript` output,
committed so CI re-checks these numbers on every platform without a Node
toolchain.

Index into a SQLite store, and the second run parses only what changed:

```bash
repoatlas index . --store .repoatlas/index.db
repoatlas search .repoatlas/index.db Resolver
```

```
changed:    0 added, 1 modified, 0 removed, 82 unchanged
parsed:     1 files
stored:     83 files, 1844 symbols, 1459 edges (2456 KiB)
elapsed:    0.04s parse + 0.08s resolve
```

A no-op re-index of an 83-file project takes 0.43 seconds end to end,
most of which is starting Python. Parsing is skipped for files whose size
and modification time are unchanged; resolution is skipped entirely when
nothing moved. Editing a tag query or upgrading tree-sitter changes what
extraction would produce, so both are hashed into the store and a mismatch
rebuilds rather than trusting stale symbols.

Parse without storing, to see what came out:

```bash
repoatlas index . --max-error-rate 0.02
```

```
files:      30
symbols:    907
references: 2298 (unresolved)
elapsed:    0.13s (232 files/s)

language      files  symbols    refs   errors
python           30      907    2298    0.0%
```

The error column is the first thing to check for a new language: published
tree-sitter error rates run from 0.2% of files in Go to 53% in C, and a
language that comes out high needs its grammar questioned before any accuracy
number from it is believed.

The report also carries a per-edge-kind table, a dangling-edge count, and a
confidence calibration table that checks whether an edge claiming 0.95
confidence is actually right 95% of the time. No other tool in this space
publishes that last one, and it is what turns a resolution cascade from a
guess into a tuned ladder.

Languages: Python, TypeScript, TSX, JavaScript, PHP. Adding one is a query
file and a registry line; the harness then says whether it worked.

Before trusting the binary SCIP reader with a new indexer version:

```bash
scip print --json oracle.scip > oracle.json
repoatlas verify-oracle oracle.scip oracle.json
```

That cross-checks this project's understanding of the SCIP schema against the
SCIP CLI's own output, on the same index.

## What the first real measurement changed

Running against genuine `scip-typescript` output immediately falsified two
assumptions, which is the argument for building the harness first:

**Producers disagree about scope, not just accuracy.** A compiler-backed
indexer records every binding it resolves, including each function parameter
and a symbol for the file itself. A map for an agent has no use for either.
Unfiltered, that difference read as 68% recall; it was not a miss. The
comparison now states its scope and the report prints it.

**An oracle does not cover every edge kind.** SCIP records occurrences, not
structure, so it never emits a containment edge. Scoring ours against it
marked every one a false positive for a claim the oracle does not contradict.
Edge kinds the oracle never emits are now reported as unscored.

Both fixes make the numbers smaller in some places and larger in others. The
point is that they now measure something.

## Design commitments

**Location-keyed comparison.** Two indexes never agree on symbol names: a
tree-sitter extractor invents ids, `scip-typescript` emits SCIP symbol strings
with package and version. They do agree on where in a file something sits, so
everything is compared as location facts.

**Confidence on every resolved edge.** Cross-file resolution without a
compiler is a cascade of decreasing certainty, from an exact import-map hit
down to a fuzzy name match. Each rung carries a confidence, and the harness
checks those numbers against an oracle instead of taking them on faith.

**Oracles are optional, not required.** The index must work with no toolchain
installed, on any language tree-sitter can parse. Where a SCIP indexer or
language server exists, it upgrades precision and doubles as ground truth for
tests. Where it does not, the heuristic cascade still runs and says how sure
it is.

**No JetBrains dependency.** Every path to compiler-grade Kotlin or PHP
resolution through JetBrains needs a running IDE or an Ultimate licence. The
open paths (`kotlin-lsp`, `scip-kotlin`, `scip-php`, PHPantom, Intelephense)
plug in as optional oracles instead.

**Runs anywhere.** The evaluation harness has zero runtime dependencies: SCIP
is decoded straight from the protobuf wire format, so the correctness suite
runs wherever Python does, on Linux, macOS, Windows and WSL.

## Roadmap

1. ~~Data model, SCIP oracle reader, comparison harness, metrics~~ done
2. ~~Tree-sitter extractor with per-language tag queries, scored against the
   harness from the first commit~~ done: Python, TypeScript, TSX, JavaScript,
   PHP, at 1.00 definition precision and recall on the TypeScript fixture
3. ~~Cross-file resolution cascade, its confidence tiers tuned against oracle
   calibration rather than guessed~~ done: five rungs from a resolved import
   down to a bare name match, at 0.90 reference precision and 0.031
   calibration error on the TypeScript fixture
4. ~~SQLite storage with content-hash incremental updates~~ done: one file,
   trigram symbol search, and a re-index that parses only what changed
5. Ranking: personalised PageRank over the symbol graph, budgeted output
6. MCP server, a small number of tools, every output under a token budget
7. Framework plugins for the string-keyed edges no generic parser can see:
   Laravel views and routes, Vue single-file components, Blade includes

The full plan, including how tiers 3 and 4 of evaluation work and which
benchmarks cover which languages, is in [docs/evaluation.md](docs/evaluation.md).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # includes parse
pytest                 # 451 tests
ruff check .
mypy
```

## Licence

MIT.
