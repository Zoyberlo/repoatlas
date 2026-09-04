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
| `same_module` | A member of the type the reference sits in, or a definition in the same file | 0.90 | 1.000 |
| `unique_name` | Exactly one definition of the name in the repository | 0.75 | not exercised |
| `suffix` | Several definitions share the name; one was chosen | 0.55 | 1.000, on one edge |
| `fuzzy` | Nothing better | 0.35 | not exercised |

The fixture is two files, so treat these as a smoke test of the method
rather than as tuned values. The method is the point: when a rung's observed
precision drifts from its claim, the report says which rung and by how much,
and the number moves rather than the argument. What the tests enforce is
over-confidence only, in bins with at least five edges: a rung that
undersells itself costs a weaker rank, a rung that oversells itself costs
the agent a wrong answer it was told to trust.

Three things sit before the ladder and are not rungs, because they carry
no confidence of their own:

- **Shadowing.** A bare name used inside a function is checked against
  the function's parameters and locals (and those of the functions around
  it) before it can reach any symbol. `return new User(label)` in
  `makeUser(label)` is a use of the parameter, not of the field
  `User.label`, and an index that says otherwise is confidently wrong.
- **Receiver typing.** `user.greet()` where `user: Greets`, or where
  `user = new Admin()`, is a member of that type or of what it extends. The
  type name is resolved like any other reference and the member is looked
  up in it and up its chain; the edge takes the tier the type resolved
  at. This is what tells one `greet` from another when a repository has
  several, which the bottom rungs cannot, and it is where a Laravel
  controller's `$this->service->handle()` will be decided.
- **Derived inheritance.** A method that overrides one declared up the
  chain, and a class that implements an interface through its base, are
  facts a compiler records and nobody writes. They are derived from the
  resolved `extends` and `implements` edges after resolution, carry no
  site, and take the weakest tier on the chain they came through.

Each of those was found by an oracle fixture, not by reading: the three
fixtures under `tests/fixtures/` each caught a different one.

### A member resolves through its receiver, or not at all

The first real repository under an oracle, a Laravel application against
`scip-php`, put the two bottom rungs at 0 of 890 and 48 of 812 right, and
every wrong edge was a member reached through a variable nobody had typed.
`$order->id` is not some job's `$id`; `$order->update()` is not a controller's.
Eloquent models declare no properties, so such members are most of what a
Laravel application contains, and matching them by name across the
repository is a coin flip with the wrong coin.

The rule since then: a member is looked up in its receiver's type and what
that type extends, implements and uses. The receiver is `$this`, a local
with a declared or constructed type or one assigned from a call whose
signature declares its return, a typed property of the enclosing class, or
a class named outright. Anything else is unknown, and unknown is
unresolved: no rung below tries. Bare calls, constructions, type names and
plain values keep the full ladder, because for those a name is most of the
evidence there is. Measured after the change on the same repository:
references 0.993 precision at 1.000 recall, and no rung claiming more than
it delivers. `docs/benchmarks/oracles.md` has the tables.

### What an oracle can judge

An oracle is partial in ways that are not errors, and a comparison that
does not know them measures the oracle. Three are handled explicitly and
reported with their counts:

- **Files it never opened.** `scip-typescript` does not read `.vue`; an
  edge into one is neither confirmed nor denied, and is set aside.
- **Positions it files as local.** A definition at a position the oracle
  records as a `local` symbol is a difference of scope, not accuracy.
- **Shapes it never resolves.** For each shape of receiver, `$this->x`,
  `$typed->x`, `$untyped->x`, `Util::x`, `make()->x`, the comparison
  counts how many of the candidate's sites the oracle resolved anything
  at. Fewer than one in a hundred, over at least thirty, and the shape is
  outside the oracle's reach: `scip-php` 0.0.2 resolves `$this->x` at six
  sites in ten and a typed variable's member at none in 880. Definitions
  get the same test, which is how the methods of a Pinia store's object
  literal, 118 of them, stopped counting as false against an indexer that
  records nothing for them.

The report prints all three under *Honesty checks* and *What the oracle
can judge*. Gating by what the oracle demonstrably does is the honest
alternative to either trusting it blindly or special-casing it by name.

### Targets

The one independent published comparison of retrieval quality on a large
Java codebase, using compiled bytecode as the oracle, puts iterative grep at
mean F1 0.32, a tree-sitter graph at 0.50 and SCIP at 0.88 on twelve
transitive type-hierarchy questions, and the gap is recall (0.32 / 0.50 /
0.97) rather than precision (0.65 / 0.67 / 0.86) ([type-resolved
reachability study], recomputed from its published `results.csv`; an
earlier revision of this page quoted 0.46 / 0.67 / 0.96, which appear in
no table of the study). Those are the numbers to beat and to be honest
about: the goal is to clear grep decisively, not to pretend to match a
compiler. On the one real
repository measured so far, within what its oracles can judge, the index
sits at 0.99 on both languages; what the oracles cannot judge, members
through typed variables and the methods of object literals, is listed in
the report and in `docs/benchmarks/oracles.md` rather than claimed.

### Framework conventions, and why they are not a rung

A convention edge sits outside the cascade rather than at the top of it, and
the reason is that its evidence is a different kind. Every rung of the
cascade weighs how likely a name is to mean a particular definition. A
convention weighs nothing: `view('users.index')` names
`resources/views/users/index.blade.php` and no other file, so the only
question is whether that file is in the repository. If it is, the edge is a
fact. If it is not, there is no weaker reading to fall back on.

That second half is the part that costs something to get right. A template
name looks like an identifier if you squint. Letting `view('users.nope')`
fall through to the name cascade in a project whose controller defines
`nope` produces an edge that is confidently wrong, which is the one failure
mode this whole project exists to avoid. So the convention kinds are cut off
from the cascade entirely, with one exception: a Vue component tag *is* an
identifier, because the script block imported it. The name itself decides,
by whether it parses as one.

Detection reads a manifest rather than a directory layout, for the same
reason. `resources/views` is a folder name anyone may use; a `composer.json`
requiring `laravel/framework` is the project stating what it is.

The rules are data rather than a class per framework, and the reason is the
same one that put the harness before the extractor: the risk is not writing
a rule, it is writing thirty and never noticing that one of them stopped
matching. A JSON file can be read whole in a minute. Thirty subclasses
cannot.

Each framework gets a directory under `plugins/frameworks/`, so a listing
says what is supported without opening anything. The unit is a framework
rather than a language because a framework spans languages and a language
does not span frameworks: Laravel's conventions are written in PHP and in
Blade, and splitting them by language would put one framework's rules in
two places and let a change touch only one.

The directory also settles where code goes when data runs out, which it
will: Laravel's `route('users.show')` names a controller action through a
table built from `routes/*.php`, and no path template describes that. It
becomes a module in `frameworks/laravel/` whose plugin joins the tuple its
`plugins()` returns, and the data engine never learns it exists.

Every file under `frameworks/` is in the toolchain stamp, code included,
because a change to either kind changes edges. A package that will not
import stops the index rather than being skipped, since a framework
silently absent is a class of edges silently missing.

Two of them can claim the same reference kind. A Laravel back end with a
Quasar front end writes `component` in Blade and in Vue and means different
things, so every rule names the languages it applies to. Without that the
answer would depend on which framework happened to be listed first, which is
the sort of correctness nobody notices losing.

One ordering rule is worth stating. A convention is consulted only for a
name nothing imported. Vue's auto-import makes `<UserCard />` mean
`src/components/UserCard.vue`, but a file that explicitly imports a
`UserCard` from somewhere else means the one it imported, and preferring the
convention there would point confidently at the wrong file whenever two
components share a name.

What is not measured here is coverage: how many of a real Laravel project's
view calls use a name that is a literal string at all. Calls like
`view($template)` and `view('admin.' . $section)` are invisible to any
static reader, and the fraction they represent is unknown until a real
repository is measured. The same question applies to Vue: how often a
component tag is auto-imported rather than declared. Precision is not in
doubt, because a convention only claims an edge when the file it names
exists. Recall is.

### A dependency that segfaults

tree-sitter 0.26.0 corrupts the heap when this extractor parses a Vue
component, and the process dies later, in an unrelated file's tag query,
with a Windows access violation and no Python traceback. It reproduced on
four runs out of four; the same source on 0.25.2 ran six times out of six
without incident.

It is recorded here rather than in a comment because of what it says about
testing a parser. Every accuracy number in this document comes from
comparing outputs, and a crash produces no output to compare. Bisecting it
also punished the obvious method: deselecting individual tests appeared to
move the fault around, because the fault depends on heap layout rather than
on any one test. Adding a probe module that did nothing was enough to make
it reappear.

So the version range in `pyproject.toml` is load-bearing, and a test asserts
it is still in force. That test cannot catch the crash; nothing can, because
a segfault takes the runner with it. It can only stop a future bump from
reintroducing it silently.

### Ranking, measured at last

For its first week this section said ranking asserted more than it had
shown: the kind prior, the containment direction and the edge weights were
judgements, and settling them needed a localisation benchmark that did not
exist. It exists now, and `docs/benchmarks/weights.md` has the run.

The short version, from 283 scoreable commits of a real Laravel and Quasar
application, paired and split into halves so the numbers are checked
against commits the sweep never saw:

- **Two settings are measurably wrong**, and neither is one this project
  uses: running containment in both directions costs a tenth of the score,
  and taking the top of the ranking without spreading it across files costs
  three points.
- **The kind prior, the edge weights, the damping, the focus weight and the
  private penalty are indistinguishable from doing nothing.** Not wrong;
  not load-bearing. The ranking is robust to all five on this repository,
  which is worth knowing mostly as a statement about where effort should
  not go next.
- **The map's spread stays at 0.5.** Below it is worse; above it buys no
  symbols while emptying the map of them.

What the benchmark did settle is not a weight at all. Steering the map by
the words of the task nearly doubles the changed symbols it names, 0.136 to
0.264 (0.188 to 0.312 once the resolver stopped guessing members by name;
see `docs/benchmarks/oracles.md`), and how those words are matched to names moves the score by more
than every weight in `pagerank.py` together. The lesson is about where the
leverage in a ranking sits: not in the constants, in what the walk is
pointed at.

Building the benchmark took two corrections, both recorded in
`docs/benchmarks/weights.md`. Scoring how many of a commit's *files* the
map listed rewarded a map that lists paths and says nothing, because file
recall rises monotonically as symbols are traded for filenames; the score
is symbols now. And scoring against today's index counted a renamed file as
a miss and put symbols on the map that did not exist when the work started;
history is walked in a scratch clone with each parent tree indexed instead.

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
thousand lines, resident memory at rest.

Measured so far, on this machine:

| | 41-file Python project | 83-file TypeScript project |
| --- | ---: | ---: |
| Cold index | 0.23 s | 2.40 s |
| No-op re-index | 0.00 s | 0.00 s |
| Store size | 3.4 MiB | 2.4 MiB |
| Symbol search | under 1 ms | under 1 ms |

The no-op figures are the store's own time; the command takes about 0.4 s
end to end, nearly all of it starting Python. Store size is dominated by
the trigram search index, which is the trade for substring lookup. Run on Linux, macOS arm64, Windows,
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
