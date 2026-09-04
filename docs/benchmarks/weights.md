# The ranking weights, measured

`rank/pagerank.py` is full of numbers that were judgements: how much
restart mass a class gets over a field, which way containment points, what
a call edge is worth against an import, the damping, how hard a focused
file pulls, how much a private name is discounted, and how fast a file's
symbols stop earning their place in the map. `docs/evaluation.md` has said
since the day they were written that none of them had been measured.

This is the measurement. It changed one thing in the benchmark, nothing in
the weights, and that second half is the result rather than the absence of
one.

## The repository and the method

A private Laravel 10 and Quasar application, 1,568 commits over three
years, 363 indexed files, 4,340 symbols. Of the 400 most recent commits
touching one to eight indexed files, 283 changed at least one symbol the
index holds and could be scored.

For each commit the scratch clone is checked out at its **parent**, the
index is brought up to date incrementally, and a map is drawn at 2,000
tokens around the identifier-like words of the commit subject. The score is
how many of the symbols that commit went on to change the map actually
names.

Two halves: the sweep reads only the first, and the half it never saw
decides. Within a half every comparison is **paired** — same commit, both
settings — because commits differ from each other far more than settings
do, and comparing group means drowns every real effect in that difference.
Intervals are a 2,000-draw bootstrap over commits.

## What had to be fixed first: the metric

The first version of this benchmark counted how many of a commit's
**files** the map listed. That metric cannot settle how a budget is spent,
because the map trades symbols for filenames and file recall rises
monotonically as it does:

| spread | file recall | symbols per file |
| ---: | ---: | ---: |
| 0.00 | 0.358 | 3.58 |
| 0.50 | 0.521 | 2.44 |
| 1.00 | 0.550 | 1.94 |
| 2.00 | 0.568 | 1.53 |
| 3.00 | — | 1.26 |

Follow that gradient and the best map is a list of paths with one line
under each, which scores highest and says least. So the score is now
symbol-level: a filename with one line under it contains none of the code
the commit changed, and gets no credit for it.

Scoring against today's index was wrong for a second reason: a file since
renamed counted as a miss, and symbols that did not exist when the work
started were on the map. Walking a scratch clone and indexing the parent
tree fixes both. The repository being read is never touched.

## What the weights measure

Baseline is what ships. Deltas are paired, on the unseen half.

| variant | symbol recall Δ | 95% interval | verdict |
| --- | ---: | :--- | --- |
| **containment** | | | |
| pointing at members instead | +0.004 | [−0.014, +0.026] | no effect |
| dropped entirely | +0.004 | [−0.014, +0.026] | no effect |
| pointing both ways | −0.100 | [−0.147, −0.054] | **worse** |
| **kind prior** | | | |
| off | −0.004 | [−0.018, +0.009] | no effect |
| steeper | −0.005 | [−0.015, +0.000] | no effect |
| gentler | −0.015 | [−0.040, +0.000] | no effect |
| **edge weights** | | | |
| all flat | −0.009 | [−0.045, +0.026] | no effect |
| containment 0.05 / 0.40 | −0.003 / −0.020 | overlapping zero | no effect |
| calls only | +0.002 | [−0.013, +0.017] | no effect |
| imports 0.4 / 1.0 | 0.000 | [0.000, 0.000] | no effect |
| **damping** 0.5 / 0.7 / 0.9 / 0.95 | +0.016 … −0.010 | all overlapping zero | no effect |
| **focus weight** 5 / 15 / 150 / 500 | −0.003 … +0.007 | all overlapping zero | no effect |
| **private penalty** 0.05 / 0.5 / 1.0 | −0.005 … +0.006 | all overlapping zero | no effect |
| **map spread** | | | |
| 0.00 | −0.034 | [−0.064, −0.007] | **worse** |
| 0.25 | −0.021 | [−0.043, −0.001] | **worse** |
| 0.75 | −0.003 | [−0.015, +0.010] | no effect |
| 1.00 | −0.008 | [−0.030, +0.015] | no effect |
| 1.50 | −0.007 | [−0.040, +0.025] | no effect |
| 2.00 | −0.000 | [−0.038, +0.038] | no effect |

Two findings, and both are worth stating plainly.

**Only two settings are measurably wrong.** Running containment in both
directions costs a tenth of the score, and turning the map's spread off —
taking the top of the ranking and cutting where the budget runs out —
costs three points. Both are things this project already does not do.

**Everything else does not matter here.** The kind prior, the edge weights,
the damping, the focus weight and the private penalty are all
indistinguishable from doing nothing, on 283 commits of a real project.
They are not wrong; they are not load-bearing. That is a useful thing to
know about where the next effort should not go.

**Spread stays at 0.5.** Anything below it is measurably worse. Anything
above it is not measurably better on symbols while it keeps emptying the
map — 2.44 symbols per file at 0.5 against 1.53 at 2.0. Buying nothing with
something is not a trade worth making, so the exponent stays where it was,
now for a reason instead of by taste.

## What does matter

The one lever with a large effect is the one measured yesterday, and the
better metric makes it look larger than the file-level one did:

| map | symbol recall | file recall |
| ---: | ---: | ---: |
| plain | 0.136 | 0.521 |
| steered by the subject's words | **0.264** | 0.597 |

Steering nearly doubles the number of changed symbols the map names. Under
file recall the same change looked like 0.37 against 0.48. How the words
are matched to names, measured in `steering.md`, moves the score by more
than every weight in `pagerank.py` put together.

## What this does not settle

One repository, one stack, one author's habits in commit messages. A
setting that does nothing here could matter in a codebase shaped
differently — a library with deep inheritance would exercise the kind
prior far harder than an application with wide controllers does.

A commit message is a generous proxy for a task: written afterwards, by the
person who did the work. These are ceilings.

And 117 of the 400 commits walked could not be scored at all, because they
changed only lines no symbol covers — imports, configuration, whitespace.
The map has nothing to say about those, which is honest, but it means the
benchmark measures the map on the subset of work it is meant to help with.

Reproduce with:

    repoatlas localize <repo> --commits 400 --spread 0.5
