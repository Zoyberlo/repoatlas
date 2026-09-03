# tsdemo

A two-file TypeScript project and a real `index.scip` produced from it by
`scip-typescript` 0.4.0.

The index is committed on purpose. Every other SCIP fixture in this suite is
hand-built, which proves the reader is self-consistent but not that it agrees
with the format as an actual indexer writes it. This one closes that gap and
keeps it closed: `test_integration.py` scores the extractor against it on
every CI run, on every platform, with no Node toolchain required.

Regenerate with:

    npm install --no-save typescript @sourcegraph/scip-typescript
    npx scip-typescript index --output index.scip

Note that `scip-typescript` writes paths using the host separator, so this
index contains backslashes. That is not a defect to fix in the fixture: it is
exactly the condition `normalise_path` exists to handle, and a fixture with
forward slashes would stop testing it.
