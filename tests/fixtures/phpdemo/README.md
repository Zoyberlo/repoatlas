# phpdemo

A three-file PHP project and a real `index.scip` produced from it by
`scip-php` 0.0.1.

PHP is half the stack this index was built for, and until this fixture
existed nothing checked a single PHP edge against a compiler-backed answer.
`test_oracles.py` scores the extractor against it on every CI run, with no
PHP toolchain required.

The sources exercise what a resolver without a compiler must work out: a
class constant used as a default argument, `$this->` calls between methods
of one class, a subclass calling a `protected` method it inherited, and a
third file constructing both classes and calling through the base type.

Regenerate with:

    composer install
    ln -s ../../../vendor vendor/davidrjenni/scip-php/vendor
    vendor/bin/scip-php
    rm vendor/davidrjenni/scip-php/vendor && rm -rf vendor composer.lock

Two notes on that recipe, both about `scip-php` rather than about this
fixture.

It resolves its own stub directory as `src/Composer/../../vendor`, which
holds when the tool is run from a clone of itself and not when Composer has
flattened it into a project's `vendor`. The symlink puts that directory
where the tool looks for it.

Its dependency `google/protobuf ^3.22` carries a published advisory, so
Composer refuses the install until told otherwise; the `audit` block in
`composer.json` is that instruction, scoped to this fixture. Nothing here
ships, and the tool parses only these three files.
