# RepoAtlas

A universal code index for LLM coding agents, built so its accuracy can be
measured rather than asserted.

**Status: early. The evaluation harness works; the extractor does not exist
yet.** That order is deliberate, and the next section explains why.

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

Compare any index against a compiler-backed oracle and get a report that does
not flatter itself:

```bash
pip install -e .
scip-typescript index --output oracle.scip
repoatlas compare candidate.scip oracle.scip
```

```
# Index accuracy: repoatlas tree-sitter vs scip-typescript 0.4.0

Compared 412 files.

## Headline
| kind | precision | recall | F1 | tp | fp | fn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| definitions | 0.981 | 0.964 | 0.972 | 3204 | 62 | 120 |
| references | 0.912 | 0.671 | 0.773 | 8891 | 858 | 4359 |

Reference F1 0.773 [0.741, 0.802] (bca, 2000 resamples over files).
```

The report also carries a per-edge-kind table, a dangling-edge count, and a
confidence calibration table that checks whether an edge claiming 0.95
confidence is actually right 95% of the time. No other tool in this space
publishes that last one, and it is what turns a resolution cascade from a
guess into a tuned ladder.

Before trusting the binary SCIP reader with a new indexer version:

```bash
scip print --json oracle.scip > oracle.json
repoatlas verify-oracle oracle.scip oracle.json
```

That cross-checks this project's understanding of the SCIP schema against the
SCIP CLI's own output, on the same index.

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
2. Tree-sitter extractor with per-language tag queries, scored against the
   harness from the first commit
3. Cross-file resolution cascade, its confidence tiers tuned against oracle
   calibration rather than guessed
4. SQLite storage with content-hash incremental updates
5. Ranking: personalised PageRank over the symbol graph, budgeted output
6. MCP server, a small number of tools, every output under a token budget
7. Framework plugins for the string-keyed edges no generic parser can see:
   Laravel views and routes, Vue single-file components, Blade includes

The full plan, including how tiers 3 and 4 of evaluation work and which
benchmarks cover which languages, is in [docs/evaluation.md](docs/evaluation.md).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
pytest                 # 237 tests
ruff check .
mypy
```

## Licence

MIT.
