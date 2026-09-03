# RepoAtlas

**A code index for LLM coding agents, built so its accuracy can be measured
rather than asserted.**

[![CI](https://github.com/Zoyberlo/repoatlas/actions/workflows/ci.yml/badge.svg)](https://github.com/Zoyberlo/repoatlas/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![Languages](https://img.shields.io/badge/languages-Python%20%C2%B7%20TS%20%C2%B7%20TSX%20%C2%B7%20JS%20%C2%B7%20PHP-informational)](#languages)

Point it at a repository and an agent can ask where a symbol is defined, what
uses it, and what the project is built around, without grepping its way
there. Every answer fits a token budget, and every edge says how confidently
it was resolved.

> **Measured, not claimed.** Against a real `scip-typescript` index the
> extractor scores **1.00** on definitions and **0.84** on resolved
> references, and every confidence rung is calibrated to within five points
> of what it claims. The oracle harness was written before the extractor it
> judges, and [the next section](#why-this-exists-and-why-it-starts-with-tests)
> explains why.

```bash
pip install -e ".[parse,serve]"
repoatlas serve /path/to/repo     # seven read-only tools over MCP
```

---

## Contents

- [Why this exists](#why-this-exists-and-why-it-starts-with-tests)
- [Scoring an index against a compiler](#scoring-an-index-against-a-compiler)
- [Indexing and searching](#indexing-and-searching)
- [Mapping a repository](#mapping-a-repository)
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
> Compared 2 files. Symbol kinds in scope: class, constant, constructor,
> enum, field, function, interface, macro, method, property, trait,
> type_alias, variable. Not scored, because the oracle emits no such edge:
> 8 edges of kind `contains`.

| kind | precision | recall | F1 | tp | fp | fn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| definitions | 1.000 | 1.000 | 1.000 | 13 | 0 | 0 |
| references | 0.900 | 0.783 | 0.837 | 18 | 2 | 5 |

Expected calibration error 0.031, worst bin 0.050, over 18 edges:

| confidence bin | edges | claimed | observed | gap |
| --- | ---: | ---: | ---: | ---: |
| 0.45 to 0.65 | 2 | 0.550 | 0.500 | +0.050 |
| 0.88 to 0.93 | 9 | 0.900 | 0.889 | +0.011 |
| 0.93 to 0.97 | 7 | 0.950 | 1.000 | -0.050 |

**That calibration table is the part to read.** Resolving a name without a
compiler is guesswork, and guesswork is fine as long as the guess says how
sure it is. An edge claiming 0.95 is right every time here; one claiming 0.55
is right about half the time. Both are honest, and an agent can weigh them.

The `index.scip` in that fixture is genuine `scip-typescript` output,
committed so CI re-checks these numbers on every platform without a Node
toolchain.

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

The rules are data, one file per framework in
[`conventions/`](src/repoatlas/plugins/conventions), beside the tag queries
that are already one per language. One module reads them, and nothing else
in the index knows any framework exists.

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
import instead, because a convention is for finding what nothing imported.

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
      search that fits a map to a token budget
- [x] **MCP server**: seven read-only tools over stdio, each answer budgeted
- [x] **Framework conventions as data**: Laravel views, layouts, includes,
      Blade and Livewire components, and Vue components a bundler
      auto-imports. Adding a framework is adding a JSON file
- [ ] **Documentation layer**: per-file summaries anchored to symbol ranges,
      cached by content hash and measured against the same harness

Known gaps, stated rather than buried: the ranking weights are judgement
calls that no benchmark has yet settled, Laravel route and config names need
tables nothing yet reads, no measurement says how many `view()` calls in a
real project use a literal string rather than a built one, and the only
committed oracle fixture is TypeScript.

The full plan, including how tiers 3 and 4 of evaluation work and which
benchmarks cover which languages, is in [docs/evaluation.md](docs/evaluation.md).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # includes parse
pytest                 # 634 tests
ruff check .
mypy
```

CI runs the suite on Linux, macOS and Windows, against Python 3.11 and 3.13.

## Licence

MIT.
