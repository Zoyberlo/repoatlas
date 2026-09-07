# The same change, asked for in different words

Ten agent-level comparisons found no win. Every one of them posed the task
as a **commit subject** — written by the developer who had just made the
change, in the vocabulary of the code. One reads like *"render the note
rows in the summary block"* — half those nouns are identifiers.

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

## The English run

6 September 2026. Nineteen wordings posed, eighteen scored, sixteen paired
in both arms after exclusions, **two repeats each** and averaged per task —
because the previous positive result on this harness was destroyed by
single-run variance and averaging is the direct answer to that.

```bash
repoatlas agentbench <app> --prompts wordings-en.json --arms grep,hook \n  --commits 44 --repeats 2 --out enbench.json --with-runs
```

| symbol recall, repeats averaged | grep | hook | paired delta |
| --- | ---: | ---: | --- |
| all sixteen tasks | 0.274 | 0.290 | +0.017 [−0.052, +0.101] W3/L3 |
| `precise` (n=4) | 0.325 | 0.325 | **+0.000 [0.000, 0.000]** W0/L0 |
| `domain` (n=12) | 0.257 | 0.279 | +0.022 [−0.071, +0.133] W3/L3 |

Three wins, three losses, ten ties. Nothing.

### Against the registered prediction

The lexical-anchor account said the advantage should be larger in `domain`
than in `precise`. Nominally it is: +0.022 against +0.000. Reading that as
support would be dishonest — there is no advantage in either stratum to
compare, the `domain` interval is three wins against three losses, and
`precise` has four tasks. **Not supported, and not falsified either.** The
comparison had no power to decide.

### The result that is not about the index

The same repository, the same commits, the same arms — and only the
sentence changed:

| | commit subjects | user wordings |
| --- | ---: | ---: |
| grep, symbol recall | 0.464 | **0.274** |
| grep, file recall | 0.958 | **0.737** |
| turns | 17.3 | 24.1 |
| tokens | 399,269 | 745,227 |
| runs lost to the turn cap | 0 | **8** |

**Asking in a person's words instead of the developer's halves accuracy and
roughly doubles cost.** Eight runs hit the thirty-turn ceiling and were
excluded; the commit-subject runs lost none. That is by far the largest
effect measured anywhere in this project, and the index does nothing about
it: both arms fall together.

It also means every earlier comparison was run on the easy half of the
distribution — which was the suspicion that prompted this, now with a
number on it.

### A correction

After the tenth comparison failed to reproduce, this project explained it
by saying injected context is a branch point and therefore variance. On
this run that explanation does not hold: `grep` scored identically twice on
10 of 16 tasks with a mean swing of 0.093, and `hook` on 9 of 13 with a
mean swing of **0.057**. The arm carrying the extra context was the steadier
of the two. The earlier explanation was a guess made after the fact and
should not be repeated.

### Cost

| repeats averaged | grep | hook | paired delta |
| --- | ---: | ---: | --- |
| tokens | 745,227 | 615,481 | −129,746 [−345,723, +34,323] W8/L8 |
| turns | 24.1 | 21.6 | −2.5 [−8.0, +1.5] W9/L7 |
| dollars | $1.019 | $1.042 | +$0.023 [−0.148, +0.225] |

Cheaper on average and evenly split on wins, so no cost claim survives
either.

### What weakens this run

Sixteen paired tasks, and eight runs excluded — five from `hook`, three
from `grep` — so the exclusions are not symmetric. The turn cap of thirty
was binding, which is a real hazard: a cap that truncates both arms could
hide a difference that only shows with more turns. The account hit its
spend limit again on the final run. And `precise` at n=4 cannot support any
comparison at all.

## The Ukrainian run, and what it falsified

7 September 2026, `--max-turns 40` because the thirty-turn cap was binding
last time. Nineteen wordings, eighteen paired, two repeats each, averaged.

This was the sharpest condition this project can construct: **nineteen of
nineteen requests share not one word with their answer**, because the code
is English and the request is Ukrainian. If a text search were ever going
to be helpless, it is here.

| symbol recall, repeats averaged | grep | hook | paired delta |
| --- | ---: | ---: | --- |
| eighteen tasks | 0.293 | 0.292 | **−0.001 [−0.026, +0.023]** W3/L3 |
| file recall | 0.792 | 0.764 | −0.028 [−0.083, +0.000] W0/L1 |

Not a weak null. The interval is ±0.025 wide, so an effect larger than
about two and a half points in either direction is ruled out. Three wins,
three losses, twelve ties.

### The rationale is falsified, not merely unsupported

The argument for this whole line of work was that a request with no
lexical anchor leaves `grep` nothing to search for. The same tasks, the
same arms, only the language of the sentence changed:

| same tasks, English → Ukrainian | English | Ukrainian | paired delta |
| --- | ---: | ---: | --- |
| `grep`, symbol recall | 0.287 | 0.281 | −0.006 [−0.038, +0.026] |
| `grep`, file recall | 0.745 | 0.779 | +0.034 [−0.020, +0.108] |
| `grep`, turns | 24.6 | 22.7 | −2.0 [−5.5, +1.5] |

**`grep` did not care.** Removing every shared word between the request and
the code cost it six thousandths of a point, and it used fewer turns.

The reason is that an agent does not search for the words in the request.
It reads the request, forms a hypothesis in the codebase's own vocabulary,
and searches for *that*. The model supplies the anchor itself, so the
anchor was never missing. "The request has no string to grep for" describes
a problem the agent does not have.

That was the last standing theoretical case for a resolved index helping an
agent that already has a shell. It is now measured and gone.

### The variance correction, confirmed twice

`grep` scored identically on both repeats for 12 of 18 tasks, mean swing
0.085; `hook` for 13 of 16, mean swing **0.046**. Same direction as the
English run. The arm carrying injected context is the steadier one, and the
[withdrawn explanation](hook.md#the-confirmation-failed) for the failed
reproduction — that extra context is a branch point and therefore variance
— is wrong in both runs that could test it.

### A number that looks like a result and is not

Pooling both wording runs, `hook` used 117,190 fewer tokens per task,
interval [−248,568, −10,988], which excludes zero. It should not be
reported as a saving, for two reasons that are visible in the same output:
**sixteen tasks were cheaper and eighteen dearer**, so the mean is pulled
by a few large savings rather than a consistent effect, and the dollar cost
does not move at all (−$0.048 [−$0.162, +$0.074]). It is also a pooling
decided after seeing the numbers.

An interval excluding zero alongside a losing win-loss count is a warning,
not a finding.

### Exclusions

Five runs lost, against eight in the English run, so raising the turn cap
helped — three of the five are a single task that neither arm could finish.
The account hit its spend limit on the final run again.

## Status

Both sets are run. The prompt sets sit outside this repository, next to the raw runs, since
they describe a client application's features: `wordings-en.json`,
`wordings-ua.json`, `enbench-2026-09-06.json`.

```bash
repoatlas agentbench <app> --prompts wordings-en.json --stratum domain \
  --arms grep,hook --commits 44 --repeats 1 --out en-domain.json --with-runs
```
