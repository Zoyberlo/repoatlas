"""Measuring the whole pipeline on a repository, so "large" is a number.

The project's stated requirement is large repositories, and until this
existed nothing measured one: the figures in the README came from a
41-file project and an 83-file one. This indexes a repository cold, again
with nothing changed, again with one file touched, then times every tool
the server exposes, and writes the lot as JSON beside the commit it was
measured on. Two runs of it are a regression test for speed.

A synthetic repository can be generated when no large one is to hand. It
is real files on disk, parsed by the real grammars, with imports that
resolve across files, so the walk, the parser, the store and the resolver
all do their actual work. What it lacks is the shape of code people write,
so a number from it is a floor for the machinery, not a claim about any
real project.
"""

from __future__ import annotations

import importlib
import json
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .rank.cache import RankCache
from .server import tools
from .store import IndexStore, update_store

__all__ = ["BenchResult", "generate_synthetic", "run_benchmark"]


@dataclass(slots=True)
class BenchResult:
    """Everything one benchmark run measured."""

    root: str
    files: int = 0
    symbols: int = 0
    edges: int = 0
    store_bytes: int = 0
    index_cold_seconds: float = 0.0
    index_noop_seconds: float = 0.0
    index_touch_seconds: float = 0.0
    tool_seconds: dict[str, float] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files": self.files,
            "symbols": self.symbols,
            "edges": self.edges,
            "store_mib": round(self.store_bytes / 2**20, 2),
            "index_cold_seconds": round(self.index_cold_seconds, 3),
            "index_noop_seconds": round(self.index_noop_seconds, 3),
            "index_touch_seconds": round(self.index_touch_seconds, 3),
            "tool_seconds": {name: round(value, 4) for name, value in self.tool_seconds.items()},
            "environment": self.environment,
        }

    def as_text(self) -> str:
        lines = [
            f"root:        {self.root}",
            f"files:       {self.files}",
            f"symbols:     {self.symbols}",
            f"edges:       {self.edges}",
            f"store:       {self.store_bytes / 2**20:.1f} MiB",
            f"index cold:  {self.index_cold_seconds:.2f} s",
            f"index no-op: {self.index_noop_seconds:.2f} s",
            f"index touch: {self.index_touch_seconds:.2f} s",
            "tools:",
        ]
        for name, value in self.tool_seconds.items():
            lines.append(f"  {name:<22} {value * 1000:8.1f} ms")
        return "\n".join(lines) + "\n"


def _environment(package_root: Path | None = None) -> dict[str, str]:
    info = {
        "repoatlas": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    try:
        info["numpy"] = importlib.import_module("numpy").__version__
    except ImportError:
        info["numpy"] = "absent"
    source = package_root or Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if commit.returncode == 0:
            info["commit"] = commit.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def _timed(action: Callable[[], object]) -> float:
    started = time.perf_counter()
    action()
    return time.perf_counter() - started


def run_benchmark(
    root: Path,
    store_path: Path,
    *,
    use_git: bool = True,
    focus: str | None = None,
) -> BenchResult:
    """Index ``root`` into a fresh store and time everything.

    ``store_path`` is removed first: a cold index is the point. The touched
    file is the first source file the walk yields, rewritten byte-for-byte
    with a trailing newline so its hash changes and nothing else does.
    """
    from .parse.walk import iter_source_files

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(store_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    result = BenchResult(root=str(root), environment=_environment())

    with IndexStore(store_path) as store:
        result.index_cold_seconds = _timed(lambda: update_store(root, store, use_git=use_git))
        counts = store.counts()
        result.files = counts["files"]
        result.symbols = counts["symbols"]
        result.edges = counts["edges"]
        result.store_bytes = store.size_bytes()

        result.index_noop_seconds = _timed(lambda: update_store(root, store, use_git=use_git))

        first = next(iter(iter_source_files(root, use_git=use_git)), None)
        # Timing a touched re-index means writing to a file, which needs
        # a working tree; this harness is always given one.
        if first is not None and first.absolute is not None:
            original = first.absolute.read_bytes()
            first.absolute.write_bytes(original + b"\n")
            try:
                result.index_touch_seconds = _timed(
                    lambda: update_store(root, store, use_git=use_git)
                )
            finally:
                first.absolute.write_bytes(original)
                update_store(root, store, use_git=use_git)

        result.tool_seconds = _time_tools(store, focus or (first.path if first else None))
    return result


def _time_tools(store: IndexStore, focus: str | None) -> dict[str, float]:
    """One representative call to each tool, cold and warm where it matters."""
    timings: dict[str, float] = {}
    cache = RankCache()
    timings["repo_map cold"] = _timed(lambda: tools.repo_map(store, budget=2000, cache=cache))
    timings["repo_map warm"] = _timed(lambda: tools.repo_map(store, budget=2000, cache=cache))
    if focus:
        timings["repo_map focus first"] = _timed(
            lambda: tools.repo_map(store, focus=(focus,), budget=2000, cache=cache)
        )
        timings["repo_map focus next"] = _timed(
            lambda: tools.repo_map(store, focus=(focus,), budget=2000, cache=cache)
        )
    used = store.most_referenced(limit=1)
    target = used[0] if used else None
    timings["search_symbols"] = _timed(lambda: tools.search_symbols(store, "er", limit=20))
    if target:
        timings["get_symbol"] = _timed(lambda: tools.get_symbol(store, target))
        timings["find_references"] = _timed(lambda: tools.find_references(store, target))
        timings["neighbours"] = _timed(lambda: tools.neighbours(store, target, direction="both"))
    if focus:
        timings["file_outline"] = _timed(lambda: tools.file_outline(store, focus))
    timings["index_status"] = _timed(lambda: tools.index_status(store))
    return timings


# --- a synthetic repository -----------------------------------------------

_PYTHON_MODULE = '''"""Module {index} of the synthetic repository."""

from {package}.mod_{dep_a} import Service{dep_a}
from {package}.mod_{dep_b} import helper_{dep_b}


class Service{index}:
    """A service that leans on two others."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.peer = Service{dep_a}(name)

    def run(self, value: int) -> int:
        return helper_{dep_b}(value) + self.peer.run(value)

    def _private(self) -> str:
        return self.name.upper()


def helper_{index}(value: int) -> int:
    """A free function other modules import."""
    return value * {index} % 97


CONSTANT_{index} = {index}
'''

_TYPESCRIPT_MODULE = '''import {{ Service{dep_a} }} from "./mod_{dep_a}";
import {{ helper{dep_b} }} from "./mod_{dep_b}";

export interface Shape{index} {{
  size: number;
  label(): string;
}}

export class Service{index} implements Shape{index} {{
  size = {index};
  private peer = new Service{dep_a}();

  label(): string {{
    return `service-{index}`;
  }}

  run(value: number): number {{
    return helper{dep_b}(value) + this.peer.run(value);
  }}
}}

export function helper{index}(value: number): number {{
  return (value * {index}) % 97;
}}
'''

_PHP_MODULE = '''<?php

namespace App\\Synthetic;

use App\\Synthetic\\Service{dep_a};

class Service{index}
{{
    private Service{dep_a} $peer;

    public function __construct(string $name)
    {{
        $this->peer = new Service{dep_a}($name);
    }}

    public function run(int $value): int
    {{
        return $this->peer->run($value) + helper{dep_b}($value);
    }}
}}

function helper{index}(int $value): int
{{
    return ($value * {index}) % 97;
}}
'''


def generate_synthetic(
    root: Path, files: int, *, languages: tuple[str, ...] = ("python", "typescript", "php")
) -> int:
    """Write ``files`` source files that import each other, and return the count.

    Each module depends on two others by a fixed rule, so every import
    resolves to a file the index covers and the resolver has real work to
    do. The three languages are spread evenly.
    """
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    per_language = max(1, files // max(1, len(languages)))
    for language in languages:
        for index in range(per_language):
            dep_a = (index * 7 + 1) % per_language
            dep_b = (index * 11 + 3) % per_language
            if language == "python":
                package = root / "synth"
                package.mkdir(exist_ok=True)
                (package / "__init__.py").touch()
                text = _PYTHON_MODULE.format(index=index, dep_a=dep_a, dep_b=dep_b, package="synth")
                (package / f"mod_{index}.py").write_text(text, encoding="utf-8")
            elif language == "typescript":
                folder = root / "src"
                folder.mkdir(exist_ok=True)
                text = _TYPESCRIPT_MODULE.format(index=index, dep_a=dep_a, dep_b=dep_b)
                (folder / f"mod_{index}.ts").write_text(text, encoding="utf-8")
            elif language == "php":
                folder = root / "app" / "Synthetic"
                folder.mkdir(parents=True, exist_ok=True)
                text = _PHP_MODULE.format(index=index, dep_a=dep_a, dep_b=dep_b)
                (folder / f"Service{index}.php").write_text(text, encoding="utf-8")
            else:
                raise ValueError(f"no synthetic generator for {language!r}")
            written += 1
    if "php" in languages:
        (root / "composer.json").write_text(
            json.dumps({"autoload": {"psr-4": {"App\\": "app/"}}}, indent=2), encoding="utf-8"
        )
    return written


def main_json(result: BenchResult, out: Path | None) -> None:
    payload = json.dumps(result.as_dict(), indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")
