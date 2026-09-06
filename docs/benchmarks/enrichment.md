# The one thing that made the index better

Every agent-level comparison in this project ends in a tie. The map does
not beat an unranked outline; `find_references` is right where grep is
four-fifths right and it does not change the answer; an agent given
Serena did not call it. Those are honest negatives and they are written
up in [grep.md](grep.md).

This is not one of them. It is an offline, verifiable improvement to what
the index knows, and it is the first measurement in this project where
the index gained something no other tool on the stack provides.

## What was missing

On a Laravel application the cascade resolves a member through its
receiver, and the receiver usually has no type anybody declared.
`$ad->balance_due` is worse than untyped: the property does not exist
anywhere. Eloquent invents it from the table at run time, so no compiler
front end sees it, which is why `scip-php` agrees with this index's
blindness rather than exposing it.

PHPStan with larastan does see it. `repoatlas phpstan` runs the collector
(see [phpstan.md](phpstan.md)); `repoatlas enrich` folds the answers in.

## What it added

Two applications on the same stack, member and call reference sites in
the backend:

| | sites | resolved before | after | added |
| --- | ---: | ---: | ---: | ---: |
| 363 files | 11,586 | 1,098 (9.5%) | **1,711 (14.8%)** | +613, **+56%** |
| 1,832 files | 60,338 | 19,391 (32.1%) | **22,994 (38.1%)** | +3,603, **+18.6%** |

The larger application gains less in relative terms because it starts
better typed — 32.1% against 9.5% — which is the expected shape: the
enrichment fills in what declarations do not say, so a codebase that
declares more has less left to fill.

What it fills in is the same category both times. On the small one:
`id` 81, `dates` 33, `name` 28, `phone` 24, `balance_due` 23, pointing at
`Ad` 259, `User` 136, `Invoice` 81. On the large one: `id` 504, `field`
247, `finance_report_id` 141, `new_value` 116, pointing at
`ChangeHistory` 637, `FinanceReport` 479, `ChangeLog` 441.

## Whether they are right, with no oracle to ask

There is no ground truth for these. That is the point of them: an
Eloquent column is precisely what `scip-php` does not report, so the
oracle that grades every other edge in this index has nothing to say.

So the check is independent of both producers. PHP names what it uses, so
a site resolved to `App\Models\Ad` in a file that never mentions `Ad` is
a claim the file contradicts — unless the type came from a parent, which
the index's own inheritance edges can follow.

| | 363 files | 1,832 files |
| --- | ---: | ---: |
| the site's own file names the target | 99.0% | 90.8% |
| a class it inherits from names it | 0.0% | 4.0% |
| **explained** | **99.0%** | **94.7%** |

The remainder is `User`, `DocumentTemplate`, `ChangeHistoryService` —
classes reached through `auth()->user()` or a container binding and named
nowhere, which this check cannot credit and which are very likely
correct. It bounds the damage rather than proving the answer: noise would
not score 95%.

## Three bugs this found, which are the interesting part

The first run on the larger application reported 68,604 targets it could
not place, against 4,573 it could. That ratio was the bug report.

**A trait is analysed once per class that uses it.** PHPStan sets the
scope's file to the *using class* while the nodes keep the trait's line
numbers, so declarations landed at line 263 of four unrelated files, one
of which is 57 lines long. `Scope::getTraitReflection()` says where the
node really is. Fixing it collapsed 22,887 reported declarations to
5,553, because the duplicates had been the same trait counted once per
user.

**The same mismatch happens without traits.** Evaluating
`OtherClass::SOME_CONSTANT` pulls the other class's declaration node into
this file's scope. There is no reflection call that undoes that, so the
join stopped trusting spans: a site already carries the declaring class's
own file, from reflection, and a member name inside one file is
unambiguous enough. The declaration records are now the fallback, and a
record whose span points outside its own file is rejected rather than
followed.

**Duplicate detection on the line was not enough.** Matching an existing
edge by `(path, line)` alone skipped whole lines, which threw away 173
edges on the small application. Matching on the exact byte column
recovered those — and on the large one it went the other way, moving
79,675 sites from "new" to "already known". Without it the enrichment
would have written tens of thousands of duplicate edges into a store
whose reference counts are read by every ranking in the project.

## Does it generalise?

The mechanism does. `repoatlas enrich --facts` takes JSON Lines from any
producer that can say "the member at this byte range resolves to this
class, declared in this file"; PHPStan is the first, not the only one the
format admits.

Whether it *pays* for a given language is a separate question with a
number, and `--dry-run` answers it before anything is built. What matters
is not how much a type engine resolves but how much of that lands inside
the repository. For PHP with larastan the split was 627 in and 3,918 out.

### The frontend of the same application does not have this prize

The measurement first, since it is what settles it. Member and call sites
the index cannot resolve, on the 1,832-file application:

| what the unresolved name is | share |
| --- | ---: |
| nothing in the repository carries it | 44.8% |
| a JavaScript builtin (`map`, `filter`, `then`, …) | 16.9% |
| a Vue framework member (`$refs`, `$route`, `$q`, …) | 4.8% |
| a repository symbol carries the same name | 33.6% |

Two thirds of it is npm and the browser. A TypeScript type engine would
resolve those correctly and point them at `node_modules`, which this
index does not hold and should not — an edge into a file nobody will open
is not navigation, and that is the same 86% that made the PHP number 583
rather than 4,272.

The last row is a loose upper bound, not a prize. Probing the largest
category, `this.x` inside components, the picture is that the extraction
is already doing its job: one component with 92 unresolved `this.x` had
128 symbols indexed — its component, 86 methods, 34 `data()` fields — and
84 of the 92 were `$refs`, `$route`, `$nextTick`, `$bus`, `$emit`. Of the
5,226 unresolved `this.x` across the frontend, resolving them through the
files each component actually imports reaches 207, and inspection shows
most of those are coincidence: `ToolBar.vue`'s `this.loading` matching a
`loading` in an unrelated component.

So the asymmetry is not about PHP and JavaScript. It is that Eloquent
invents members **that belong to the repository's own models**, from the
repository's own migrations, while Vue and Quasar's `$refs` and `$q`
belong to the framework. One is a gap in what the index knows about its
own code; the other is the boundary of what it should hold at all.

The rule that falls out, and the reason `--dry-run` exists: **write a
producer for a language when its framework invents members that belong to
the repository.** Laravel does. Vue does not.

## Does an agent need it?

Two of this project's benchmarks cannot answer that, and knowing why
saved a run. `agentbench` asks which files a change touches, which is
driven by the map, and the map moved by one file of fifty-five at 2,000
tokens. `sitebench` scores against `scip-php`, and **none** of the added
edges sits at a site that oracle resolves — 0 of 620 — which is the same
fact that makes them worth having and makes that benchmark blind to them.

What did move is `find_references` on a model: 117 uses to 376.

So the question has to be the one those edges answer. Six columns whose
true set is small and whose name is common, put to an agent with grep,
a shell and a local checkout:

| question | true sites | answered | hit | precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| `Schedule.id` | 8 | 25 | 7 | 0.28 | 0.88 |
| `User.id` | 14 | 25 | 7 | 0.28 | 0.50 |
| `Schedule.status` | 4 | 25 | 1 | 0.04 | 0.25 |
| `Schedule.date` | 13 | 25 | 4 | 0.16 | 0.31 |
| `Client.balance` | 13 | 25 | 2 | 0.08 | 0.15 |
| `Ad.files` | 8 | 25 | 8 | 0.32 | 1.00 |
| **mean** | | | | **0.19** | **0.51** |

An agent with grep answers the cap every time and is right about a fifth
of the time. It is not that the model is careless: `status` appears on
four other models and in four hundred lines of this repository, and
nothing in the text says which `$x->status` is a `Schedule`.

Repeated on the 1,832-file application, six columns chosen across a
range of difficulty — the noise column is how many `grep -w` lines the
name returns for each site the type engine attributes:

| question | noise | true | answered | hit | precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `ReportTemplateGroup.id` | 439× | 9 | 23 | 9 | 0.39 | 1.00 |
| `ClientCompany.name` | 359× | 11 | 25 | 9 | 0.36 | 0.82 |
| `Note.additional_fields` | 30× | 7 | 24 | 7 | 0.29 | 1.00 |
| `ContactInfo.client_company_id` | 29× | 11 | 18 | 11 | 0.61 | 1.00 |
| `FinanceReport.financial_type` | 11× | 14 | 25 | 14 | 0.56 | 1.00 |
| `ChangeLog.source_page` | 4× | 13 | 25 | 13 | 0.52 | 1.00 |
| **mean** | | | | | **0.46** | **0.97** |

### What this does and does not prove

Less than it first appears, and the difference matters.

Recall is 0.97 on the larger application against 0.51 on the smaller, so
"grep misses them" is not a finding that replicated. What did replicate
is that the agent answers two to three times as many locations as the
type engine attributes to that model.

Reading those extras is what settles it, and they are three different
things. For `ChangeLog.source_page`, of twelve:

- `'source_page' => 'client_company_form'` — an array key in a payload
  that is later mass-assigned. Not a member access on a `ChangeLog`, but
  a human asking "what writes this column" would want it.
- `'source_page' => $change->source_page` — a genuine read the engine did
  not type.
- `$entry->source_page` in a test — genuine, and outside the analysed
  directory, since only `app/` was given to PHPStan.

So the reference is incomplete by construction, the agent's precision is
understated, and **this experiment does not establish that the enrichment
beats grep at the agent level.** What it establishes is that the two
answer different questions. The index answers "member accesses a type
engine can attribute to this model", exactly and only. An agent with grep
answers "everything that looks like this column", which is a superset
containing array keys, tests and untyped receivers.

Which is more useful depends on the task. For "what breaks if I rename
this column", the superset is right. For "which code reads a `Schedule`'s
status rather than some other model's", the index's answer is the one
asked for, and the `Schedule.status` case — four correct lines against
twenty-five answered — is where that shows.

The honest summary is that the *offline* result stands on its own and
does not need this: 56% and 18.6% more resolved references, corroborated
94.7% to 99.0% by the files themselves, contradicting the compiler-backed
oracle nowhere. Whether an agent converts that is, as everywhere else in
this project, not yet demonstrated.

## What it costs, and the one caveat

The edges carry their own tier, `type_engine` at 0.98, one rung below
`oracle`. An inference can be wrong where a compilation cannot, and a
report that mixed them would be claiming more than it has.

Nothing is overwritten. A site the cascade already settled is left as it
was, matched on the exact byte column rather than the line — matching by
line alone threw away 173 edges, because on this application a chained
line usually holds more than one member access.

The caveat worth stating plainly: a column's edge points at the model
class, not at a declaration of the column, because there is no
declaration to point at. `find_references` on `Ad` therefore now includes
the places that read its columns. That is true — code that reads
`$ad->balance_due` does use `Ad` — but it is a wider claim than "this is
a use of a declared member", and a reader should know which they are
getting. The tier is how they can tell.

The other caveat is operational: enrichment writes edges into an existing
store, and the next re-index of those files replaces them. It is a step
in a pipeline, not a property of the index yet.

## What it does *not* reach: the search hook

Measured 6 September 2026, on the 1,832-file application, against the
store the `agentbench` run built. The dump took 51 seconds
(`repoatlas phpstan <app>/backend --larastan <...>/extension.neon`:
119,630 sites, 114,307 resolved, 146,291 facts), and folding it in added
6,340 edges, 89,515 to 95,855.

That is a 7.1% larger index. It is very nearly no change at all to what
[the search hook](hook.md) can say:

| grepped name | plain | enriched |
| --- | ---: | ---: |
| `save`, `store`, `index`, `handle`, `show`, `boot`, `user`, `report` | unchanged | unchanged |
| `render` | 1,032 chars | 1,127 |
| `delete` | 1,602 | 1,838 |
| `update`, `create` | 1,841 / 1,888 | 1,763 / 1,873 (shorter: the cap redistributes) |
| `query`, `invoice` | silent | silent |

Two of fourteen common names gained anything.

The reason is in the enrichment's own numbers rather than in the hook:
**5,367 of the 6,340 added edges point at a member nothing declares** —
Eloquent's columns and magic relations. The hook groups uses by the
*declaration* they reach, and an edge whose target has no declaration has
no group to appear under. The two producers are close to orthogonal on
this application.

So enrichment is not the next lever for the hook, which is the opposite of
what was assumed when the ceiling arm was planned. Where it should still
pay is a direct `find_references` on a declared symbol whose receiver only
a type engine can resolve — a different question, asked by a different
tool, and not yet measured at the agent level.

The measurement itself is cheap enough to keep: 51 seconds a commit means
enrichment could run inside a benchmark walk for about twenty minutes of
extra wall clock. It is affordability, not value, that this establishes.
