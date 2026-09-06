# zoyberlo-plugins

A Claude Code plugin marketplace holding one plugin so far.

| Plugin | What it does |
|---|---|
| [`agent-guard`](./plugins/agent-guard) | Enforces a project's hard rules with a `PreToolUse` hook, from rules the project declares in `.claude/agent-guard.json`. |

## Install

```bash
claude plugin marketplace add ./claude-agent-guard
```

```bash
claude plugin install agent-guard@zoyberlo-plugins
```

Then, in any project that should be guarded, create `.claude/agent-guard.json` — start from
[`plugins/agent-guard/examples/agent-guard.example.json`](./plugins/agent-guard/examples/agent-guard.example.json).

Hooks are read when a session starts, so the guard goes live in the **next** session, not the
current one.

## Verify it is actually on

```bash
node plugins/agent-guard/scripts/selftest.js
```

That proves the engine. To prove the wiring, ask Claude to edit a file one of your rules forbids —
it should come back blocked, and `.claude/agent-guard.log` should have a new `BLOCK` line.

## Layout

```
.claude-plugin/marketplace.json    the marketplace manifest
plugins/agent-guard/
  .claude-plugin/plugin.json       the plugin manifest
  hooks/hooks.json                 registers the PreToolUse hook
  scripts/guard.js                 the engine
  scripts/selftest.js              22 cases
  examples/                        a rules file showing every field
```
