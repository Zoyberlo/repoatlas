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

The application's backend, 11,586 member and call reference sites:

| | resolved | share |
| --- | ---: | ---: |
| the cascade alone | 1,098 | 9.5% |
| **with the type engine folded in** | **1,708** | **14.7%** |

610 sites newly resolved, **+56% on what the index had**. 627 edges were
added; 615 of them point at a member nothing declares.

What they are is exactly the category that was missing:

| newly resolved | count |
| --- | ---: |
| `id` | 81 |
| `dates` | 33 |
| `name` | 28 |
| `heim_dates` | 25 |
| `phone` | 24 |
| `email`, `file`, `balance_due` | 23 each |

and what they point at is the models that own them: `Ad` 259, `User` 136,
`Invoice` 81, `Schedule` 69, `Client` 32.

## Whether they are right, with no oracle to ask

There is no ground truth for these. That is the point of them: an
Eloquent column is precisely what `scip-php` does not report, so the
oracle that grades every other edge in this index has nothing to say.

So the check has to be independent of both producers, and the file's own
text is: PHP names what it uses, and a site resolved to `App\Models\Ad`
in a file that never mentions `Ad` is a claim the file contradicts.

**621 of 627 (99.0%)** land in a file that names the target class. The
six that do not are all `User`, in files that reach it through
`auth()->user()` or a container binding and so never import it — correct
resolutions that this check cannot credit. It is a bound on the damage
rather than a proof: if the enrichment were noise, this number would not
be 99%.

## What it lets you ask

"Which code reads this column" is the question the new edges answer, and
it is one a text search answers badly, because a column name is an
ordinary English word that also appears in migrations, blade templates,
other models' columns and comments. Lines a reader would have to look
through, either way:

| column | sites the index names | lines `grep -w` returns | ratio |
| --- | ---: | ---: | ---: |
| `name` | 28 | 849 | **30.3×** |
| `email` | 23 | 407 | 17.7× |
| `id` | 81 | 1,062 | 13.1× |
| `phone` | 24 | 258 | 10.8× |
| `dates` | 33 | 275 | 8.3× |
| `heim_dates` | 25 | 82 | 3.3× |

This is a cost ratio, not precision and recall. Grading the index's
answers against larastan would be circular, since larastan produced them,
and grep's extra lines are not all wrong — some are the same column on a
different model, which is a different question with the same spelling.
What the table says is how much a reader wades through, and it is the
same kind of claim as the 7× already measured for `find_references`.

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
