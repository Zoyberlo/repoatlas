# agent-guard

Turns a project's written rules into rules that actually hold.

A rule in `AGENTS.md` or `CLAUDE.md` is advisory: it works only while the agent reads it and keeps
reading it. This plugin registers a `PreToolUse` hook, so the rules it carries are enforced by the
harness before the tool call happens — whether or not anything was read.

## How it splits

- **The engine** (`scripts/guard.js`) ships in the plugin and is project-agnostic.
- **The rules** live in each project at `.claude/agent-guard.json`.

Install once, then every repo declares its own rules. A project without that file is unaffected.

## Rule format

```json
{
  "rules": [
    {
      "id": "blades",
      "path": "/backend/resources/views/",
      "message": "Blade templates are not edited — fix the importer instead."
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Name in the log. Required in practice; defaults to `unnamed`. |
| `message` | What the agent is told when the rule fires. Say what to do instead, not just "no". |
| `tools` | Which tools the rule applies to. Defaults to `["Edit", "Write"]`. |
| `path` | Substring of the file path. |
| `pathRegex` | Regular expression against the file path. |
| `command` / `commandRegex` | The same, against a Bash command. Needs `"tools": ["Bash"]`. |
| `content` / `contentRegex` | Matched against the whole tool input, so it sees the text being written. |
| `action` | `"block"` (default) or `"warn"`. |

Conditions are ANDed — `path` plus `content` fires only when both match, which is how you write a
narrow rule like "not this flag, in these files" without banning a whole directory. A rule that
declares no conditions matches nothing rather than everything.

Paths are resolved to absolute and normalised to forward slashes before matching, so one pattern
covers POSIX, Windows and UNC paths alike.

See `examples/agent-guard.example.json` for every shape in one file.

## Promoting a rule instead of guessing

Start a candidate as `"action": "warn"`. It prints its message and records a `WARN` line in
`.claude/agent-guard.log`, but lets the call through. After a week the log says whether it fires on
real work or only gets in the way — then promote it to blocking, or delete it. The point is to
decide on evidence rather than on impression.

## Escape hatch

`AGENT_GUARD_OFF=1` turns every rule into a pass and records a `BYPASS` line. For the genuine
exception, not for a rule that is merely inconvenient — if it is inconvenient, fix the rule.

## Failure behaviour

Every failure path allows the call: unparseable input, a missing or malformed rules file, a broken
regex in one rule. A guard that wedges a session is worse than one that misses a case. A malformed
rules file complains on stderr so it cannot disarm everything in silence.

## Tests

```bash
node scripts/selftest.js
```

22 cases covering both path conventions, tool scoping, the warn action, the escape hatch, and every
fail-open path. Run it after touching the engine.
