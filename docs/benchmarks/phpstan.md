# What a type engine knows about PHP that this index does not

`scip-php` is the PHP oracle this project started with, and on a real
Laravel application it is a poor one — not because it is wrong, but
because it is wrong in the same places. Both it and this index resolve a
member only when something declares the receiver's type, so 67% of PHP
callables look unused to both. Grading against an oracle that shares your
blind spots measures agreement.

PHPStan does not share them: it infers types rather than reading
declarations, and with `larastan` it also knows what an Eloquent model's
columns are — the one category no compiler front end sees, because the
property does not exist until a row is fetched.

`repoatlas phpstan <root>` runs it. The PHP half is three classes and a
neon under `src/repoatlas/oracle/phpstan/`; nothing is written into the
analysed project.

## The trap that made the first numbers meaningless

PHPStan answers `yes` to `hasMethod` on `mixed`. That is correct for its
own purpose — it must not report an error where it knows nothing — and it
hands back `stdClass` as the declaring class. Believed, that turned
**3,910 of 8,806 sites**, 44% of them, into resolutions naming a class the
source never mentions:

```
app/Http/Requests/UserUpdateRequest.php:46  call  where   receiver=mixed  -> stdClass::where
```

The first run therefore read 86.3% resolved. The true figure was 42.1%.
The guard is this project's own rule, applied to the oracle: a member
resolves through its receiver or not at all, so the receiver has to name
at least one class.

A second, quieter one: NEON merges arrays, so
`universalObjectCratesClasses: []` is a no-op that reads like a fix. It
needs a trailing `!` to overwrite.

## What it resolves

The test application's backend, `app/`, 8,806 member accesses:

| | resolved | of those, written down nowhere |
| --- | ---: | ---: |
| PHPStan alone | 3,705 (42.1%) | 640 |
| **with larastan** | **5,350 (60.8%)** | **1,357** |

larastan adds 1,645 and takes none away. What it adds is exactly the
shape a Laravel index cannot see:

| what larastan resolved | count |
| --- | ---: |
| `Model::where`, `find`, `get`, `whereDate`, `orderBy` … | 698 |
| model columns and relations: `Ad::id`, `User::phone`, `Ad::dates` | 591 |
| `response()->json()` | 117 |
| `Collection` methods | 95 |
| `Storage::url` | 38 |

## Against this index, on the same sites

7,964 member and call sites in `backend/app/` that both producers report:

| | resolved |
| --- | ---: |
| PHPStan + larastan | 5,229 (63.1%) |
| this index | 961 (9.9%) |

That gap is smaller than it looks, and the split matters more than the
total. Of the 4,272 sites PHPStan resolves and this does not:

- **3,689 (86%) point into `vendor/`** — Carbon, the query builder, the
  facades. This index does not index vendor, and an edge into a file
  nobody will open is not navigation.
- **583 (14%) point at a declaration inside the repository.** Those are
  real misses, and they are almost entirely Eloquent: `Ad::id` 38,
  `User::phone` 22, `Ad::balance_due` 19, `Schedule::ad_id` 16.

So the honest figure for what a type engine would add to *this* index is
583 in-repo resolutions on the backend, against the 961 it has — a 61%
increase, concentrated on model columns and relations. Worth having, not
transformative, and nothing like the 5.4× the headline suggested.

## What is still unresolved, and why

3,442 sites resist even larastan, in two shapes:

- `mixed->id`, `mixed->balance`, `mixed->save` — 180, 118, 77. Untyped
  code that nothing can resolve, and the honest floor of the exercise.
- `App\Models\Ad|Illuminate\Database\Eloquent\Collection<int, App\Models\Ad>`
  — 249. A union from `Model::find()`, resolvable in principle by taking
  each branch.

## Reading the licences, since this is an MIT project

`phpstan/phpstan` and `larastan/larastan` are both MIT and install through
composer, which is why they are what this uses.

Intelephense, the PHP language server Serena defaults to, is not an
option. Its licence limits use to "an individual end user … paired with a
Language Server Protocol compatible Integrated Development Environment or
text editor", and forbids reproducing, distributing "or otherwise using"
it for any other purpose. An indexer is not an IDE. Separately, *find all
implementations*, *go to declaration* and *go to type definition* are
premium features requiring a purchased key.
