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
repoatlas agentbench <app> --prompts wordings-en.json --arms grep,hook \
  --commits 44 --repeats 2 --out enbench.json --with-runs
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

## Does a clarifying question buy the gap back?

The wording runs measured a gap nobody has tried to close: a request in a
person's words scores 0.274 where the same change described in the
developer's scores 0.464. That is +0.19, six times anything the index ever
moved, and it is the only large effect this project has found.

The obvious intervention is to ask. Instead of searching on a vague
request, the agent asks one or two questions first, and someone who knows
what they wanted answers them.

### How it is posed without cheating

Three phases, so that what the answerer may say can be controlled:

1. A model sees **only the vague request** and writes at most two questions.
2. A second call sees the change and answers them **as the person who filed
   the request** — someone who uses the application and cannot read code.
3. The request plus that exchange becomes the task, run through the same
   harness, the same commits, the same ground truth.

The third phase is where this could quietly become a leak rather than an
experiment. If the answerer says "it is in `ReportController::clear`", the
run measures how well an agent follows a pointer, which is not the
question. So every answer goes through the same classifier the wordings
did: **an answer that shares any identifier with the ground truth is
rejected**, not edited. Rejections are counted and reported.

### Registered before the run

> **The intervention earns its place** if the clarified set beats the vague
> set on symbol recall with a paired 95% interval excluding zero, on the
> same tasks and the same arm.
>
> **How much of the gap it recovers** is the secondary number, against the
> +0.19 between vague and commit-subject phrasing. Recovering a third of it
> would already be larger than any tool result in this project.
>
> **It can also lose.** Two extra model calls and a longer prompt cost
> tokens; if accuracy does not move, the honest reading is that the loss
> from vagueness is under-determination the user cannot resolve either, and
> that asking cannot fix what the requester does not know.
>
> **Two repeats, averaged per task**, as with both wording runs, because a
> single run on a task set full of ties is a hypothesis here and not a
> finding.

### What the generated exchanges are, and are not

Nineteen of nineteen passed the leak check at the strict setting: no answer
named a word selecting ten symbols or fewer. The most selective thing any
of them shared with its own ground truth was `app` (596 symbols), `cell`
(257), `loan` (324) — product vocabulary, not pointers. The filter is not
inert either: the same answers would fail at looser thresholds, 5 of 19 at
100 candidates and 15 of 19 under plain overlap, so there is a real
gradient and these sit at the clean end of it.

They read like the real thing. One answer runs *"I was on a report's loan
section, and I clicked into one of the existing loans to open the loan
details dialog where you edit the dates, rate and payment"* — genuine
localisation a person can give without naming any code.

**But the answerer saw the change.** Another says *"only when I've
double-clicked into the square to edit it first"*, which is exactly the
condition the fix turned on. A real reporter might never notice that. So
the simulated user has perfect recall of the true cause, expressed in
user language, and this run therefore measures an **upper bound** on what
asking can buy rather than the typical case. If the upper bound is small,
the intervention is dead; if it is large, the next question is how much of
it a real exchange recovers, which this cannot answer.

The run holds `--max-turns 30`, matching the vague English run exactly, so
the only thing that differs between them is the sentence.

## The clarifying run

7 September 2026. The same nineteen tasks, the same `grep` arm, the same
thirty-turn cap, two repeats averaged. The only difference from the vague
English run is that each request carries a two-question exchange with the
person who filed it.

| repeats averaged, seventeen paired tasks | vague | clarified | paired delta |
| --- | ---: | ---: | --- |
| **symbol recall** | 0.287 | **0.482** | **+0.195 [+0.066, +0.350] W8/L0** |
| file recall | 0.745 | 0.843 | +0.098 [−0.020, +0.245] W3/L1 |
| turns | 24.6 | 20.3 | −4.4 [−9.8, +1.0] |
| tokens | 787,059 | 668,415 | −118,644 [−432,106, +192,963] |
| cost | $1.061 | $0.996 | −$0.065 [−0.390, +0.261] |
| runs lost to the turn cap | 8 | **4** | |

**Eight wins, nine ties, no losses.** The registered bar — a paired 95%
interval excluding zero — is met, and met by the widest margin anything in
this project has produced.

### How much came back

The gap between a person's words and the developer's was 0.274 to 0.464, or
0.190. Clarification moved the same tasks by **+0.195**: on this run it
recovers the whole of it. An agent asked in user language and then allowed
two questions performs as if it had been given the developer's own sentence.

For scale, the index at its very best moved +0.093 once and did not
reproduce; the ceiling arm with every lever at once managed +0.031. This is
six times that, in the opposite place: the prompt, not the retrieval.

Cost did not rise. Every cost interval crosses zero so none of it is a
claim, but the direction is down on all four measures and the turn cap
killed half as many runs.

### What this is not yet

**One run.** The rule this project set after the hook result is that
nothing counts until a second independent run of the same tasks holds it.
Two repeats inside one run is not that. The shape is much sturdier than the
hook's was — eight wins to none against six to one with seventeen ties —
but the rule exists precisely because the sturdy-looking one evaporated.

**An upper bound**, for the reason registered before the run: the simulated
reporter had seen the change, so it answers with perfect recall of the true
cause. A real person volunteers less. What fraction of +0.195 survives a
real exchange is the question this cannot answer.

**Not a comparison of tools.** Both sides are the plain `grep` agent. This
says something about how work is requested, not about what it is given.

## The confirmation

Run again the same day, fresh clone, fresh index, the same nineteen tasks
and the same prompt set — the same wordings deliberately, because what
destroyed the hook result was run-to-run variance on identical inputs, and
that is the thing to isolate.

| | point | interval | W/L |
| --- | ---: | --- | ---: |
| **vague → clarified #2** | **+0.233** | **[+0.093, +0.397]** | **W9/L1** |
| vague → clarified #1 | +0.195 | [+0.066, +0.350] | W8/L0 |
| **clarified #1 → clarified #2** | +0.024 | [−0.015, +0.076] | W3/L1 |

The third row is the one that matters. **The two clarified runs do not
differ from each other**, so the measurement is stable and the effect is
not a sample of a noisy arm.

Per task, in the format that settled the hook:

| task | vague | clar 1 | clar 2 | Δ1 | Δ2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 48413647 | 0.00 | 1.00 | 1.00 | +1.00 | +1.00 |
| e0a8bcc7 | 0.25 | 1.00 | 1.00 | +0.75 | +0.75 |
| 1e4e0ccc | 0.00 | 0.56 | 0.56 | +0.56 | +0.56 |
| 0ed75adb | 0.50 | 1.00 | 1.00 | +0.50 | +0.50 |
| 60675c8d | 0.25 | 0.50 | 0.50 | +0.25 | +0.25 |
| 6227ed12 | 0.07 | 0.21 | 0.21 | +0.14 | +0.14 |

**Every large win reproduced exactly.** Twelve of sixteen tasks scored
identically across the two runs. The hook's four wins of 0.33, 0.25, 0.25
and 0.14 had come back at 0.00; these come back to the second decimal.

**The registered bar is met**, and it is the only agent-level result in
this project that has ever survived its own re-run.

### What is still not known

**The reporter is simulated and had seen the change.** This remains an
upper bound, and it is now the whole of the doubt. A real person answers
from memory of what they did, not from the diff, and would not volunteer
"only when I've double-clicked into the square first" unless they happened
to notice. What fraction of +0.2 survives a real exchange is the next
question and this cannot answer it.

**One application, one model, English.** Nineteen tasks on a 1,832-file
Laravel and Vue codebase with Opus 5.

**The exchanges were generated once.** Both runs used the same nineteen,
so this confirms the agent-level measurement, not the stability of
generating them. Different questions might buy less.

**It is not a tool result.** Both arms are the plain `grep` agent. Twelve
comparisons of tooling moved nothing; changing what the request says moved
0.23. That is the finding, and it points at a product that asks rather than
one that indexes.
