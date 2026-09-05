# Reviewing a diff with no checkout to grep

Five experiments in this project ended in a tie, and every one was
measured with a local checkout, a shell and grep. The explanation each
time was the same: the agent reads what it is given and then greps, so
grep recovers whatever the index would have supplied.

This is the setting where it cannot. A review bot, a CI check and a
hosted agent see a diff and can fetch a file by path; there is no tree to
search. The question is the reviewer's own — *this changed, what else
uses it?* — and the ground truth is the compiler-backed oracle's
references, minus the ones inside the file the diff already shows.

Three arms differing by one thing each. `read` and `index` both have
`Read` and neither has a shell; `index` additionally has the MCP server.
`grep` keeps its shell as a reference point, because every other result
in this project was measured under those conditions and the two settings
belong in one table.

## The first run was void, and how that was caught

Every arm scored F1 1.000, including the one with no search tools, which
is impossible. The run records said why: the `read` arm called `Bash`
three times a run.

`--allowedTools` pre-approves; it does not withhold. Under
`--permission-mode dontAsk` an arm listing only `Read` still had the whole
toolbox. Withholding needs `--disallowedTools`, and closing it took two
passes: with `Bash` denied the agent reached for `Monitor`, which takes a
shell command of its own.

The fix is checked by running it, not by reading the flag. In the run
below the `read` arm called `Read` and nothing else, 28.6 times per task.

## The result

Twenty symbols asked, eight with uses outside their own file:

| | read | **index** | grep (has a checkout) |
| --- | ---: | ---: | ---: |
| precision | 1.000 | **1.000** | 1.000 |
| recall | 0.938 | **1.000** | 1.000 |
| F1 | 0.958 | **1.000** | 1.000 |
| turns | 29.6 | **9.2** | 3.6 |
| tokens | 924,885 | **67,295** | 52,253 |
| cost | $1.537 | **$0.170** | $0.089 |

Paired on the task, 95% bootstrap:

| | point | interval | W/L |
| --- | ---: | ---: | ---: |
| F1, read → index | +0.042 | [0.000, 0.125] | 1/0 |
| **tokens**, read → index | **−857,590** | **[−1,567,339, −257,454]** | **0/8** |
| **turns**, read → index | **−20.4** | **[−27.5, −13.1]** | **0/8** |
| **cost**, read → index | **−$1.37** | **[−$2.22, −$0.60]** | **0/8** |
| F1, grep → index | 0.000 | [0.000, 0.000] | 0/0 |
| tokens, grep → index | +15,042 | [+5,813, +24,435] | 6/2 |

## What was registered, and what happened

The threshold was written down before the run: *the index earns its place
if its advantage over `read` in F1 has a 95% interval excluding zero.*

**It does not.** +0.042, interval [0.000, 0.125], one task better out of
eight. By the rule as stated, this is the sixth tie on accuracy.

What is not a tie is the cost, and it is not close: **eight tasks out of
eight, no overlap with zero, thirteen times fewer tokens and nine times
less money.** An agent with no shell and no index does get the right
answer — by reading 28.6 files per task and spending $1.54 to do it. With
the index it reads 3.75 files, makes 3.2 server calls, and spends $0.17.

That effect was not registered in advance, so it is a hypothesis this run
produced rather than a claim it establishes. It is stated separately for
that reason.

## The same benchmark on a 1,832-file application

The caveat above was the prediction: 363 files is small enough that
reading 28 of them is a viable strategy. On the larger application it
should not be, and the oracle for it was built with `scip-php` — this
index scores 1.000 on definitions and 0.991 F1 on references against it,
so it is sound.

Fourteen symbols, chosen for having at least two uses outside their own
file: methods called `row`, `value`, `section`, `notes`, `message`, with
three to thirty-four external uses each and between 338 and 2,979 lines
of `grep -w` noise.

**Five of fourteen `read` runs never answered.** They exhausted thirty
turns reading files — 50.3 files per task — and produced nothing. That is
the collapse the prediction called for, and it lands in the exclusions
rather than in the scores, so the scores have to account for it: an agent
asked a question that produces no answer has failed at it, and scoring
the attempt as absent hands the arm a free pass on exactly the tasks it
could not do.

| all 14 tasks, unanswered counted as 0 | read | **index** | grep (has a checkout) |
| --- | ---: | ---: | ---: |
| answered | 9/14 | **13/14** | 14/14 |
| F1 | 0.503 | **0.839** | 0.868 |
| turns | 51.3 | **15.2** | 7.9 |
| tokens | 688,011 | **137,849** | 166,679 |
| cost | $1.228 | **$0.324** | $0.337 |

Paired on the task, 95% bootstrap:

| | point | interval | W/L |
| --- | ---: | ---: | ---: |
| **F1, read → index** | **+0.337** | **[+0.129, +0.565]** | **6/0** |
| F1, grep → index | −0.029 | [−0.071, +0.000] | 0/2 |
| F1, read → grep | +0.365 | [+0.153, +0.587] | 8/0 |

**The registered threshold is met.** The index's advantage over
file-reading alone excludes zero, on six tasks better and none worse. It
is the first time in this project that a pre-registered accuracy
threshold has been.

And the second row matters as much: against an agent that *does* have a
checkout, the index arm's difference crosses zero. Without a tree to
search, at a third of the turns, it answers as well as grep does with
one, for the same money.

### What is a choice here, stated as one

Counting an unanswered task as zero is a decision, and the result depends
on it. Over the nine tasks `read` did finish, its F1 is 0.782 and the
paired difference is +0.190 [0.000, 0.444], which crosses zero. Both
numbers are in this document because they answer different questions: the
first is "how good is a reviewer with no index", the second is "how good
is it when it manages to answer at all".

The first is the one a reviewer cares about, which is why the threshold
was tested against it.

## What this changes and what it does not

On the small application the same benchmark showed no accuracy difference
at all — `read` scored 0.958 there. So the effect is a function of
repository size, and that is the finding rather than a caveat on it:
brute-force reading works at 363 files and stops working at 1,832. Nine
of this project's benchmarks were run in a setting where grep was
available and every one tied; this is the setting where it is not, and it
is the one where the index earns its place.

Fourteen tasks on one PHP backend is thin, and the effect should be
confirmed on another repository before it is built on.

## The caveat that decided this run

This repository is 363 files. Brute-force reading is a viable strategy at
that size, which is why the `read` arm's accuracy held up at all. On the
1,832-file application it should not be: 28 files is 1.5% of the tree
rather than 8% of it.

So the follow-up that separates *the index saves money* from *the index
is necessary* is the same benchmark on the larger application. If `read`
accuracy collapses there while `index` holds, the no-checkout setting is
where this project's premise is true. If `read` holds there too, the
index is a cost optimisation and should be described as one.

Eight paired tasks is also thin, and twelve of twenty symbols were
dropped because every use sat in the file the diff already showed.
