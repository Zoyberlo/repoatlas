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

The full run, forty-four commits and twenty-four tasks scored in both
arms, one repeat, Opus 5 under a Max login:

| | grep | with the index | paired delta |
| --- | ---: | ---: | --- |
| symbol recall | 0.317 | 0.349 | +0.032 [−0.059, +0.122] W6/L3, p=0.51 |
| symbol recall at five | 0.272 | 0.231 | −0.041, p=0.51 |
| file recall | 0.777 | 0.777 | ±0.000 |
| tokens | 361,282 | 429,397 | **+68,116**, p=0.064 |
| turns | 17.9 | 21.5 | **+3.7**, p=0.027 |
| cost | $0.58 | $0.61 | +$0.03 |
| index calls | 0 | 8.5 | every run used it |

No difference in what was found, more turns to find it, at 19% more
tokens. The index arm was told to start with `repo_map` and did: not one
of the twenty-four runs ignored it, against the 0–6% unprompted use the
published ablation saw, so this is not a case of an agent refusing a
tool. Split at three files the picture does not change: +0.077 recall on
small changes, −0.022 on large ones, neither near significance. On this
task the index earns nothing, and that agrees with everything published
about it.

`repoatlas sitebench` asks who uses a symbol, scored against the SCIP
oracle rather than against this index. That is where the ceiling says
the difference lives. It does not.

| fourteen symbols, one repeat | grep | with the index |
| --- | ---: | ---: |
| precision | 1.000 | 1.000 |
| recall | 1.000 | 1.000 |
| F1 | **1.000** | **1.000** |
| turns | 6.3 | 20.4 |
| tokens | 91,947 | 328,278 |
| cost | $0.19 | $0.40 |

Fourteen out of fourteen, both arms, every symbol. The tool is right
where a name search is four-fifths right, and it does not matter: the
agent with grep reads the eight or twenty-two lines the search returned,
throws away the namesakes itself, and arrives at the same answer in a
third of the turns.

Two things are worth separating in that. The first is the measurement's
own limit: these fourteen are the symbols `scip-php` can ground-truth,
and the hardest of them returns twenty-two grep lines. Reading twenty-two
lines is nothing. The symbols where a name search really fails, `client`
at 1,121 lines, are the ones with no compiler-backed truth, so the
experiment that would show a difference cannot be run on this stack.

The second is that the cost is ours. The index arm spent 20.4 turns
because it was told to: the prompt hint that suits localisation, "start
with repo_map, then search_symbols, then find_references", is exactly
wrong for a question one `find_references` answers, and the agent
followed it, calling `get_symbol` 4.6 times and `search_symbols` 3.1
times per run before getting there. It also has to hunt for a symbol id,
because no tool output prints one. Neither of those would change the
F1 column, both would change the cost column, and both are the index's
fault rather than the agent's.

## Against the closest relative

Aider's repo map is the same idea and came first: tree-sitter tags,
PageRank, a token budget. Its graph connects *files*, and a reference to
a name is an edge to every file defining that name, by exact string
match, with no imports and no types. So the difference between that and
this is one thing only, the edges, and it can be measured by building
aider's graph over the same symbols and ranking, steering, rendering and
scoring it identically.

| map at 2,000 tokens, 281 commits | symbol recall | file recall |
| --- | ---: | ---: |
| skeleton prefix, no ranking | 0.028 | 0.032 |
| grep for the task's words | 0.111 | 0.222 |
| **name-matched graph, aider's** | **0.269** | **0.494** |
| resolved graph, unsteered | 0.221 | 0.554 |
| resolved graph, steered | **0.309** | **0.579** |

Matching names gets most of the way. Against the steered map that is the
fair comparison, resolution is worth +0.040 symbol recall and +0.085 file
recall, about a sixth more of each. For drawing a map, aider's answer is
most of the answer at a fraction of the code, and it is worth saying so.

Where it is not most of the answer is the question aider does not ask.
`find_references` on a name that means fifteen things is exactly right;
matching that name is, by construction, the same 0.78 as grep, because it
*is* grep with a file-level index. That is the whole of the difference,
and it is the whole of the case for the extra machinery.

## What the published comparisons say

| study | task | index versus grep |
| --- | --- | --- |
| [arXiv 2606.22417] | SWE-bench issue to fix, index on/off in one agent, 91 instances | resolve 41.9% → 50.4%, localisation acc@5 44.3% → 84.5%, **fewer** tokens; multi-file 44.9% → 91.3% |
| [arXiv 2608.13568] | find every call site | F1 0.706 → 0.778, precision 1.00 against 0.76, used 45–57% unprompted |
| the same | localise a named symbol | +6% tokens (Opus), used 0–6% of the time |
| [arXiv 2601.23254] | repository-level code completion | grep-like retrieval **beats** a graph method, 38.6% against 19.4% |

## What this measures and what it does not

Measured, and positive: the index is right (0.99 precision and recall
against compilers on this application); its answers cost a fraction of
the equivalent reading; on the one question with independent ground
truth it is exactly right where a name search is four-fifths right; and
its resolved graph draws a better map than the name-matched graph that
is the state of the art in an open-source tool, by about a sixth.

Measured, and negative: on both questions that can be posed to an agent
with ground truth, it does no better with the index than with grep.
Where a change goes: 0.349 against 0.317 over twenty-four paired tasks,
four more turns, a fifth more tokens. Who uses a symbol: identical and
perfect for both, in three times the turns. In every one of those
thirty-eight runs the agent did call the index. It did not need it.

Not measured, and not measurable on this stack: whether the index helps
on the cases where grep is worst. Those are the cases where the language
declares nothing, which is exactly why no compiler-backed oracle can
judge them either, so the experiment does not exist. That is the honest
edge of what can be known here, and it is where the whole remaining case
for the index sits.

What follows from all of it: the index is not what makes an agent find
code on this stack, because reading is cheap and a capable agent filters
a name search by itself. What it is good for is the answer given
directly, to a person or a script, without an agent in the loop: the
right call sites in 170 tokens instead of 331 noisy ones, a file's
definitions for a fifteenth of reading it, a map that names three times
as many files as the closest published alternative and includes the
half of this repository written in Vue. Those are all measured. None of
them needs an agent to be worth something, and the agent measurements
say plainly that the agent is not where the worth is.

[arXiv 2606.22417]: https://arxiv.org/abs/2606.22417
[arXiv 2608.13568]: https://arxiv.org/abs/2608.13568
[arXiv 2601.23254]: https://arxiv.org/abs/2601.23254
