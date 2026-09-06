# RepoAtlas

**A code index for LLM coding agents, built so its accuracy can be measured
rather than asserted.**

[![CI](https://github.com/Zoyberlo/repoatlas/actions/workflows/ci.yml/badge.svg)](https://github.com/Zoyberlo/repoatlas/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![Languages](https://img.shields.io/badge/languages-Python%20%C2%B7%20TS%20%C2%B7%20TSX%20%C2%B7%20JS%20%C2%B7%20PHP-informational)](#languages)

Point it at a repository and an agent can ask where a symbol is defined, what
uses it, and what the project is built around. Every answer fits a token
budget, and every edge says how confidently it was resolved.

**And then it was measured against `grep`, nine times, and did not win.**
That result is the most useful thing here; it is summarised
[below](#what-nine-agent-level-comparisons-found) and reported in full in
[docs/benchmarks/](docs/benchmarks/).

> **Measured, not claimed.** Against real `scip-typescript`, `scip-python`
> and `scip-php` indexes the extractor finds definitions at **1.00**
> precision in all three languages. On three production repositories it
> scores **1.000** on definitions and **0.990–0.991 F1** on references
> against `scip-php`. Every number below is checked by CI on every
> platform. The oracle harness was written before the extractor it judges,
> and [the next section](#why-this-exists-and-why-it-starts-with-tests)
> explains why.
>
> Being right turned out not to be the same as being useful, which is what
> [the agent-level comparisons](#what-nine-agent-level-comparisons-found)
> are about.

```bash
pip install -e ".[parse,serve]"
repoatlas serve /path/to/repo     # seven read-only tools over MCP
```

---

## Contents

- [Why this exists](#why-this-exists-and-why-it-starts-with-tests)
- [**What nine agent-level comparisons found**](#what-nine-agent-level-comparisons-found)
- [Scoring an index against a compiler](#scoring-an-index-against-a-compiler)
- [Indexing and searching](#indexing-and-searching)
- [Mapping a repository](#mapping-a-repository)
- [Calibrating the token budget](#calibrating-the-token-budget)
- [Serving it to an agent](#serving-it-to-an-agent)
- [What the first real measurement changed](#what-the-first-real-measurement-changed)
- [Design commitments](#design-commitments)
- [Framework conventions](#framework-conventions)
- [Roadmap](#roadmap)

## Why this exists, and why it starts with tests

Code-graph tools for coding agents arrived in force during 2025 and 2026,
several with tens of thousands of stars. They report token savings and answer
quality. Almost none report whether the edges they emit are correct.

That gap matters more than it sounds. An index that claims a call edge which
does not exist is worse than no index at all, because the agent follows it and
spends a turn in the wrong file. Meanwhile the evidence on whether such an
index helps is split, and splits along one line:

| Arrangement | Result |
| --- | --- |
| A graph that **replaced** reading code | Quality fell: 0.83 against 0.92 for a plain grep-and-read agent, while using ten times fewer tokens |
| A structural index that **fed** an agent still reading code | Quality rose and cost fell: file Acc@5 of 84.5% against 44.3%, resolve rate up 7.9 points at p=0.003 |

So RepoAtlas is a layer over grep and file reading, never a replacement. The
goal is not that the model sees less code. It is that the model reads the
right code first, with fewer tokens as a consequence rather than a target.

Which means accuracy is the product, and accuracy has to be measurable from
day one. Hence: oracle harness first, extractor second.

## What nine agent-level comparisons found

The section above argues the index should help. It was then put to an agent
and measured, and the argument did not survive contact.

| what was asked | n | result |
| --- | ---: | --- |
| localise a change from its commit subject | 24 | 0.349 against grep's 0.317, interval crosses zero |
| find every call site of a symbol | 14 | both **F1 1.000**; grep took 6.3 turns, the index 20.4 |
| ranked map against an unranked outline of the same size | 56 | never clears zero, on either of two repositories |
| review a diff **with no checkout** | 36 | **0.000 difference against grep**, at a third of the turns |

Nine head-to-head comparisons, no win. Where a shell and a checkout exist,
`grep` is not merely competitive — it is *cheaper*: 5.1 turns against 14.7,
$0.185 against $0.206.

The explanation is in the tool-call records rather than the scores. An agent
reads whatever context it is handed and then greps, so grep recovers whatever
the index would have supplied. And an agent ignores what it can: given
Serena, it called it **0 times out of 8**; given a ranked map, **0.8 times a
run**.

**What the index does win is narrower and real.** In the setting where there
is no tree to search — a review bot, a CI check, a hosted agent — it answers
identically to an agent with a checkout, at a third of the turns, and between
five and nineteen times cheaper than one that brute-forces by reading files.
On the largest repository tested, file-reading alone failed outright on 5 of
14 questions; the index answered 13.

Offline it also beats grep on what a tool *returns* — `find_references` at
1.000 F1 against `rg -w`'s 0.847, for half the tokens — but that advantage
does not survive an agent that also has grep.

| report | what it settles |
| --- | --- |
| [grep.md](docs/benchmarks/grep.md) | every head-to-head, including the map ablation that falsified the ranked map |
| [review.md](docs/benchmarks/review.md) | the one setting the index wins, across three repositories |
| [enrichment.md](docs/benchmarks/enrichment.md) | +56% and +18.6% more resolved references from a type engine |
| [phpstan.md](docs/benchmarks/phpstan.md) | what PHPStan and larastan see that no SCIP indexer does |

Read those before building on anything here.

## Scoring an index against a compiler

Parse a repository and score it against a compiler-backed oracle, in one
command:

```bash
repoatlas compare tests/fixtures/tsdemo tests/fixtures/tsdemo/index.scip
```

The report is Markdown, so it renders wherever you paste it. On the committed
TypeScript fixture it says:

> **Index accuracy: repoatlas 0.1.0 (tree-sitter) vs scip-typescript 0.4.0**
>
> Compared 2 files. Symbol kinds in scope: class, component, constant,
> constructor, enum, field, function, interface, macro, method, property,
> trait, type_alias, variable. Not scored, because the oracle emits no such edge:
> 6 edges of kind `contains`.

| kind | precision | recall | F1 | tp | fp | fn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| definitions | 1.000 | 1.000 | 1.000 | 13 | 0 | 0 |
| references | 1.000 | 1.000 | 1.000 | 23 | 0 | 0 |

Expected calibration error 0.079, worst bin 0.100, over 19 edges:

| confidence bin | edges | claimed | observed | gap |
| --- | ---: | ---: | ---: | ---: |
| 0.88 to 0.93 | 11 | 0.900 | 1.000 | -0.100 |
| 0.93 to 0.97 | 8 | 0.950 | 1.000 | -0.050 |

**That calibration table is the part to read.** Resolving a name without a
compiler is guesswork, and guesswork is fine as long as the guess says how
sure it is. Every rung is right on this fixture, which says more about the
fixture than the rungs: nineteen edges cannot tell 0.9 from 1.0, and one
edge in the 0.55 bin cannot tell it from anything. What the tests hold the
tiers to is the direction that would hurt an agent: no rung with enough
edges to judge may claim more than it delivers by over a tenth.

The `index.scip` in that fixture is genuine `scip-typescript` output,
committed so CI re-checks these numbers on every platform without a Node
toolchain. Two more fixtures do the same for the rest of the stack:

| oracle | definitions P / R | references P / R |
| --- | ---: | ---: |
| `scip-typescript` 0.4.0 | 1.00 / 1.00 | 1.00 / 1.00 |
| `scip-python` 0.6.6 | 1.00 / 1.00 | 1.00 / 1.00 |
| `scip-php` 0.0.1 | 1.00 / 1.00 | 0.93 / 1.00 |

The one PHP false positive is a call on a local assigned from a method
whose signature says what it returns; this index reads that, `scip-php`
does not. Getting here took what a tag query alone cannot do: dropping
uses of parameters and locals that shadow a symbol's name, typing a
receiver from its annotation, its `new` or the call it was assigned from,
so `user.greet()` goes to the class `user` was declared as, and deriving
the override and transitive-interface edges a compiler records but nobody
writes down. `tests/test_oracles.py` holds each language to its floor so
none of it can regress quietly.

Fixtures are where problems are found, not where an index is proven. The
same two indexers were run over a production Laravel 10 and Quasar
application, 163 PHP files and 24 JavaScript files the oracles cover:

| | definitions P / R | references P / R |
| --- | ---: | ---: |
| Laravel backend, `scip-php` | 0.996 / 1.000 | 0.993 / 1.000 |
| Quasar frontend, `scip-typescript` | 0.968 / 1.000 | 0.964 / 1.000 |

The first run against that backend scored 0.429 on references. Every
member reached through an untyped variable, `$order->id`, `$order->update()`,
had been matched by name across the repository, and the calibration table
put those rungs at 0 and 6 percent right. The rule that came out of it:
a member resolves through its receiver, or not at all. The report also
learned to ask what an oracle can judge: `scip-php` resolves nothing
through a variable, typed or not, in four thousand tries, so edges of that
shape are listed rather than scored. The whole story, before and after, is
in [docs/benchmarks/oracles.md](docs/benchmarks/oracles.md).

Those copies were each project on its own. Pointed at the monorepo they
live in, the index resolved 9% of references, because `composer.json`
was read at the repository root and the Laravel application keeps it in
`backend/`. That, and what a Laravel + Vue codebase actually looks like
(services injected in constructors and assigned to untyped properties,
Eloquent finders that return the model, Pinia stores returned by a hook,
Vue components with no class for `this` to be, routers that name pages
in `import()`), took the rate to 24% and the map from watchers to
components; the rest is the framework's, and stays unresolved rather
than guessed.

The report also carries a per-edge-kind table, a dangling-edge count, and
bootstrap confidence intervals resampled over files. Before trusting the
binary SCIP reader with a new indexer version:

```bash
scip print --json oracle.scip > oracle.json
repoatlas verify-oracle oracle.scip oracle.json
```

That cross-checks this project's understanding of the SCIP schema against the
SCIP CLI's own output, on the same index.

## Indexing and searching

Index into a SQLite store; the second run parses only what changed.

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

A no-op re-index of an 83-file project takes 0.43 seconds end to end, most of
which is starting Python. Parsing is skipped for files whose size and
modification time are unchanged; resolution is skipped entirely when nothing
moved. Editing a tag query or upgrading tree-sitter changes what extraction
would produce, so both are hashed into the store and a mismatch rebuilds
rather than trusting stale symbols.

Parsing without storing reports what came out:

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

<a name="languages"></a>
**Languages:** Python, TypeScript, TSX, JavaScript, PHP. Adding one is a query
file and a registry line; the harness then says whether it worked.

### Where the tokens go

A map spends its budget; the skeleton it chooses from costs what it
costs, and `tokens` says where:

```bash
repoatlas tokens .repoatlas/index.db --depth 2
```

```
skeleton of 363 files, 3333 symbols: 59277 tokens (estimated)

   31188  52.6%  backend/  (272 files, 1310 symbols)
     24022  40.5%  app/  (153 files, 1083 symbols)
        6518  11.0%  Http/  (33 files, 287 symbols)
        5519   9.3%  Console/  (34 files, 211 symbols)
        4201   7.1%  Services/  (24 files, 164 symbols)
      5191   8.8%  database/  (64 files, 200 symbols)
        4846   8.2%  migrations/  (59 files, 184 symbols)
   ...
```

That is the real application: a twelfth of its skeleton is migrations,
which no task ever asks for, and the tree is how one finds that out.

Heaviest first, measured with the store's own estimator, so the numbers
are the ones a budget is spent against. With `--max-total N` it is a CI
gate that exits 1 when the skeleton has outgrown what an agent is expected
to hold, the way `repomix --token-budget` does for a packed repository.

### Ten thousand files

`repoatlas bench` indexes a repository cold, again untouched, again with one
file changed, then times every tool, and writes the result as JSON beside
the commit it measured. `--synthetic N` generates a repository first: real
files in three languages whose imports resolve across each other, so the
walk, the grammars, the store and the resolver all do their actual work.
The latest run on 10,000 such files (79,993 symbols,
96,657 edges, 71 MiB store) is in
[docs/benchmarks/synthetic-10k.json](docs/benchmarks/synthetic-10k.json):

| what | time |
| --- | ---: |
| cold index | 12.7 s |
| re-index, nothing changed | 0.96 s |
| re-index, one file changed | 2.5 s |
| `repo_map`, first call | 1180 ms |
| `repo_map`, after that | 44 ms |
| `repo_map` with `focus` | 263 ms |
| `search_symbols` | 89 ms |
| `get_symbol` on the most used symbol | 29 ms |
| `find_references` on it | 40 ms |
| `neighbours` on it | 107 ms |

The first run of this benchmark took 296 seconds to index cold. The
resolver's bottom rung chose among every definition sharing a name, and
chose again for every reference to that name: ten thousand calls to `run`
times six thousand definitions of it was forty million candidate checks.
Choosing once per name made it twelve seconds. A synthetic repository is
what found it, because no real one on hand had six thousand `run`s, and
every real one of any size does.

A one-file re-index no longer resolves the whole repository. A reference
outside the changed file can only change its answer if a symbol with its
name was added, removed or moved, so only those references are revisited,
plus the file's own, plus the framework conventions when a file appeared
or vanished. On the synthetic index an edit that moves no definition
revisits 13 references out of 93,000 and takes 2.5 s, most of it loading
the symbol table the cascade needs and re-ranking; a test holds every
scoped update to the same edges a rebuild produces.

## Mapping a repository

Ask what the repository is built around, in three hundred tokens:

```bash
repoatlas map . --budget 300
```

```
src/repoatlas/store/database.py:
  118  class IndexStore:

src/repoatlas/model.py:
   52  class SymbolKind(enum.Enum):
  166  class Position:
  181  class SourceRange:
  196    def of(cls, start_line: int, start_char: int, end_line: int, end_char: int) -> SourceRange:
  237  class Symbol:
  325  class IndexSnapshot:

src/repoatlas/eval/facts.py:
   66  class DefFact:

src/repoatlas/eval/metrics.py:
   47  class Score:

src/repoatlas/cli.py:
   69  def build_parser() -> argparse.ArgumentParser:
  583  def main(argv: Sequence[str] | None = None) -> int:
```

Every entry is a `path:line` an agent can open directly, and the gap
between two numbers says how much was left out. Importance is personalised
PageRank over the symbol graph, so a symbol matters when the things that
refer to it matter. Pointing it at what you are working on changes the
answer:

```bash
repoatlas map . --budget 300 --focus src/repoatlas/store/database.py
```

That returns the structure of the store and the one file it leans on, rather
than an overview of the project. It is the same mechanism aider uses, moved
from files to symbols so a large class does not have to be included whole.

## Calibrating the token budget

Every budget in this project is enforced by an estimate, and the estimate
is a constant that moved by a third between model generations: Anthropic's
token-counting documentation states that models from Opus 4.7 on, which
includes the Fable and Mythos families, count about 30 percent higher than
earlier ones for the same text. A map fitted with the old constant is not a
2,000-token map on the model reading it.

The default now carries that correction. The measurement replaces it:

```bash
repoatlas calibrate .repoatlas/index.db --model claude-fable-5-1
```

That renders a few real answers, counts them on the provider's free
counting endpoint under the named model's tokenizer, and records the
constant in the index. Every tool over that index, and the `map` command,
then estimate with it. `index_status` says which model the index is
calibrated for, or that it is not.

### How the budget is spent

Selection is a greedy over gain per token. Rank is the gain, the rendered
line is the cost, and the *k*-th symbol taken from one file is worth its
rank divided by √k, so a file whose thirty methods all rank well cannot
fill the map with thirty lines of itself while a file that would have told
the agent something new gets nothing. That is coverage with diminishing
returns, a submodular objective, and the lazy greedy that maximises it
carries the usual constant-factor guarantee; the budgeted context-selection
literature (PACMS, AdaGReS) reaches the same shape from embeddings.

On this repository at 2,000 tokens it shows 37 files and 69 types where
the rank prefix showed 27 and 58, for the same tokens. The exponent is a
judgement, `--spread 0` gives the plain prefix back, and the localisation
benchmark in the backlog is what would settle it.

### Steering by name

A task description names things before any file is open: "the invoice
export" is `InvoiceExporter` and `invoice.ts`. `--mention` takes those
words, matches each to symbols by name and to files by stem, and restarts
the walk there, so the map is drawn around them:

```bash
repoatlas map . --budget 300 --mention Resolver --mention cascade
```

Words that match nothing are named in the map's header rather than
silently ignored, since a global map looks exactly like an answer to the
question that was asked. The MCP tool takes the same `mention` list, and
spends 4,000 tokens when nothing steers it and 2,000 when something does:
a map is worth most on the first call of a session, when the agent has
nothing else to go on.

### Does the map find the right files?

`repoatlas localize` answers with the repository's own history. Every
commit that touched a few files was, for its author, a localisation task,
and its message is what the author knew before finding them. For each
recent commit the tool draws a map around the words of the message and
checks whether the files the commit touched are on it. This is the
number every ranking judgement in `pagerank.py` was waiting for, and it
is specific to whatever repository it is run on.

For each commit it checks out the **parent** tree in a scratch clone,
indexes it, draws a map at the usual budget around the words of the commit
subject, and asks how many of the symbols that commit went on to change the
map names. The repository being read is never touched.

It has been run on one: a private Laravel 10 and Quasar application, 1,568
commits over three years, indexed as 363 files and 4,340 symbols. Of its
400 most recent qualifying commits, 283 changed a symbol the index holds:

| map | recall of changed symbols | recall of changed files |
| --- | ---: | ---: |
| plain | 0.14 | 0.52 |
| steered by the subject's words | **0.26** | 0.60 |

That measurement has changed the code twice. Steering originally matched a
word only against a *whole* symbol name and scored *below* not steering at
all: "clients report fix" found a local variable spelled `client` and
dragged the map away from `ClientsReportExport`. Matching a word against
the *parts* of a name fixed it —
[docs/benchmarks/steering.md](docs/benchmarks/steering.md) has the nine
variants tried.

Then the same benchmark was turned on the ranking weights themselves.
Every constant in `rank/pagerank.py` was a judgement; a paired sweep over
those 283 commits, split so the winner is checked on commits it never saw,
found that only two settings are measurably wrong and neither is one this
project uses, and that the kind prior, edge weights, damping, focus weight
and private penalty are all indistinguishable from doing nothing. The
weights did not change; they stopped being guesses.
[docs/benchmarks/weights.md](docs/benchmarks/weights.md) has the run, and
the two corrections the benchmark itself needed first.

One caveat that cannot be engineered away: a commit message is a generous
proxy for a task, written afterwards by the person who did the work.

A ranking has to beat not ranking at all, and this project once lost to
a baseline that naive: file recall rewarded a map of bare filenames. So
`localize` scores two baselines beside the plain and the steered map, at
the same budget: the skeleton of the whole repository in path order,
which is what `repomix --compress` hands a model, and grep for the task's
own words, busiest file first, which is what an agent with no index does
first. Every arm is scored the same way, a line crediting the innermost
symbol around it, so a grep hit inside a method counts as finding it.

| map, 281 commits, 2,000 tokens | symbol recall | file recall |
| --- | ---: | ---: |
| skeleton prefix (Repomix-shaped) | 0.028 | 0.032 |
| grep for the task's words | 0.111 | 0.222 |
| ranked, plain | 0.221 | 0.554 |
| ranked, steered by the same words | 0.309 | 0.579 |

The steered map finds nearly three times what grep finds for the same
words in the same tokens, and the unsteered map twice. What that is
worth to an agent that can grep as much as it likes is a different
question, and `repoatlas agentbench` is built to ask it: the same tasks
through Claude Code headless with and without the index, scored the same
way, with tokens, turns and cost beside the recall. It waits on a
logged-in Claude Code to run.

## Serving it to an agent

```bash
repoatlas serve /path/to/repo
```

Add it to Claude Code with a `.mcp.json` entry:

```json
{
  "mcpServers": {
    "repoatlas": {
      "command": "repoatlas",
      "args": ["serve", "."]
    }
  }
}
```

Seven tools over stdio, every one read-only and every answer bounded:

| Tool | The question it answers |
| --- | --- |
| `repo_map` | What is this project built around? |
| `search_symbols` | Where is this defined, and does it matter? |
| `get_symbol` | What is this, and what uses it? |
| `find_references` | What breaks if I change this? |
| `neighbours` | What does this depend on, one hop out? |
| `file_outline` | Is this file worth opening, and which lines? |
| `index_status` | How much of the repository does this cover? |

Seven rather than thirty because every schema is loaded into the model's
context on every turn. Each description says *when* to use the tool, not only
what it returns: agents handed a graph tool never called it in fifty-eight
percent of trials, defaulting to grep, so a description that merely describes
is a tool nobody uses. The server instructions say plainly where grep is
still the better choice.

Every answer is trimmed to a token budget and says what it left out. Claude
Code truncates a tool result at 25,000 tokens, and a result cut by the client
is cut at a point the agent cannot see.

### What a map call costs on a large index

Measured on a synthetic index of 100,000 symbols in 5,000 files with
500,000 call edges, on one laptop:

| call | before | now |
| --- | ---: | ---: |
| first `repo_map` after the server starts | 5.7 s | 2.6 s |
| every `repo_map` after that | 1.1 s | 0.04 s |
| `repo_map` with `focus`, first time | 7.3 s | 1.0 s |
| `repo_map` with `focus`, after that | 7.3 s | 0.37 s |

Three changes did it. The server keeps the graph and the global ranking
between calls, keyed by a generation the index bumps when it changes. The
global ranking is written to the store at index time, so the first map
after a restart runs no power iteration at all. And the budget fit no
longer renders the whole ranking to discover it does not fit; every entry
costs at least a token, so the search is bounded by the budget.

The power iteration itself runs as array operations when `numpy` is
installed, which `repoatlas[serve]` brings in; without it the same walk
runs in plain Python and a test holds the two to the same answer. The
core still installs with nothing.

## What the first real measurement changed

Running against genuine `scip-typescript` output immediately falsified two
assumptions, which is the argument for building the harness first.

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

## Framework conventions

Some of the most important edges in a project are invisible to every parser
that has ever read it. `view('users.index')` is a string; to Laravel it is
`resources/views/users/index.blade.php`, and it is how a controller reaches
the thing a user actually looks at.

Each framework gets a directory under
[`plugins/frameworks/`](src/repoatlas/plugins/frameworks), holding the
naming conventions it uses as data. Adding support for one is adding a
directory; nothing central lists them, and nothing else in the index knows
any framework exists.

```
plugins/frameworks/
  laravel/
    __init__.py          what Laravel contributes
    conventions.json     its naming rules
  vue/
    __init__.py
    conventions.json
```

The unit is a framework rather than a language, because a framework spans
languages and a language does not span frameworks: Laravel's conventions
are written in PHP and in Blade, and each rule names the languages it
applies to. When data is not enough, and Laravel's named routes will not
be, the code goes in that framework's own directory and its `plugins()`
returns it alongside.

| Written | In | Resolves to |
| --- | --- | --- |
| `view('users.index')` | PHP | `resources/views/users/index.blade.php` |
| `@extends('layouts.app')` | Blade | `resources/views/layouts/app.blade.php` |
| `@include('partials.header')` | Blade | `resources/views/partials/header.blade.php` |
| `<x-forms.input />` | Blade | `resources/views/components/forms/input.blade.php` |
| `<x-data-table />` | Blade | `app/View/Components/DataTable.php` |
| `<livewire:user-list />` | Blade | `app/Livewire/UserList.php` |
| `<UserCard />`, `<user-card />` | Vue | `src/components/**/UserCard.vue` |

The Vue row is the one a bundler makes true. Quasar and its neighbours
auto-import components, so a tag with no import beside it still names a
file, at whatever depth it sits. A tag that *was* imported follows its
import instead, because a convention is for finding what nothing imported
— unless that import leads nowhere this index covers, in which case the
convention is asked after all. Path aliases are read from every project's
own `tsconfig.json` or `jsconfig.json`, not just the repository root's,
because that is where a Quasar application declares `src/*`.

Two frameworks can claim the same reference kind, and in a Laravel back end
with a Quasar front end they do: both write `component`. Each rule names the
languages it applies to, so a Blade tag is never offered to Vue's rule.

Detection reads the project's own manifest, not its directory names. A
`resources/views` folder proves nothing; a `composer.json` requiring
`laravel/framework` does, and a `package.json` requiring `quasar` does.

A convention that resolves is evidence rather than inference, so it carries
the same confidence as a resolved import. A convention that does not resolve
produces **no edge at all**. This is the part worth insisting on: a template
name is not an identifier, and letting `view('users.nope')` fall through to
the name cascade would match any function called `nope` and label the result
0.95.

## Roadmap

- [x] **Data model, SCIP oracle reader, comparison harness, metrics**
- [x] **Tree-sitter extractor** with per-language tag queries, scored against
      the harness from its first commit. Python, TypeScript, TSX, JavaScript
      and PHP, at 1.00 definition precision and recall on the TypeScript
      fixture
- [x] **Cross-file resolution cascade**, its confidence tiers tuned against
      oracle calibration rather than guessed. Five rungs from a resolved
      import down to a bare name match, at 0.90 reference precision and 0.031
      calibration error
- [x] **SQLite storage** with content-hash incremental updates: one file,
      trigram symbol search, and a re-index that parses only what changed
- [x] **Ranking**: personalised PageRank over the symbol graph, with a binary
      search that fits a map to a token budget. **Since falsified as an agent
      feature**: over 56 paired tasks on two repositories a ranked map never
      beat an unranked outline of the same size. It still holds up offline,
      where an unranked outline of a 1,832-file repository scores 0.000 at
      every budget to 32,000 tokens and the ranked map scores 0.345 — the
      agent simply greps its way past the difference
- [x] **MCP server**: seven read-only tools over stdio, each answer budgeted
- [x] **Framework conventions as data**: Laravel views, layouts, includes,
      Blade and Livewire components, and Vue components a bundler
      auto-imports. Adding a framework is adding a directory
- [x] **Enrichment from a type engine**: a producer-neutral facts format and
      a PHPStan collector, worth +56% and +18.6% more resolved references on
      two applications, corroborated 94.7–99.0% and contradicting the
      compiler-backed oracle nowhere
- [x] **Four benchmark harnesses** — `localize`, `agentbench`, `sitebench`,
      `reviewbench` — with paired bootstrap intervals and pre-registered
      thresholds
- [ ] **A deployment path for the one setting that wins**: building an index
      without a working tree, moving a store between machines, refreshing it
      from a diff. Until that exists, "review with no checkout" is a
      benchmark result rather than a capability
- [ ] **Documentation layer**: per-file summaries anchored to symbol ranges,
      cached by content hash and measured against the same harness

Known gaps, stated rather than buried, largest first:

- **The one measured win cannot be deployed.** It needs a store built and
  served without a checkout, and nothing here builds one from anything but a
  working tree.
- **That win is PHP-only.** Three repositories, all Laravel, 14 questions
  each. It should be confirmed in another language before it is built on.
- **The ranked map does not earn its place** at the agent level and should
  stop being described as the reason this exists.
- Laravel route and config names need tables nothing yet reads, and Kotlin is
  not supported.

The full plan, including how tiers 3 and 4 of evaluation work and which
benchmarks cover which languages, is in [docs/evaluation.md](docs/evaluation.md).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # includes parse
pytest                 # 635 tests
ruff check .
mypy
```

CI runs the suite on Linux, macOS and Windows, against Python 3.11 and 3.13.

## Licence

MIT.
