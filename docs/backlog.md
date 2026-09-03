# Backlog: what is weak, and what the theory says to do about it

An audit of the code as of 2026-09-04, with measurements where a claim
could be measured, and a survey of the token-budget literature mapped onto
this codebase. Items are ordered by what they cost the user of the index,
not by how interesting they are to build.

Numbers below marked *measured* were produced on this machine today.
Everything else is either a defect visible in the code or a result quoted
from a source listed at the end.

## 1. What is measurably wrong now

### 1.1 The token estimate is about thirty percent low for current models

*Done 2026-09-04.* `repoatlas calibrate` records a per-model constant in the
store; every tool and the map command use it; the default carries the
documented 1.3 correction; `index_status` reports the calibration state.
The budget tracker (1.4) became constant-time in the same change, and
the volatile size line moved last (3.5).

`rank/tokens.py` divides characters by 3.4. Anthropic's token-counting
documentation now states that the tokenizer introduced with Opus 4.7 and
shared by the Fable and Mythos families produces "approximately 30 percent
more tokens" for the same text than earlier models. A map fitted to 2,000
tokens is therefore roughly 2,600 on the model most likely to read it, and
every tool answer overshoots its budget by the same margin.

This is the one defect that touches the project's stated purpose directly:
a budget the index cannot keep is not a budget.

The fix is not a better constant. The counting endpoint is free, rate
limited at 5,000 requests per minute on the lowest tier, and counts under
the tokenizer of whichever model is named. So:

- `repoatlas calibrate --model <id>` renders a handful of real maps and
  outlines, counts them, and writes `chars_per_token` into the store's
  metadata keyed by model.
- Every tool and the map reader use the stored constant when present. Today
  `server/tools.py` imports the global `estimate_tokens` and cannot take a
  calibrated estimator at all, so the `make_estimator` path in `tokens.py`
  is dead code from the server's point of view.
- The default constant gets a source, and a test that fails if the
  documented ratio drifts, so the next tokenizer change is noticed.

Done means: a map rendered at budget 2,000 counts within ten percent of
2,000 on the endpoint, for the model the user names.

### 1.2 The map is recomputed from scratch on every call

*Done 2026-09-04.* Cold 5.7 s → 2.6 s, warm 1.1 s → 0.04 s, focused
7.3 s → 1.0 s first and 0.37 s after, on the 100k-symbol synthetic index.
Graph and global ranking cached per store generation; global ranks
persisted at index time; the budget search bounded by the budget instead
of rendering everything first; power iteration as numpy array operations
when available, with a test holding both walks to one answer. The cold
start is now dominated by loading 100k symbol rows (about 1 s) and the
edge rows (0.4 s); the remaining lever there is loading symbols lazily.

`repo_map` calls `store.snapshot()`, which loads every symbol and edge, then
runs PageRank, then renders. Nothing is cached between calls.

*Measured* on synthetic graphs, pure Python, this machine:

| symbols | edges | build graph | rank | render |
| ---: | ---: | ---: | ---: | ---: |
| 10,000 | 50,000 | 0.05 s | 0.14 s | 0.06 s |
| 100,000 | 500,000 | 0.89 s | 4.55 s | 1.01 s |

A repository of a hundred thousand symbols is not exotic; a mid-sized
Laravel monolith with its Vue front end is a fair fraction of that. Six
seconds per map call, repeated every time the agent asks, is the kind of
latency that makes an agent stop calling the tool, and the whole value of
the index is that it gets called.

Three changes, in order of return:

1. Cache the built `SymbolGraph` and the *global* ranking in the server
   process, invalidated when the store's toolchain stamp or file table
   changes. A focused map only changes the restart vector, and the graph
   and transition table are identical across calls.
2. Persist the global rank in the store at index time, so the first call
   after a restart is as fast as the tenth. The MCP server already opens
   the store; reading one column is cheaper than a power iteration.
3. Make `render_map` a single greedy pass rather than a binary search over
   renders (see 3.1), which removes a dozen full renders per call.

An optional `numpy` path for power iteration would take the 4.55 s to well
under a second, but the core installs with nothing and that promise is
worth more than the speedup until a real repository shows it is needed.

Done means: a second `repo_map` call on a hundred-thousand-symbol index
returns in under 200 ms, and the first after restart in under one second.

### 1.3 Re-indexing one file re-resolves the whole repository

`store/incremental.py` parses only what changed and then resolves
everything. The docstring defends this, and the defence is right about
correctness: an edge *into* a changed file can only be recomputed by
looking at the file that holds it. But the cost is every reference in the
repository, every save.

There is a narrower rule that is equally correct. An edge's target can
change only if a symbol with the same name was added, removed or moved.
So the set that needs re-resolution is

- every reference in a changed file, plus
- every reference whose name is in the set of names of symbols the change
  added, removed or moved.

That set is usually tiny. It needs the `refs` table indexed by name, which
it is not today, and a test that proves the narrowed result is identical to
the full one on the fixtures.

Done means: editing one file in a ten-thousand-file index costs resolution
proportional to that file, and the resulting edge set is byte-identical to
a full rebuild.

### 1.4 The budget tracker is quadratic

`_Budget.add` re-joins and re-estimates every accumulated line on every
call. *Measured*: 3 µs per add at 200 lines, 48 µs at 3,200. At the default
4,000-token budget this never matters, but it is a ten-line fix to keep a
running total, and a tool that is handed a larger budget should not slow
down for it.

### 1.5 No large-repository benchmark exists

*Done 2026-09-04.* `repoatlas bench`, with `--synthetic N`; the 10,000-file
result lives in `docs/benchmarks/`. Its first run found something this
audit had missed: the resolver's bottom rung was quadratic in how many
definitions share a name, and a cold index of 10,000 files took 296 s. The
choice is now made once per name and kind; cold is 12.4 s and a
one-file re-index 4.3 s, of which resolution is still all but the
parse. `get_symbol`, `find_references` and `neighbours` fetched one symbol
per edge; they now fetch in batches or count in SQL.

The performance figures in the README come from a 41-file Python project
and an 83-file TypeScript one. The project's stated requirement is large
repositories, and nothing measures one. The synthetic numbers above are
the first data point, and a synthetic graph has no file system, no git,
and no SQLite behind it.

A benchmark script that indexes a real repository of at least ten thousand
files, reports index time, store size, no-op re-index time, and the latency
of every MCP tool, and stores the result as JSON beside the commit hash,
would turn "must work on large projects" from a wish into a number. The
Laravel framework itself, with its own test suite, is a candidate the user
already has locally.

## 2. Defects found by reading

Small, certain, and each about an hour. *The first five were fixed on 2026-09-04;* the last two remain.

- **`neighbours` with an unknown edge kind raises `ValueError`**, not
  `ToolError`, so the agent sees a generic failure instead of "kinds must
  be one of ...". `EdgeKind(kind)` at `server/tools.py` needs the same
  translation the rest of the tool surface gets.
- **`search_symbols` under-reports what was left out.** It over-fetches by
  one past the window, so the trailing note reads "1 more not shown" when
  five hundred were. Either fetch a count or say "more" without a number.
- **`max_files` is applied after the budget is spent.** `_render` truncates
  the file list after the binary search chose a symbol count, so the map
  can land well under budget with files dropped that would have fit.
- **`_is_private` misses `#private` fields** in JavaScript and TypeScript,
  and counts `__init__` and every other dunder as private though they are
  the public surface of a class. Ranking noise rather than an error.
- **`index_status` prints the store size**, which changes on every
  re-index. A tool answer that differs between otherwise identical calls
  is a cache miss for the agent's whole conversation prefix (see 3.5).
  Move it last, or drop it.
- **Blade's PHP islands are not parsed.** `{{ $user->name }}` and `@php`
  blocks produce no references, so a Blade template reaches its components
  and layouts but never the PHP it calls. The `php_only` grammar the Blade
  injections expect is not in the language pack; parsing the island with
  the `php` grammar after prefixing `<?php ` would work but reintroduces
  the offset arithmetic `included_ranges` was chosen to avoid. Worth doing
  carefully, with a test that a symbol at a known line lands on that line.
- **Only one oracle fixture exists**, and it is TypeScript. Every accuracy
  number in the README is a TypeScript number. PHP and Python fixtures
  from `scip-php` and `scip-python` are the first step to any claim about
  the stack this is used on, and the `suffix` rung is already flagged
  overconfident by the one fixture there is.

## 3. What the theory says, mapped onto this code

Each item names the source, states the result in the source's own
numbers, and says what it would change here.

### 3.1 Select by coverage per token, not by rank prefix

`render_map` takes the top *n* symbols by PageRank and binary-searches *n*
against the budget. Rank is the only signal, so a file whose thirty methods
all rank well fills the budget with thirty lines of one file, and a second
file that would have told the agent something new gets nothing.

The literature calls the alternative **facility-location coverage**. PACMS
(2026) selects a set *S* under a token budget to maximise

    F(S) = Σ_i max_{j∈S} w_ij     subject to  Σ_{j∈S} tok(j) ≤ B

where the sum runs over the *whole* candidate pool, so a chosen item earns
credit for everything it is close to, and a second item close to the same
things earns almost nothing. The function is monotone submodular, so the
lazy greedy (CELF) that adds the item with the best marginal gain per token
carries a constant-factor guarantee, and it is one pass rather than a dozen
renders. PACMS reports it beating maximal marginal relevance by 8 to 12
accuracy points at a 45 percent budget, and beating top-k on answers while
trailing it on raw recall, which they read as coverage producing prompts
the model can actually use. AdaGReS (2025) reaches the same shape from the
other direction, a relevance term minus a redundancy penalty, proves it
ε-approximately submodular, and derives the trade-off weight in closed form
from the pool statistics so nothing needs tuning.

For an index with a graph rather than embeddings, similarity is structural,
and the cheap version is already principled: give the *k*-th symbol chosen
from one file, or one class, a gain of `rank / √k`. That is concave in *k*,
so the objective stays submodular, the greedy keeps its guarantee, and the
map spreads across files in proportion to how much each has to say. Cost
per symbol is its rendered line, so a long signature has to earn its
length. The effect on this repository is testable with the existing
fixtures: count distinct files and distinct classes in a 2,000-token map
before and after.

### 3.2 Let the agent say what it is looking for

*Done 2026-09-04.* `repo_map` takes `mention`: each word is matched to
symbols by name and to files by stem, case-insensitively, and the walk
restarts there with the focus weight. Words that match nothing are named
in the header. The CLI has `--mention`. Restart seeding rather than
aider's edge multiplier, so the cached graph stays immutable; the effect
on localisation is unmeasured, like every other ranking choice, until 3.8.

aider's map takes *mentioned identifiers* and *mentioned filenames* from
the conversation and steers the ranking with them: an edge whose name is
mentioned is weighted ×10, and any identifier of eight or more characters
in snake, kebab or camel case is weighted ×10 on the grounds that a long
conventional name is a specific one. Files that are mentioned share a
personalisation mass of 100 between them.

`repo_map` here takes `focus` paths and nothing else. An agent working on
"the invoice export" has no way to say so. Adding a `mention` parameter,
resolving each term to symbols through the existing search and boosting
those edges, is the single largest relevance lever this project does not
have, and it is a day's work because every piece already exists.

### 3.3 Spend more when the agent has nothing

*Done 2026-09-04.* An unsteered map defaults to 4,000 tokens, a steered
one to 2,000. Doubling rather than aider's eightfold, because Claude Code
warns at ten thousand tokens per tool result.

aider multiplies the map budget by 8 when no files are in the chat, capped
by the context window, on the reasoning that a map is worth most exactly
when the agent has nothing else to go on. `repo_map` uses a flat 2,000
tokens whether or not `focus` is given. A default that scales with the
absence of focus is one line and matches how the tool is actually used:
the first call in a session is the unfocused one.

### 3.4 Three levels of detail, not two

ContextSniper (2026) keeps a hierarchy per symbol, signature then skeleton
then body, and includes deeper levels as budget allows, reporting
competitive repair rates at a fraction of the tokens of full inclusion.
SWE-Pruner (2026) finds that pruning at *line* level keeps the code
syntactically valid far more often than pruning at token level, 87 percent
AST-correct after compression.

This index has two levels: a signature line, or `include_body` which reads
the whole thing. A **skeleton**, the body with every nested block collapsed
to its first line and `...`, is the missing middle, and tree-sitter makes
it a few lines: keep the lines that start a child node, elide the rest.
`get_symbol` gains a `detail=skeleton`, and the map can promote its top few
entries to skeletons when budget remains after signatures.

### 3.5 Keep tool output byte-stable, because the agent's cache depends on it

Every provider's prompt cache works on exact prefix match. "Don't Break the
Cache" (2026) lists what breaks it in agent loops: timestamps and ids in
tool output, reordered content, and whitespace that varies between calls.
Once a tool answer is in the conversation it is part of every later
request's prefix, so a `repo_map` that renders in a different order on the
second call costs the agent the cache on everything after it.

The map is already deterministic given the same index, and that should be
a test rather than an accident. The store size in `index_status` is the
one volatile field in the tool surface. Nothing here emits a timestamp;
nothing should start to.

### 3.6 Search should rank, not just match

`search_symbols` matches substrings and orders by exact-name, then
name-contains, then length. It does not use the graph it sits on. The
citation-grounded code comprehension study (2025) finds hybrid retrieval,
a dependence graph combined with BM25, beating either alone on citation
recall. FTS5 already provides `bm25()` over the trigram index, and the
global PageRank from 3.1 is one column away. `score = bm25 × (1 + log
rank)` costs nothing and turns "twenty things called Resolver" into the
three that matter first.

### 3.7 Prune bodies by the task, later

Squeez (2026) prunes tool output line by line conditioned on the task
description and reports substantial reduction without loss on SWE-bench.
For `get_symbol include_body` that means keeping lines that mention the
agent's terms plus the structural lines around them. It needs a
measurement to justify it and belongs after 3.4, which gives it the
structural lines to keep.

### 3.8 A benchmark for this stack, from git history

Every localisation benchmark in the literature is Python. There is none
for Laravel and none for Vue, and the ranking weights in this project are
recorded as unmeasured because of it. Git history is a ground truth that
exists for every repository: for each commit that touched *n* files, the
question "does a map focused on the commit message's identifiers include
those files" has an answer. Recall at 2,000 tokens over a few hundred
commits of a real Laravel project is a number, it is specific to the stack
this is used on, and it is what would finally settle the weights in
`pagerank.py`.

## 4. The list, in order

| # | Item | Why first | Size |
| ---: | --- | --- | --- |
| 1 | Calibrate tokens per model, thread the estimator through the tools (1.1) | The budget is wrong by a third | half a day |
| 2 | `neighbours` kind error, search "more" count, `max_files` before budget (2) | Certain, small, user-visible | an hour each |
| 3 | Determinism test for every tool answer; drop volatile size (3.5) | Protects the agent's cache | an hour |
| 4 | Cache graph and global rank in the server; persist rank in the store (1.2) | Six seconds per call on a large index | a day |
| 5 | Large-repository benchmark script with stored results (1.5) | Nothing measures the stated requirement | half a day |
| 6 | `mention` parameter on the map, aider's ×10 rule (3.2) | Largest relevance lever missing | a day |
| 7 | Greedy coverage-per-token selection replacing the binary search (3.1) | Better maps and faster maps at once | a day, plus measurement |
| 8 | Budget scales when no focus is given (3.3) | One line | minutes |
| 9 | Scoped re-resolution by changed names (1.3) | Save-time cost on large repos | a day, with the equivalence test |
| 10 | BM25 × rank in search (3.6) | Search ignores the graph it sits on | half a day |
| 11 | Skeleton detail level (3.4) | The missing middle between line and body | a day |
| 12 | PHP and Python oracle fixtures (2) | Every accuracy number is TypeScript | a day, needs the indexers |
| 13 | Blade PHP islands (2) | Templates never reach PHP | a day, offset care |
| 14 | Git-history localisation benchmark for Laravel (3.8) | The only way to settle the weights | two days |
| 15 | Task-conditioned body pruning (3.7) | After 11, and only if 14 can measure it | later |
| 16 | Running total in `_Budget` (1.4) | Quadratic, harmless today | minutes |

## Sources

- Anthropic, *Token counting*: the count endpoint is free, 5,000 requests
  per minute at the lowest tier, and models from Opus 4.7 on count about
  30 percent higher. <https://platform.claude.com/docs/en/build-with-claude/token-counting>
- PACMS: submodular context selection as a pluggable engine for LLM agents,
  facility-location objective under a token knapsack, CELF lazy greedy,
  +8 to +12 points over MMR. <https://arxiv.org/html/2606.20047>
- AdaGReS: adaptive greedy context selection via redundancy-aware scoring
  for token-budgeted RAG, closed-form trade-off, ε-approximate
  submodularity. <https://arxiv.org/abs/2512.25052>
- Citation-grounded code comprehension: hybrid graph plus BM25 retrieval,
  submodular packing versus greedy and file-limited baselines.
  <https://arxiv.org/pdf/2512.12117>
- ContextSniper: token-efficient code memory for repository-level repair,
  signature / skeleton / body hierarchy. <https://arxiv.org/pdf/2607.01916>
- SWE-Pruner: self-adaptive context pruning for coding agents, line-level
  pruning keeps 87 percent AST correctness. <https://arxiv.org/pdf/2601.16746>
- Squeez: task-conditioned tool-output pruning for coding agents.
  <https://arxiv.org/pdf/2604.04979>
- Don't Break the Cache: an evaluation of prompt caching for long-horizon
  agentic tasks. <https://arxiv.org/pdf/2601.06007>
- aider `repomap.py`: mentioned identifiers ×10, long conventional names
  ×10, `map_mul_no_files = 8`, binary search with 15 percent tolerance.
  <https://github.com/Aider-AI/aider/blob/main/aider/repomap.py>
- ARISE: repository-level graph representation and toolset for agentic
  repair, with BM25-only ablations on SWE-bench Lite.
  <https://arxiv.org/pdf/2605.03117>
