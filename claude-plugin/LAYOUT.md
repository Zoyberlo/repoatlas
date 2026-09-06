# What lives here, and why it moved

`agent-guard` is a Claude Code plugin that turns a project's written rules
into rules the harness enforces. It was developed separately; it lives
here now because the two halves of this project are converging on the
same finding.

## The finding that brought them together

This repository spent a long session measuring whether an index makes an
agent better. Nine head-to-head comparisons against `grep`, and the
result is written up in [docs/benchmarks/grep.md](../docs/benchmarks/grep.md):
where a shell and a checkout exist, the index never won.

The consistent explanation is in the tool-call records rather than the
scores. **Everything the model could choose to ignore, it ignored.**

- an agent with Serena attached called it **0 times out of 8**
- an agent with a ranked map called `repo_map` **0.8 times per run**
- a prompt telling it to start with the map cost turns and changed nothing

And everything mechanical held. The one benchmark the index won —
[reviewing a diff with no checkout](../docs/benchmarks/review.md) — it won
because the shell was *withheld*, not because the agent was persuaded.

So the design rule this plugin already embodies is the one the
measurements arrived at independently: **a rule the model must remember is
advisory; a rule the harness enforces is a fact.**

## The layout

    claude-plugin/
      .claude-plugin/marketplace.json   the local marketplace
      plugins/agent-guard/
        .claude-plugin/plugin.json
        hooks/hooks.json                registers PreToolUse on Edit|Write|Bash
        scripts/guard.js                the engine, project-agnostic
        scripts/selftest.js             22 cases, `node scripts/selftest.js`
        examples/agent-guard.example.json

The engine ships with the plugin; the rules live in each repository at
`.claude/agent-guard.json`, so one install serves every repo and a
repository without that file is unaffected.

## Installing it

    /plugin marketplace add A:/Projects/repoatlas/claude-plugin
    /plugin install agent-guard@zoyberlo-plugins

Verified on Windows with Node 24: the self-test passes 22 of 22, and a
live payload against a path rule exits 2 with the rule's message while an
unrelated path exits 0.

## What is deliberately not here

The rules and knowledge base of the application this was developed
against are **not** in this repository. This repository is private, so
that is not the reason: they are a client codebase's business documents,
and a private repository under a different account is still a different
repository. They sit outside it, at
`A:/Projects/reference/client-agent-docs/`, as a reference to design
against — the shapes are worth copying, the contents are not ours to
move.
