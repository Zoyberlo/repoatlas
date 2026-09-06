# The same change, asked for in different words

Ten agent-level comparisons found no win. Every one of them posed the task
as a **commit subject** — written by the developer who had just made the
change, in the vocabulary of the code. *"render note-row badges in a
summary block"*: half those nouns are identifiers.

That is the condition `grep` is best in, because the request already
contains the string to search for. So the ten null results are all measured
in the baseline's best case, and none of them has tested the request people
actually send: *"the button on the report page that clears it doesn't
work."*

This is the experiment that asks properly. Same commits, same ground truth,
same pairing — only the sentence changes.

## How a wording is placed

By overlap with the answer, computed rather than judged
([prompts.py](../../src/repoatlas/prompts.py)): a request is **precise** if
it names an identifier or path segment from the ground truth in any casing,
**domain** if it uses the project's words but not the answer's, and
**unanchored** if it shares nothing at all. Camel case and snake case are
split, so a wording cannot claim to be unanchored while naming the answer
in another shape.

## What the sets came out as

Nineteen wordings, authored from each commit's diff rather than from its
subject, for the tasks that a person could plausibly report. Six of the
twenty-four are test-infrastructure commits — worker-unique fixtures,
`.gitignore`, teardown helpers — and no user ever files those, so they are
left out rather than dressed up.

| set | precise | domain | unanchored |
| --- | ---: | ---: | ---: |
| English | 5 | 14 | **0** |
| Ukrainian | 0 | 0 | **19** |

**Not one English wording came out unanchored**, and that is the finding
before any agent has run. Writing deliberately in user language — "square"
for cell, "box" for field, "grid" for table — still leaves an overlap,
because the code is written in English and so is the user. Five leaked
enough to be classified `precise` despite the intent, which is the filter
working rather than failing.

The Ukrainian set is unanchored nineteen times out of nineteen, by
construction: the codebase contains no Ukrainian. For this project's owner
that is not a contrived condition but the ordinary one — the client writes
in Ukrainian and the code is in English.

## What each set can and cannot settle

The English set isolates the thing being argued about. Same language, same
domain, only the *specificity* differs — so a difference between its
`precise` and `domain` strata is about lexical anchoring and nothing else.

The Ukrainian set is the sharper test and the more confounded one. It
varies two things at once: there is no lexical anchor **and** the domain
has to be carried across a language boundary. If both arms collapse, it
says little about the index. If `grep` collapses and the index does not,
that is as strong a signal as this project can produce, because it is the
one condition where a text search has nothing whatever to work with.

Stated plainly so it cannot be quietly forgotten when the numbers arrive:
**a Ukrainian result cannot be reported as "the index helps on vague
requests" without the English set agreeing.** On its own it can only say
"the index helps when the request is in another language".

## Registered before the run

> **English, precise against domain.** The lexical-anchor account predicts
> the index's advantage is larger in `domain` than in `precise`. It is
> supported if `hook − grep` in `domain` exceeds `hook − grep` in
> `precise`. It is falsified if the two are level or reversed.
>
> **Ukrainian.** The index earns something here if `hook − grep` on symbol
> recall has a 95% interval excluding zero. Both arms scoring near zero is
> the outcome that says the task, not the tooling, is the limit — and it
> is to be reported as such rather than as a tie.
>
> **The reproducibility bar applies to all of it.** The previous positive
> result on this harness did not survive a second run of the same tasks
> ([hook.md](hook.md)), while `grep` reproduced itself on seven tasks out
> of eight. So no wording result counts until it has been run twice and
> held. One run is a hypothesis here, not a finding.

## Status

Not run. The account hit its monthly spend limit partway through the
ceiling run, which is also why that one scored nine tasks instead of
twenty-four. The prompt sets are written and sitting outside this
repository, next to the raw runs, since they describe a client
application's features: `wordings-en.json`, `wordings-ua.json`.

```bash
repoatlas agentbench <app> --prompts wordings-en.json --stratum domain \
  --arms grep,hook --commits 44 --repeats 1 --out en-domain.json --with-runs
```
