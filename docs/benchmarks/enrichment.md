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
