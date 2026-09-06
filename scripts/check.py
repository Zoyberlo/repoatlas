"""Every gate this project has, in one local command.

CI was cut back to what a laptop cannot do — a clean machine, and running
when someone forgets. Everything else moved here, which only works if
"here" is a single command that cannot be half-run.

That last part is not decoration. Three times in this project a commit went
in on a failing suite, each time through a shell line shaped like

    pytest -q | tail -2 && git commit -m ...

where the `&&` reads the *pipe's* exit code and never sees pytest's. Each
step below is run on its own and judged by its own status, and a failure
anywhere makes the whole run fail. There is no arrangement of pipes that
can make this print `all gates pass` while something is broken.

    python scripts/check.py              # everything
    python scripts/check.py --skip types # everything but mypy
    python scripts/check.py --list       # what the gates are

Roughly a minute, against about three for a push through CI.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "claude-plugin" / "plugins" / "agent-guard" / "scripts"


@dataclass(frozen=True)
class Gate:
    """One check, its command, and what has to exist for it to run."""

    name: str
    argv: list[str]
    what: str
    needs: str | None = None

    def missing(self) -> str | None:
        """Why this gate cannot run at all, if it cannot."""
        if self.needs and shutil.which(self.needs) is None:
            return f"{self.needs} is not on PATH"
        return None


def gates() -> list[Gate]:
    """The gates, cheapest signal first is not the order — correctness is.

    Tests run first because a failure there makes the rest uninteresting,
    and the two node gates run last because they are the fastest to re-run
    while iterating on a rule.
    """
    return [
        Gate("tests", [sys.executable, "-m", "pytest"], "the suite"),
        Gate("lint", [sys.executable, "-m", "ruff", "check", "."], "ruff"),
        Gate("types", [sys.executable, "-m", "mypy"], "mypy, strict"),
        Gate(
            "guard",
            ["node", str(GUARD / "selftest.js")],
            "the hook's own behaviour",
            needs="node",
        ),
        Gate(
            "rules",
            [
                "node",
                str(GUARD / "check.js"),
                "--cases",
                str(ROOT / ".claude" / "agent-guard.cases.json"),
            ],
            "this repository's nine rules still fire",
            needs="node",
        ),
    ]


def run(gate: Gate) -> tuple[bool, float, str]:
    """Run one gate. Its own exit code decides, and nothing else."""
    started = time.monotonic()
    finished = subprocess.run(
        gate.argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - started
    output = (finished.stdout or "") + (finished.stderr or "")
    return finished.returncode == 0, elapsed, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip", action="append", default=[], metavar="GATE")
    parser.add_argument("--only", action="append", default=[], metavar="GATE")
    parser.add_argument("--list", action="store_true", help="name the gates and stop")
    args = parser.parse_args(argv)

    chosen = gates()
    if args.only:
        chosen = [g for g in chosen if g.name in args.only]
    chosen = [g for g in chosen if g.name not in args.skip]

    if args.list:
        for gate in gates():
            print(f"  {gate.name:<8} {gate.what}")
        return 0
    if not chosen:
        print("check: nothing selected", file=sys.stderr)
        return 2

    failures: list[tuple[Gate, str]] = []
    for gate in chosen:
        why = gate.missing()
        if why:
            # Not a skip. A gate that cannot run has not passed, and
            # reporting it as anything softer is how a check goes dead
            # while everyone keeps believing it.
            print(f"  {gate.name:<8} CANNOT RUN  {why}")
            failures.append((gate, f"{why}\nInstall it, or pass --skip {gate.name}."))
            continue
        # The progress line is overwritten by the result, which only works
        # on a terminal; in a log or a pipe it would leave a torn duplicate.
        live = sys.stdout.isatty()
        if live:
            print(f"  {gate.name:<8} ...", end="", flush=True)
        ok, elapsed, output = run(gate)
        prefix = "\r" if live else ""
        print(f"{prefix}  {gate.name:<8} {'ok  ' if ok else 'FAIL'} {elapsed:6.1f}s  {gate.what}")
        if not ok:
            failures.append((gate, output))

    if not failures:
        print(f"\nall {len(chosen)} gate(s) pass")
        return 0

    for gate, output in failures:
        print(f"\n--- {gate.name} ---")
        print(output.rstrip()[-4000:])
    print(f"\n{len(failures)} of {len(chosen)} gate(s) failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
