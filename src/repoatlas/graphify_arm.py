"""graphify, wired into a benchmark arm the way its authors install it.

graphify (Graphify-Labs/graphify) is the most widely adopted tool in the
category this project measured, and the only one in it with a published
positive result on code: 82.0% against 70.8% key-fact coverage over six
questions on a large repository. This module exists so that claim can be
tested with the same harness, pairing and repeats every other arm got.

Being fair to it means using it as documented rather than as convenient,
and `graphify install --project` for Claude Code does three things. All
three are reproduced here, from graphify's own installed files rather than
from a transcription of them:

- **Instructions.** The `always_on/claude-md.md` block, telling the agent to
  run `graphify query` before searching. Installed, it goes into CLAUDE.md;
  here it is appended to the system prompt, because CLAUDE.md is a file in
  the checkout and the baseline arm reads the same checkout.
- **Hooks.** Two `PreToolUse` hooks calling `graphify hook-guard`: on
  `Bash|Grep` a mandatory-sounding nudge, on `Read|Glob` the same nudge — or,
  in strict mode, a denial of the first raw source read of a session. Both
  are delivered as `additionalContext` / `permissionDecision`, which Claude
  Code does show the model.
- **The CLI.** The agent consumes the graph by running `graphify query`,
  `path` and `explain` through Bash. The arm may run `graphify` and nothing
  else it could not already run.

What differs from an installed project is only where things live, and each
difference exists to keep the baseline arm blind to graphify:

- the graph is built into `GRAPHIFY_OUT` **outside** the checkout, so the
  baseline arm cannot find `graphify-out/GRAPH_REPORT.md` by globbing;
- `graphify` is on `PATH` for this arm's process only.

The graph is rebuilt for every checked-out commit with `graphify update
--force`, which graphify describes as code-only and needing no LLM. It
measured 19 seconds for 1,947 files on the application this was tested
against. Documents, PDFs and images are not given semantic extraction:
that step uses Gemini or the host agent as a language model, and a task
that asks where a code change goes does not turn on it.

Nothing leaves the machine. graphify's own LLM clients activate only when a
provider key is present, and the rebuild runs with every such variable
removed from its environment as a second line of defence rather than a
first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "PROVIDER_VARIABLES",
    "Graphify",
    "GraphifyError",
    "RebuildResult",
]

# Every variable graphify's backend detection reads, from llm.py's
# `detect_backend` and `_backend_env_keys` in 0.9.62. Stripped from the
# rebuild's environment, so a key that appears in this shell later cannot
# route a client codebase to a hosted model.
PROVIDER_VARIABLES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "OLLAMA_BASE_URL",
    "OLLAMA_HOST",
)

# The outputs of one build. The `cache/` directory beside them is kept
# between commits: it is content-addressed, so it makes a rebuild of a
# neighbouring commit fast without letting one commit's graph survive into
# the next.
_BUILD_OUTPUTS = (
    "graph.json",
    "graph.html",
    "GRAPH_REPORT.md",
    "manifest.json",
    ".graphify_labels.json",
    ".graphify_labels.json.sig",
    ".graphify_root",
)


class GraphifyError(RuntimeError):
    """graphify could not be found, read, or built."""


@dataclass(frozen=True)
class RebuildResult:
    seconds: float
    nodes: int
    edges: int


@dataclass(frozen=True)
class Graphify:
    """One installed graphify, one output directory, one hook mode."""

    executable: Path
    out: Path
    strict: bool = False

    # -- what the arm is given ------------------------------------------

    def instructions(self) -> str:
        """graphify's own CLAUDE.md block, read from the installed package.

        Read rather than copied, so the arm is given exactly what the version
        under test ships. If it cannot be read the arm cannot be run as
        documented, and a benchmark of a weakened arm would be a benchmark
        of something graphify does not ship — so that is an error, not a
        fallback.
        """
        python = self._python()
        probe = (
            "import graphify, pathlib; "
            "print(pathlib.Path(graphify.__file__).parent / 'always_on' / 'claude-md.md')"
        )
        try:
            located = subprocess.run(
                [str(python), "-c", probe], capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GraphifyError(f"could not locate graphify's CLAUDE.md block: {exc}") from exc
        path = Path(located.stdout.strip())
        if located.returncode != 0 or not path.is_file():
            raise GraphifyError(
                f"graphify's CLAUDE.md block is not where the package keeps it: {path}"
            )
        return path.read_text(encoding="utf-8").strip()

    def settings(self) -> dict[str, Any]:
        """The PreToolUse hooks `graphify install` writes, in the same shape.

        Mirrors `_claude_pretooluse_hooks` in graphify's install.py: the same
        two matchers, the same commands, the same ten-second timeout, and
        `--strict` on the read guard when strict mode is on. The executable
        is absolute, as graphify writes it for a non-project install.
        """
        exe = str(self.executable)
        read = f'"{exe}" hook-guard read' + (" --strict" if self.strict else "")
        return {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|Grep",
                        "hooks": [
                            {"type": "command", "command": f'"{exe}" hook-guard search', "timeout": 10}
                        ],
                    },
                    {
                        "matcher": "Read|Glob",
                        "hooks": [{"type": "command", "command": read, "timeout": 10}],
                    },
                ]
            }
        }

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """The arm's process environment: graphify on PATH, its output located.

        Built from `base` rather than mutating it, so the baseline arm's
        environment is untouched and cannot run `graphify` even if it tried.
        """
        env = dict(os.environ if base is None else base)
        env["PATH"] = f"{self.executable.parent}{os.pathsep}{env.get('PATH', '')}"
        env["GRAPHIFY_OUT"] = str(self.out)
        # graphify reads this at runtime to force strict mode on or off,
        # which pins the mode to the arm instead of to whatever a shell has.
        env["GRAPHIFY_HOOK_STRICT"] = "1" if self.strict else "0"
        return env

    def forget_recent_queries(self) -> None:
        """Clear graphify's record of the last query, before each run.

        Strict mode does not deny a read if *any* `graphify query`, `path` or
        `explain` ran within `GRAPHIFY_HOOK_STRICT_TTL` — thirty minutes by
        default — and it records that in `cache/last_query_stamp` in the
        output directory. In a project that is a sensible proxy for "this
        developer has just oriented themselves". In a benchmark, where every
        run shares one output directory, it means the first query any run
        makes switches strict mode off for every run in the next half hour,
        including runs of the other arm.

        That is not hypothetical. The smoke run before this was added showed
        the strict arm making no graphify call on two tasks of three while
        reading files, and zero denial markers in total: the default arm ran
        first, queried, and silenced strict mode for everything after it. Fed
        the same read directly, the hook answered with a nudge while the stamp
        existed and with `permissionDecision: deny` once it was removed.

        Clearing it before each run makes every run what a real session is —
        one in which nobody else has just queried — without touching the
        graph or its cache.
        """
        (self.out / "cache" / "last_query_stamp").unlink(missing_ok=True)

    # -- building the graph ----------------------------------------------

    def rebuild(self, root: Path, *, timeout: int = 1800) -> RebuildResult:
        """Build the graph for the tree currently checked out at `root`.

        Removes the previous build's outputs first, so nothing from another
        commit can survive into this one, and keeps the content-addressed
        cache so an unchanged file is not parsed twice.
        """
        self.out.mkdir(parents=True, exist_ok=True)
        for name in _BUILD_OUTPUTS:
            target = self.out / name
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)

        env = {k: v for k, v in os.environ.items() if k not in PROVIDER_VARIABLES}
        env["GRAPHIFY_OUT"] = str(self.out)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(self.executable), "update", ".", "--force"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GraphifyError(f"graphify update failed to run: {exc}") from exc
        seconds = time.monotonic() - started

        graph = self.out / "graph.json"
        if completed.returncode != 0 or not graph.is_file():
            tail = (completed.stdout + completed.stderr).strip().splitlines()[-3:]
            raise GraphifyError(
                f"graphify update exited {completed.returncode} with no graph: "
                + " | ".join(tail)
            )
        data = json.loads(graph.read_text(encoding="utf-8"))
        nodes = data.get("nodes") or []
        edges = data.get("links") or data.get("edges") or []
        if not nodes:
            # An empty graph would make the hooks and queries silently useless
            # and turn the arm into the baseline with a longer prompt.
            raise GraphifyError(f"graphify built an empty graph for {root}")
        return RebuildResult(seconds=seconds, nodes=len(nodes), edges=len(edges))

    # -- internals -------------------------------------------------------

    def _python(self) -> Path:
        """The interpreter graphify's executable runs under."""
        try:
            first = self.executable.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError) as exc:
            raise GraphifyError(f"cannot read {self.executable}: {exc}") from exc
        if not first.startswith("#!"):
            raise GraphifyError(f"{self.executable} has no interpreter line")
        return Path(first[2:].strip().split()[0])
