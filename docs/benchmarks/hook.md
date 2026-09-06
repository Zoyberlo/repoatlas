# Answering a search instead of offering a tool

Every earlier agent-level comparison in this project offered the index as
something the agent could call. Nine of them found no win. This one does
not offer it: a `PostToolUse` hook runs after each `Grep` and appends what
the index knows about the name that was searched for.

It is the first agent-level result on the localisation task where the
interval excludes zero. It is also the weakest kind of positive result
this project accepts, and the caveats below are not decoration.

## The run

`repoatlas agentbench`, 6 September 2026, on a private Laravel application
of 1,832 indexed files. Forty-four commits walked, twenty-four scored in
both arms, one repeat, Opus 5 under a Max login.

```bash
repoatlas agentbench <app> --arms grep,hook --commits 44 --repeats 1 \
  --out hookbench.json --with-runs
```

Both arms have the same tools (`Read`, `Grep`, `Glob`, and the rest of the
read-only set), the same prompt and the same hint. The only difference is
that the `hook` arm runs with a `--settings` file declaring:

```json
{"hooks": {"PostToolUse": [{"matcher": "Grep",
  "hooks": [{"type": "command", "command": "repoatlas hook --store <db>"}]}]}}
```

## What came back

| twenty-four paired tasks | grep | hook | paired delta |
| --- | ---: | ---: | --- |
| **symbol recall** | 0.464 | **0.556** | **+0.093 [+0.009, +0.197]** W6/L1/T17 |
| symbol recall at five | 0.322 | 0.413 | +0.091 |
| file recall | 0.958 | 0.958 | ±0.000 [0.000, 0.000] |
| file precision | 0.242 | 0.219 | −0.023 |
| locations named | 12.8 | 13.7 | +0.9 |
| tokens | 399,269 | 450,015 | +50,746 [−15,913, +118,156] |
| cost | $0.664 | $0.721 | +$0.057 |
| turns | 17.3 | 18.6 | +1.3 |

The shape of that is the interesting part. **File recall is identical and
was already near its ceiling**; the movement is entirely in which symbol
inside those files the agent named. That is exactly what the hook says and
nothing else: `save` is four methods in four classes, and here is which of
them the resolved edges reach.

An effect that appears precisely where the mechanism predicts it is worth
more than the same effect appearing somewhere unexplained.

## Why this and not the tool

The tool was tried, twice, and the failure was not adoption on this task.
On the earlier `agentbench` run the index arm called the index 8.5 times a
run and **not one of twenty-four runs ignored it** — and it still finished
level, at 19% more tokens and 3.7 more turns. The index was used and did
not pay.

What is different here is not that the agent finally listens. It is
*when*: the hook answers a search the agent had already decided to make,
so the disambiguation costs no extra turn. The earlier arm bought the same
information with 3.7 turns of tool calls, and the information was not
worth 3.7 turns.

## What the hook says, and what it refuses to say

It speaks only when the searched name resolves to more than one indexed
symbol, or when a resolved use was not among what the search printed.
When grep already answered, it emits nothing and costs nothing. Against
this application's index, common method names produce 1.0–2.1 KB of
context; a name with one definition and no unlisted uses produces zero.

It never argues from absence. A line grep printed that the index has no
edge for is **not** reported as noise, because having no edge is not
evidence that a line is meaningless. Every claim it makes is a stored edge
with a confidence tier behind it.

## The caveats, largest first

**It was not pre-registered.** The project's standing rule — a measure
earns its place when its paired 95% interval excludes zero — was applied
after the number was seen, not before. [review.md](review.md) registered
its threshold in advance; this did not. That is a real difference in
weight, and the honest label for this result is *promising*, not
*established*.

**The bound is thin.** The lower end is +0.009. Seventeen of twenty-four
tasks are exact ties, so the whole result rests on seven tasks, six up and
one down. A single task moving would take it back across zero.

**One repository, one repeat.** The three-repository replication that made
the no-checkout result credible has not been done here.

**It costs more, by an amount nobody can state.** +50,746 tokens with an
interval that crosses zero, sixteen of twenty-four tasks dearer. The
earlier index arm was rejected partly on cost, and this arm has not shown
it avoids that.

**This repository is not the one the earlier run used.** That was a
363-file application; this is 1,832 files. So there is no like-for-like
grep baseline to compare the absolute levels against, only the pairing
inside this run.

## The next run, and its threshold, written down now

The last point suggests the experiment that would settle the mechanism. An
independent ablation ([arXiv 2608.13568]) found the variable that decides
whether a structural index helps is the target's **lexical collision
rate** — how often a name means something else in the same repository. A
1,832-file application collides far more than a 363-file one, and this
hook is a collision-resolver and nothing else.

So the prediction is specific: **the effect should shrink on the smaller
repository.** Registered before running it:

> Repeat `--arms grep,hook` on the 363-file application, same commits,
> same repeat count. The mechanism story holds if the symbol-recall
> advantage there is smaller than +0.093. It is falsified if the advantage
> is as large or larger, which would mean the gain is coming from
> somewhere other than collision.
>
> Separately, the result here is confirmed only if a second run on *this*
> application also puts the interval above zero.

Raw runs are kept outside this repository, since they carry a client
project's commit subjects and paths: `hookbench-2026-09-06.json`.
