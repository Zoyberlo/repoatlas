# pydemo

A three-file Python package and a real `index.scip` produced from it by
`scip-python` 0.6.6.

Every accuracy number this project published for its first week came from
TypeScript, because `tsdemo` was the only committed oracle. A tag query is
per language, so a TypeScript number says nothing about Python: this fixture
is what makes the Python claim checkable, and `test_oracles.py` scores the
extractor against it on every CI run, on every platform, with no Node
toolchain required.

The sources are deliberately small and deliberately awkward: a default
argument that refers to a module constant, a method calling another method
on `self`, a subclass overriding a method and calling the private one it
inherited, and a second file importing all three names across a file
boundary. Those are the edges a resolver without a compiler has to earn.

Regenerate on Linux or macOS with:

    npm install --no-save @sourcegraph/scip-python
    npx scip-python index --project-name pydemo --project-version 1.0.0 \
        --output index.scip .

`scip-python` does not run on Windows: it builds a regular expression from
`path.sep`, which is a backslash there, and the pattern is invalid. Use WSL.
