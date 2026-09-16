# graphify, under the same harness

[graphify](https://github.com/Graphify-Labs/graphify) is the most widely
adopted tool in the category this project spent twelve comparisons
measuring — a code knowledge graph for coding agents — and the only one in
it with a published positive result on code. This page runs it through the
same harness, pairing and repeats every other arm here got, with the
thresholds written down before any run.

## What it claims

graphify's `BENCHMARKS.md` (v0.9.62) reports one agent benchmark on code:

| | graphify | grep + read |
| --- | ---: | ---: |
| key-fact coverage, ERPNext (~1M LOC) | **82.0%** | 70.8% |

What that document does and does not state:

| | |
| --- | --- |
| questions | 6, not listed |
| repeats | not stated |
| interval, p-value, per-question wins | none |
| who wrote the key facts | not stated |
| judge validation (κ = 0.81) | stated for the conversational benchmarks, not for this one |
| baseline tokens | not stated |

Under equal weighting a single question is about 17 points of that score,
so the whole claimed effect (+11.2) is smaller than one question answered
differently. By the standard that withdrew this project's own +0.093 — which
had four times the tasks and an interval above zero — it is not yet a
finding. That is not a verdict on the tool, and the run below is the way to
reach one.

## How it is installed here

Exactly as `graphify install --project` sets it up for Claude Code, taken
from graphify's own installed files rather than transcribed:

- **its instructions** — the `always_on/claude-md.md` block, telling the
  agent to run `graphify query` before searching;
- **its hooks** — `PreToolUse` on `Bash|Grep` and `Read|Glob` calling
  `graphify hook-guard`, which puts a mandatory-sounding nudge in front of
  the model, and in strict mode *denies* the first raw source read of a
  session and redirects it to the graph;
- **its CLI** — `graphify query`, `path` and `explain`, run through Bash.

Two things differ from an installed project, both so the baseline arm
cannot see graphify: the instructions go into the system prompt rather
than CLAUDE.md, and the graph is built into a directory beside the scratch
clone rather than inside it, since the baseline arm reads the same checkout
and could otherwise glob its way to `GRAPH_REPORT.md`.

The graph is rebuilt for every commit posed, after the checkout, with
`graphify update --force` — code-only, no language model, measured at 19
seconds for 1,947 files and 18,420 nodes on this application. Nothing
leaves the machine: graphify's model clients activate only when a provider
key is set, none is, and the build runs with every such variable removed
anyway.

## The arms

| arm | the same as `grep`, plus |
| --- | --- |
| `grep` | — |
| `graphify` | graphify's default install: instructions, nudging hooks, CLI |
| `graphify-strict` | the same with strict mode, graphify's own answer to agents ignoring the graph |

The hint in the task prompt is the `grep` arm's, word for word, and the
only tool either graphify arm has that `grep` lacks is `Bash(graphify *)`.
Strict mode is its own arm because this project's first finding was that an
agent ignores what it can, and strict mode is precisely graphify's remedy
for that.

Calls to graphify are counted apart from other Bash, so a null can say
whether the tool was ignored or used and unhelpful.

## Registered before the run

> **Setting.** The application with 1,832 indexed files, the same
> twenty-four commit subjects the hook and ceiling runs posed, two repeats
> per task per arm, averaged per task, Opus 5.
>
> **An arm earns its place** if `arm − grep` on symbol recall has a paired
> 95% interval excluding zero. Nothing short of that is reported as a
> result, whatever the point estimate.
>
> **The prediction this project's own evidence makes is a null for both.**
> On a commit subject the request already carries the code's vocabulary,
> and where it does not, the agent supplies it: removing every shared word
> between request and code moved `grep` by −0.006. If a graph helps here it
> has to do something no tool in the twelve comparisons did. Strict mode is
> the one feature that might — it forces a query rather than offering one —
> so a strict-only win would be a finding about forcing, not about graphs.
>
> **Cost counts.** Tokens and turns are reported beside accuracy. A null
> that costs more is a loss, not a tie.
>
> **A validity check, not a result.** The strict arm must show at least one
> graphify call in most runs, because its hook denies the first read until
> one is made. If it does not, the harness failed to reproduce strict mode
> and that arm's numbers are void rather than negative.

## What the smoke run caught, before any result was read

A three-task run of all three arms was made to check the wiring, not to
measure anything. Its accuracy numbers are not reported; two things it
exposed changed the method, and both are recorded here before the full run
rather than explained after it.

**Strict mode had been silently switched off.** The validity check above
failed: the strict arm made no graphify call on two tasks of three while
reading files, and the output directory held **zero** denial markers — strict
mode had not denied a single read. graphify does not deny a read if *any*
`graphify query`, `path` or `explain` ran within `GRAPHIFY_HOOK_STRICT_TTL`,
thirty minutes by default, and it keeps that stamp in the shared output
directory. The default arm runs first on every task, queried, and so
silenced strict mode for every run after it, including the other arm's.

It was confirmed by feeding the hook one read directly, with no model
involved: with the stamp present a fresh session got a nudge; with it
removed, `permissionDecision: deny`; and the same session a second time, a
nudge again — once per session, as documented. The stamp is now cleared
before every run of a graphify arm, which makes each run what a real
session is, one in which nobody else has just queried, without touching the
graph.

**The tasks had drifted.** The source repository has moved to another
branch since the hook and ceiling runs, and none of their twenty-four
commits exist in it any more; the smoke run posed three different ones. They
survive in the first run's scratch clone, all twenty-four reachable from the
commit that run walked from, so the full run walks from that same commit
with the same subject text pinned. The walk was checked to offer all
twenty-four before anything was spent.

One consequence is worth stating in advance: the extractor has changed since
that first run, so the ground-truth symbols for the same commits may differ
slightly and `grep`'s absolute score need not reproduce 0.464. That does not
touch the comparison, which pairs every arm on identical ground truth within
this run.

## What this cannot settle

It is not graphify's own task — that was answering questions about a
codebase, scored on key facts; this is localising a change, scored against
the symbols the commit changed. It is not graphify's scale: 1,832 files
against roughly a million lines, and this project's own data shows file
reading degrading sharply with size, so a large repository is exactly where
a graph could start to pay. It is one repository, one model, and a
code-only graph without graphify's semantic extraction of documents.

A null here would say graphify does not help an agent localise changes on a
repository of this size. It would not say graphify is useless.
