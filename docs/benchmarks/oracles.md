# Two compiler-backed oracles over a real application

Every accuracy number this project had was measured on fixtures of two or
three files, written to exercise the resolver and small enough to read.
Fixtures are where problems are found, not where an index is proven. This
is the first measurement against a real application, and it changed more
of the resolver than the fixtures ever had.

## The repository and the oracles

A private Laravel 10 and Quasar application, the same one
`docs/benchmarks/steering.md` and `docs/benchmarks/weights.md` use. Two
copies were made outside the repository and indexed by the compiler-backed
tools for their languages:

- `backend/`, 248 PHP files under `app`, `routes`, `database`, `tests` and
  `config`, indexed by `scip-php` 0.0.2, which resolves through Composer's
  autoloader. 163 of the files the oracle covers are compared; the rest are
  Blade templates and configuration it does not open.
- `frontend/`, a Quasar application in JavaScript: 24 `.js` files and 62
  `.vue` single-file components, indexed by `scip-typescript` 0.4.0 with
  `allowJs`. It does not open `.vue` files, so the comparison covers the
  24.

Both indexers needed coaxing; the recipes are at the end.

## What the first run said

| | definitions P / R | references P / R | reference edges wrong |
| --- | ---: | ---: | ---: |
| backend, before | 1.000 / 0.967 | 0.429 / 0.985 | 1,932 of 3,381 |
| frontend, before | 0.169 / 1.000 | 0.032 / 0.141 | 687 of 710 |

Recall was fine. Precision was not, and the calibration table said where:

| rung | claimed | edges | observed |
| --- | ---: | ---: | ---: |
| `suffix` | 0.55 | 890 | 0.000 |
| `unique_name` | 0.75 | 864 | 0.059 |
| `same_module` | 0.90 | 598 | 0.931 |
| `import_map` | 0.95 | 618 | 0.696 |

The two bottom rungs, which match a bare name against the whole
repository, were wrong 1,700 times out of 1,754. Every one of those edges
was a member reached through a variable nobody had typed: `$order->id` went to
some job's `$id`, `$order->update([])` to a controller's `update()`,
`$client->user` to an authentication controller's `user()`. Eloquent models
have no declared properties, so a Laravel application is nothing but these.

`import_map` at 0.696 was a different story, and the oracle's rather than
the index's; see the next section.

## What the oracle can and cannot judge

Before trusting a number, the comparison was asked what shape of receiver
the oracle resolves at all. For every reference of ours, grouped by what
its member was reached through, this is how often the oracle had resolved
anything at the same site:

| receiver | ours | oracle resolved | ratio |
| --- | ---: | ---: | ---: |
| bare call, `helper()` | 3,172 | 31 | 0.010 |
| `$this->x` | 826 | 489 | 0.592 |
| static, `Util::x` | 966 | 18 | 0.019 |
| variable with a declared type, `$svc->x` | 880 | 0 | 0.000 |
| variable without one, `$order->x` | 3,166 | 0 | 0.000 |
| member of an expression, `make()->x` | 826 | 35 | 0.042 |

`scip-php` 0.0.2 resolves a member through `$this` and, sometimes, through
a class name. It never resolves one through a variable, typed or not, in
four thousand tries. The 187 "false positives" it charged against
`import_map` were calls such as `$service->report()` inside a
`handle(ReportService $service)`, every one of them right.

So `repoatlas compare` now gates its score by this probe. A shape the oracle
resolves at fewer than one site in a hundred, given at least thirty, is
outside its reach, and edges of that shape are listed rather than counted
wrong. The same test is applied to definitions: `scip-typescript` records
nothing for the methods of an object literal, which is every action and
getter of a Pinia store, and 118 of the frontend's 149 definitions are
those. Both gates are printed in the report under *What the oracle can
judge*, with the counts that justified them.

## What changed in the resolver

The rule that came out of the calibration table: **a member resolves
through its receiver, or not at all.** The receiver is `$this`, a variable
with a declared or constructed type, a property of the enclosing class
with a declared type, a class named outright, or nothing anyone can tell,
and only the first four are followed. The bottom rungs still serve bare
calls, constructions, type names and values, where a name is most of the
evidence there is.

Along the way, each of these was found by the oracle and fixed:

- Static calls were recorded as base classes, so every command that
  queried a model inherited from it and `parent::__construct()` went there.
- `Auth::user()` inside a controller that had its own `user()` resolved to
  the controller's, because a facade that was not ours fell through to
  the enclosing class. A receiver that is not ours now stops the lookup.
- `use Foundation\Kernel as ConsoleKernel` above `class Kernel` resolved
  its import site to the class under it. Import sites resolve through the
  import rung only.
- Members of `new class { ... }` were nobody's, so `$this->data` inside one
  went to some export class's `$data`. Anonymous classes are types.
- A method calling itself produced no edge, because an edge never pointed
  a symbol at itself. Recursion is a call.
- `: static` as a return type was a type named `static`.
- PHP's `end($list)` resolved to a `public $end` in some class. A bare call
  reaches functions and types, never fields or methods.
- `$greeter = $this->build()` typed nothing. A local assigned from a call
  now takes the callee's declared return type, read off its signature in
  the callee's own file; `admin = Admin()` in Python is an `Admin`.
- Function-local `const`s were symbols, and 148 of the frontend's
  definitions. They are locals, and a use of one shadows any symbol with
  the name, inside anonymous callbacks too.
- `export default x`, `export { x }`, `a = x` and `{ x }` were not
  references. Every bare identifier is, now, with binding sites removed.
- `$echo` in JavaScript was `echo`: the sigil strip was PHP's.
- A `tsconfig.json` that `extends` another declared no aliases.

## What the last run says

| | definitions P / R | references P / R | edges outside the oracle's reach |
| --- | ---: | ---: | ---: |
| backend, after | 0.996 / 1.000 | 0.993 / 1.000 | 187 through typed variables |
| frontend, after | 0.968 / 1.000 | 0.994 / 1.000 | 44 through `this`; 118 object-literal members |

Calibration on the backend, 1,455 scored edges:

| rung | claimed | edges | observed |
| --- | ---: | ---: | ---: |
| `unique_name` | 0.75 | 92 | 1.000 |
| `same_module` | 0.90 | 573 | 0.983 |
| `import_map` | 0.95 | 790 | 1.000 |

Every rung now delivers more than it claims. `suffix` produced no scored
edge at all: with members off its menu it has nothing left that the
repository is ambiguous about. The ten remaining backend false positives
are `$this->owner` reaching the relationship method `owner()`, which is
what Laravel means by it and not what PHP does. The four false definitions
are the anonymous classes themselves, which the oracle has no symbol for.

The localisation benchmark, which had nothing to do with any of this, moved
too: symbol recall on the same repository went from 0.136 to 0.188 plain
and from 0.264 to 0.312 steered, on 280 commits. Wrong edges had been
feeding the ranking.

## Recipes

`scip-php` inside a copy of a Laravel project:

    composer require --dev "davidrjenni/scip-php:^0.0.2" -W --no-scripts --ignore-platform-reqs
    ln -s ../../../vendor vendor/davidrjenni/scip-php/vendor
    vendor/bin/scip-php

It reads the root package's version from `vendor/composer/installed.php`,
which is `null` for a copy that is not a git checkout, and dies on it. Add
`"version": "1.0.0"` to `composer.json` and, if the copy has no `.git`, set
the root `'reference'` in `installed.php` to any string.

`scip-typescript` over a JavaScript project needs a `tsconfig.json`, even
beside a `jsconfig.json`:

    { "extends": "./jsconfig.json",
      "compilerOptions": { "allowJs": true, "checkJs": false, "noEmit": true },
      "include": ["src/**/*.js"] }

and `node_modules` in place, or every package import is `any` and nothing
that flows through one resolves. `--infer-tsconfig` produces an index of
nothing.

Neither copy, nor any per-file result, is committed; only these aggregates.
