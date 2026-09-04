# Steering the map by a task's words: what the measurement said

The map can be told what a task is about. `repo_map(mention=[...])` takes
words, finds what they name, and restarts the walk there. That idea came
from aider, which weights the identifiers mentioned in a conversation ten
times an ordinary file, and it shipped here on 2026-09-04 without a
measurement, because none existed.

`repoatlas localize` is that measurement. This is what it found on the
first real repository it ran against, and what changed as a result.

*Revisited later the same day:* after the resolver stopped matching
members by name (`docs/benchmarks/oracles.md`), the same benchmark reads
0.188 plain and 0.312 steered at the symbol level, from 0.136 and 0.264.
The numbers below are as they were measured; the conclusions stand.

## The repository

A private Laravel and Quasar application: Laravel 10 in `backend/`, Quasar
in `frontend/`, one git history, three years and 1,568 commits. As indexed:
363 files, 4,340 symbols, 10,600 edges. 252 PHP, 62 Vue, 33 JavaScript, 15
Blade, 1 TypeScript.

The sample is the 200 most recent commits that touched between one and
eight indexed files. For each, the map is drawn at 2,000 tokens around the
identifier-like words of the commit subject, and scored on how many of the
files that commit touched appear on it.

## What was wrong

A mention matched a symbol only when the word *was* its whole name.

| map | recall | first file listed was touched |
| --- | ---: | ---: |
| plain, no steering | 0.370 | 0.190 |
| steered, whole-name matching | 0.356 | 0.175 |

Steering made the map **worse**. Better on 4 commits, worse on 12,
unchanged on 184; and on the 110 commits whose words matched nothing at
all it could not do anything either way.

The reason is visible in the cases it hurt. A subject reading "clients
report fix" touched `ClientsReportExport.php` and `ReportController.php`.
The word `client` matched something, because some local variable is
spelled exactly that, and the walk restarted there and dragged the map
away from both files it should have found. A task's word is usually a
*part* of the name that matters, not the whole of it.

## What was tried

Five variants over the same 200 commits, one index, one budget.

| variant | recall | first hit |
| --- | ---: | ---: |
| no steering | 0.370 | 0.190 |
| whole name, seed the symbol (what shipped) | 0.356 | 0.175 |
| whole name, seed the symbol's file instead | 0.348 | 0.145 |
| whole name, seed only high-ranked matches | 0.355 | 0.175 |
| whole name, seed both symbol and file | 0.348 | 0.145 |

Every way of weighting a whole-name match was worse than not steering. So
the weighting was not the problem; the matching was.

| variant | recall | first hit |
| --- | ---: | ---: |
| no steering | 0.370 | 0.190 |
| word matches a component of the name | **0.479** | 0.135 |
| ... capped at the 3 best-ranked matches per word | 0.460 | 0.150 |
| ... capped at the single best | 0.451 | 0.150 |
| ... seeding the files rather than the symbols | 0.453 | 0.150 |

## What ships

A mention now matches a symbol whose name it is, **or whose name is partly
made of it**, and a file the same way by stem. Components shorter than
four characters do not count on their own: `id`, `api` and `get` are in
half the names in any project.

Measured through the shipped command, same 200 commits:

| map | recall | first hit |
| --- | ---: | ---: |
| plain | 0.370 | 0.190 |
| steered | **0.477** | 0.135 |

Better on 50 commits, worse on 12. The words of a subject now match
something on 141 commits rather than 90. The median commit seeds 9 symbols
of 4,340, and the worst 174, so a common word spreads the restart rather
than concentrating it, which degrades toward the plain map instead of
collapsing onto one wrong file.

## The cost, stated

First-file-listed accuracy fell from 0.190 to 0.135. Steering finds more of
the right files and is less sure which one to name first. For a map that
is a menu the agent reads, recall is the metric that matters; if the map
were a single answer, this trade would be the wrong way round. It is worth
revisiting if the map is ever used to pick one file.

## What this does not settle

Uncapped component matching won here, but a cap costs little (0.460
against 0.479) and bounds the worst case; a second repository is what would
decide between them.

A commit subject is a proxy for a task description, and a generous one: it
was written after the work, by the person who did it. Half of these
subjects still name nothing in the index at all. An agent's own words may
be more precise, in which case these numbers are pessimistic, or vaguer, in
which case they are not.

Every other ranking judgement in `rank/pagerank.py` — the kind prior, the
containment direction, the edge weights, the spread exponent — remains
unmeasured. This tool can now settle any of them the same way.
