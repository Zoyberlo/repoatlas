# Evaluation plan

RepoAtlas is built evaluation-first. The reason is narrow and practical: an
index that asserts a call edge which does not exist is worse than having no
index, because an agent will follow the wrong edge and burn a turn on the
wrong file. Published code-graph tools report token savings and answer
quality; almost none report whether their edges are correct. That is the gap
this project starts from.

Four tiers, in order. Each is cheap enough to run often and each gates the
next.

## Tier 1: grammars and queries

Scope: does the extractor see what is in one file?

- Fixture corpora per language with tag assertions, in the format
  `tree-sitter test` uses for `test/tags/`: a caret under a token naming the
  capture it should produce.
- Golden JSON snapshots of extracted symbols and edges per fixture.
- Property tests: stable symbol ids across a no-op re-index; incremental
  update equals full rebuild; every `@definition.*` capture yields exactly
  one symbol.
- **Error rate per language.** Track the share of files containing
  tree-sitter `ERROR` nodes. Published rates across GitHub are about 16% of
  files overall, with C at 53% and Java at 0.5% ([arXiv:2509.04936]). Kotlin,
  PHP and Vue have no published figure, so measure them here.

Status: partly built. `repoatlas index --max-error-rate` reports the error
rate per language and can fail a build on it. `tests/test_extract.py`
holds per-language expectations written in Python, plus property tests for
id stability and for one symbol per definition capture. There are no golden
JSON snapshots yet and the caret-assertion fixture format is not wired up.

## Tier 2: index correctness against an oracle

Scope: does the index describe cross-file reality correctly?

This tier is implemented, and it is what the extractor is scored by. The
one-command form parses a repository and compares it in place:

```bash
repoatlas compare path/to/repo oracle.scip
```

An oracle is any producer that resolves names with a real compiler front
end. SCIP indexers are first because one
format covers TypeScript, Python, Java, Kotlin and PHP, and because their
output is a file rather than a live server, which makes runs reproducible.

Generate an oracle, then score against it:

```bash
scip-typescript index --output oracle.scip
repoatlas compare candidate.scip oracle.scip --min-f1 0.6
```

Before trusting the binary reader for a new indexer version, confirm it
agrees with the SCIP CLI:

```bash
scip print --json oracle.scip > oracle.json
repoatlas verify-oracle oracle.scip oracle.json
```

What is reported, and why each matters:

| Measure | Why it is there |
| --- | --- |
| Definition precision / recall / F1 | The floor. Missing definitions make every edge that targets them dangling. |
| Reference precision / recall / F1 | The number heuristic resolution actually loses on. |
| Per-edge-kind breakdown | A producer can be strong on containment and weak on calls; one blended figure hides that. |
| Dangling edge rate | Edges pointing at symbols the index never defined are claims it cannot support. |
| Confidence calibration | An edge claiming 0.95 should be right 95% of the time. This is what turns the resolution cascade from a guess into a tuned ladder, and it is how the tier confidences were set rather than argued over. |
| Bootstrap intervals over files | Whether a change is real. Files are the resampling unit because errors cluster: one badly parsed file emits a burst of wrong edges. |

Three comparison choices are deliberate, and the last two were forced by
the first real run against `scip-typescript` rather than anticipated:

**Matching is by location, not by name.** A tree-sitter extractor invents its
own symbol ids while `scip-typescript` emits SCIP symbol strings carrying
package and version. The only thing both agree on is where in the file
something sits, so both sides are projected into location-keyed facts.

**Overlap, not exact equality, by default.** SCIP indexers disagree about
whether a character offset counts UTF-8 bytes or UTF-16 code units, which
shifts columns on lines containing non-ASCII text. Matches that needed that
slack are counted and reported, so the tolerance cannot quietly hide a real
error. Use `--policy exact` to turn it off.

**Oracle-covered files only, by default.** Oracles are routinely partial:
`scip-typescript` indexes what `tsconfig.json` includes. Scoring against
files the oracle never looked at would count correct edges as false
positives. `--all-paths` overrides this.

**Navigable symbols only, by default.** A compiler-backed indexer records
every binding it resolves, including each function parameter, every local,
and a symbol standing for the file itself. A map for an agent has no use for
any of those. Left unfiltered this reads as a recall failure when it is a
difference in purpose, so the scope is stated explicitly and printed in the
report. `ComparisonOptions(symbol_kinds=None)` compares everything.

**Only the edge kinds the oracle emits.** SCIP records occurrences, not
structure, so it never emits a containment edge. Scored against it, every
containment edge is a false positive for a claim the oracle does not
contradict. Such kinds are counted and reported as unscored rather than
wrong; `restrict_to_oracle_edge_kinds=False` scores them anyway.

### The resolution cascade, and how its numbers were set

Cross-file resolution without a compiler is a ladder of decreasing
evidence. Each rung has a confidence, and those confidences are not
opinions: they come from running the cascade against an oracle and reading
the calibration table.

| Rung | Evidence | Claimed | Observed on the TypeScript fixture |
| --- | --- | ---: | ---: |
| `import_map` | The name was imported, and the import names a file this index covers | 0.95 | 1.000 |
| `same_module` | A member of the type the reference sits in, or a definition in the same file | 0.90 | 0.889 |
| `unique_name` | Exactly one definition of the name in the repository | 0.75 | not exercised |
| `suffix` | Several definitions share the name; one was chosen | 0.55 | 0.500 |
| `fuzzy` | Nothing better | 0.35 | not exercised |

The fixture is two files, so treat these as a smoke test of the method
rather than as tuned values. The method is the point: when a rung's observed
precision drifts from its claim, the report says which rung and by how much,
and the number moves rather than the argument.

### Targets

The one independent published comparison of retrieval quality on a large
Java codebase, using compiled bytecode as the oracle, puts iterative grep at
F1 0.46, a tree-sitter graph at 0.67 and SCIP at 0.96, with the gap widest
on transitive type-hierarchy questions ([type-resolved reachability study]).
Those are the numbers to beat and to be honest about: the goal is to clear
grep decisively, not to pretend to match a compiler.

## Tier 3: localisation metrics

Scope: given an issue, does the index put the right code in front of the
agent?

Benchmark coverage is uneven, and this matters for planning:

| Benchmark | Languages | PHP | Kotlin | Vue |
| --- | --- | --- | --- | --- |
| [SWE-Explore-Bench] | 10, line-level ground truth | ~28 | no | no |
| [SWE-bench Multilingual] | 9 | 43 | no | no |
| [Kotlin SWE-bench] | Kotlin, 106 tasks | no | yes | no |
| [Long Code Arena] | Python, Java, Kotlin | no | 50 | no |
| [Loc-Bench], [MULocBench] | Python | no | no | no |
| [SWE-PolyBench] | Java, JS, TS, Python | no | no | no |

Vue is covered by nothing, and Kotlin by 156 tasks in total. For those,
build ground truth from git history with the pipeline every one of these
datasets uses: merged pull requests linked to an issue by a closing keyword;
issue title and body only as the query; files from the diff as file-level
labels; AST nodes overlapping each hunk at the **base** commit as
function-level labels; future commits scrubbed and a post-run leak audit.
Expect roughly two files and two functions per task, with a long tail.

Report Acc@k at file, module and function level, plus SWE-Explore's
line-level recall and context efficiency under a line budget.

## Tier 4: agent-level A/B

Scope: does any of this help a real agent?

Claude Code headless supplies what is needed:

```bash
claude -p "$ISSUE" --bare --mcp-config mcp.json \
  --output-format stream-json --permission-mode dontAsk
```

`--bare` disables hooks, skills and project config so runs are reproducible.
The `system/init` event lists loaded MCP servers, so a run where the index
failed to attach can be discarded rather than silently counted as a loss.
The final result line carries token usage and cost.

Arms: grep only, the built-in LSP tool, RepoAtlas, RepoAtlas plus LSP. At
least three repeats per arm, since there is no seed flag. Metrics: resolve
rate, file Acc@5 recovered from the files the trajectory actually read,
tokens, cost, turns, wall clock.

Statistics, following the most rigorous published comparison of a structural
index against agentic grep ([Code Isn't Memory]): paired Wilcoxon or McNemar
on per-instance binary outcomes, paired BCa bootstrap intervals on per-seed
deltas, and a published ledger of excluded instances. Harness choice alone
can move scores by more than twenty points, so the harness is fixed across
arms and reported.

Keep a small subset, on the order of eighty instances, for cheap iteration.

## Performance

Measured last, because an index that is fast and wrong is worthless.
Reference points to compare against:

| System | Indexing | Query | Notes |
| --- | --- | --- | --- |
| codebase-memory-mcp | ~8k nodes/s; Linux kernel in ~3 min; ~1.2 s incremental | 0.3 ms depth-5 traversal | tree-sitter plus SQLite |
| Codanna | 75k–250k symbols/s | <0.3 ms exact lookup | Rust plus tantivy |
| PHPantom LSP | 1.5 MLOC in 5 s | n/a | compiler-backed PHP |
| scip-typescript | 1.23 MLOC in ~706 s | n/a | the precision baseline, and why it is not the default |

Track: cold indexing rate per language, incremental latency for one saved
file at p50 and p95, query latency at p50 and p99, database bytes per
thousand lines, resident memory at rest. Run on Linux, macOS arm64, Windows,
and WSL2 on both ext4 and `/mnt/c`, since the last of those cannot receive
file-watch events at all.

[arXiv:2509.04936]: https://arxiv.org/pdf/2509.04936
[type-resolved reachability study]: https://medium.com/@csharp36/where-a-semantic-code-index-finally-beats-grep-type-resolved-reachability-fd8a077da688
[SWE-Explore-Bench]: https://github.com/Qiushao-E/SWE-Explore-Bench
[SWE-bench Multilingual]: https://www.swebench.com/multilingual.html
[Kotlin SWE-bench]: https://github.com/Kotlin/kotlin-swe-bench
[Long Code Arena]: https://huggingface.co/datasets/JetBrains-Research/lca-bug-localization
[Loc-Bench]: https://huggingface.co/datasets/czlll/Loc-Bench_V1
[MULocBench]: https://arxiv.org/abs/2509.25242
[SWE-PolyBench]: https://arxiv.org/abs/2504.08703
[Code Isn't Memory]: https://arxiv.org/abs/2606.22417
