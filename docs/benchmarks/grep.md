# Does this beat grep?

The honest form of the question is "at what". Grep is not one thing: it
is a name search an agent can repeat, widen and read. This index is not
one thing either: it answers where code is, who uses it, and what a
repository contains, and those three questions have three different
answers.

Everything below is measured on one repository, a private Laravel 10 and
Quasar application: 363 indexed files, 3,696 symbols, 10,825 edges, three
years of history. It is the stack this project exists for, and one
repository is one repository.

## What makes a name hard to search for

The variable that decides whether a structural index is worth anything is
not the language. An independent five-arm ablation across three models
([arXiv 2608.13568]) found it is *the target's lexical collision rate*:
how often the name means something else in the same repository.

On this application, measured over its 3,242 navigable symbols:

| | |
| --- | ---: |
| symbols whose name is also some other symbol's name | 2,097 (**65%**) |
| grep lines returned for the median symbol's name | 13 |
| grep lines per real use, median | 4.0 |
| grep lines per real use, mean | 29.6 |

The worst are the ones a task is most likely to name. `client` is fifteen
different symbols and 1,121 lines; `user` is nine and 1,066; `card` is six
and 812. For those, a name search is not a wrong answer, it is not an
answer.

## What an answer costs

The same question asked both ways, priced with the store's own token
estimator, sampling forty files, forty symbols and thirty classes:

| question | through the index | by reading | ratio |
| --- | ---: | ---: | ---: |
| what does this file define | 152 tokens (`file_outline`) | 2,258 (read it) | **15x** |
| who uses this symbol | 80 tokens, 4 lines (`find_references`) | 579 tokens, 14 lines (`rg -w`) | **7x** |
| what does this class contain | 136 tokens (`get_symbol --skeleton`) | 515 (read the class) | **4x** |
| what is this repository | 3,950 tokens (`repo_map`) | 926,444 (read it all) | 235x |

## The ceiling, before any agent

If the index's own answer were worse than a name search, no agent holding
it could win. So: fourteen symbols from the backend, chosen by the
compiler-backed `scip-php` oracle because it knows every call site of
them, ordered hardest-for-grep first; `find_references` against every
whole-word hit, both scored on the oracle's sites.

| | precision | recall | F1 | tokens |
| --- | ---: | ---: | ---: | ---: |
| `find_references` | **1.000** | 1.000 | **1.000** | 170 |
| `rg -w <name>` | 0.782 | 1.000 | 0.847 | 331 |

Fourteen of fourteen exactly right, against grep's four in five, at half
the tokens. That number is not this project's own opinion: the same
language-server ablation reports LSP precision 1.00 against grep's 0.76 on
the same question, on other repositories in other languages.

Two things it does not say. Grep's recall is perfect here, so an agent
willing to read all fourteen lines gets the same answer for more tokens
and more turns. And these fourteen are the *easy* symbols: `scip-php`
cannot resolve a call through an untyped variable, so `$ad->client()` has
no compiler-backed truth at all, and the cases where grep is worst are
exactly the cases no oracle on this stack can judge.

## The edges nobody else has

A SCIP indexer for PHP reads PHP. `scip-typescript` pointed at this
front end opens 24 of its 91 files and skips every `.vue`. Neither reads
a Blade template, a router that names a page in a string, or
`view('users.index')`. On this repository that is not a rounding error:

| | |
| --- | ---: |
| edges from a Vue component into the JavaScript it imports | 1,040 |
| edges the other way | 39 |
| PHP into Blade | 3 |
| component tags resolved to a file in the repository | 73 |
| dynamic `import(...)` resolved | 37 |

The 1,450 component tags left alone are Quasar's own `q-btn` and
`q-input`, which are outside the repository and correctly nobody's. The
two halves of the monorepo have no edges between them at all, which is
also true: they talk over HTTP, and an index should not invent a call
where there is a fetch.

## What an agent does with it

Two harnesses, both posing real work from the repository's own history
and both scoring against what the commits changed.

`repoatlas agentbench` asks where a change goes. This is the task the
literature says an index does not win: on it, agents reach for a semantic
tool 0 to 6 percent of the time and a language server costs Opus six
percent more tokens for the same answer. The first pilot here, twelve
commits and four paired tasks, agreed and settled nothing: symbol recall
0.223 with the index against 0.208 without, at 30% more cost, deltas
inside the noise. It also showed why the harness had to change: the grep
arm had shell commands denied and spent a third of its turns being
refused; both arms padded their answers to the allowed fifteen entries.

`repoatlas sitebench` asks who uses a symbol, scored against the SCIP
oracle rather than against this index. That is where the ceiling above
says the difference lives.

<!-- numbers land here when the runs complete -->

## What the published comparisons say

| study | task | index versus grep |
| --- | --- | --- |
| [arXiv 2606.22417] | SWE-bench issue to fix, index on/off in one agent, 91 instances | resolve 41.9% → 50.4%, localisation acc@5 44.3% → 84.5%, **fewer** tokens; multi-file 44.9% → 91.3% |
| [arXiv 2608.13568] | find every call site | F1 0.706 → 0.778, precision 1.00 against 0.76, used 45–57% unprompted |
| the same | localise a named symbol | +6% tokens (Opus), used 0–6% of the time |
| [arXiv 2601.23254] | repository-level code completion | grep-like retrieval **beats** a graph method, 38.6% against 19.4% |

## What this measures and what it does not

Measured: the index is right (0.99 precision and recall against
compilers on this application), its answers cost a fraction of the
equivalent reading, and on the one question with independent ground truth
it is exactly right where a name search is four-fifths right.

Not measured, and not measurable on this stack: whether the index helps
on the cases where grep is worst, because those are the cases where the
language declares nothing and no compiler-backed oracle exists to judge
either tool. That is the honest edge of what can be known here.

[arXiv 2606.22417]: https://arxiv.org/abs/2606.22417
[arXiv 2608.13568]: https://arxiv.org/abs/2608.13568
[arXiv 2601.23254]: https://arxiv.org/abs/2601.23254
