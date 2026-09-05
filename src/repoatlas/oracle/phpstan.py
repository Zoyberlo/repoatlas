"""PHPStan as an oracle, and as a source of the types PHP never writes down.

The SCIP family covers PHP through ``scip-php``, and on a real Laravel
application it agrees with this index's blind spots rather than exposing
them: 67% of PHP callables look unused to both, because both resolve a
member only when something declares the receiver's type. Grading against an
oracle that shares your blindness measures agreement, not accuracy.

PHPStan is the PHP tool that does not share it. It carries a full type
inference engine, so it knows what ``$service`` holds without a
declaration; and with ``larastan`` loaded it also knows what an Eloquent
model's columns are, which is the one category no compiler front end sees
at all, because the property does not exist until the row is fetched.

Two things come back from a run:

*definitions* — every class, method, property and constant in the analysed
files, located by the span of its name token, which is what an oracle
comparison anchors on.

*sites* — every member access, with the type PHPStan resolved for its
receiver, whether the member resolves, which class declares it, and whether
that declaration is written down anywhere. The last flag is the interesting
one: a resolved member with no declaration is Eloquent's doing, and this is
the only tool in the pipeline that can name it.

Nothing here writes into the analysed project. PHPStan is given a generated
configuration in a scratch directory, its cache goes there too, and the
project is read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    PositionEncoding,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)

__all__ = [
    "DUMP_VARIABLE",
    "PhpStanError",
    "PhpStanRun",
    "extension_directory",
    "read_phpstan",
    "run_phpstan",
    "write_config",
]

DUMP_VARIABLE = "REPOATLAS_PHPSTAN_DUMP"
"""Where the extension writes its dump; must match ``DumpRule::PATH_VARIABLE``."""


class PhpStanError(RuntimeError):
    """A run that could not produce a dump, phrased so a caller can fix it."""


_SYMBOL_KINDS = {
    "class": SymbolKind.CLASS,
    "interface": SymbolKind.INTERFACE,
    "trait": SymbolKind.TRAIT,
    "enum": SymbolKind.ENUM,
    "method": SymbolKind.METHOD,
    "constructor": SymbolKind.CONSTRUCTOR,
    "function": SymbolKind.FUNCTION,
    "property": SymbolKind.PROPERTY,
    "constant": SymbolKind.CONSTANT,
}

_EDGE_KINDS = {
    "call": EdgeKind.CALLS,
    "static_call": EdgeKind.CALLS,
    # `new X()` is a call to a constructor and a use of a type at once;
    # both collapse into the reference-like group the comparison scores.
    "new": EdgeKind.CALLS,
    "property": EdgeKind.REFERENCES,
    "static_property": EdgeKind.REFERENCES,
    "constant": EdgeKind.REFERENCES,
}


def extension_directory() -> Path:
    """Where the PHP half of this lives, shipped as package data."""
    return Path(__file__).parent / "phpstan"


@dataclass(slots=True)
class PhpStanRun:
    """What one analysis produced, beyond the snapshot itself."""

    snapshot: IndexSnapshot
    sites: int = 0
    resolved: int = 0
    magic: int = 0
    """Sites resolved to a member that is written down nowhere."""

    linked: int = 0
    """Resolved sites whose target is a definition in the analysed files."""

    definitions: int = 0
    unresolved_names: dict[str, int] = field(default_factory=dict)

    @property
    def resolution_rate(self) -> float:
        return self.resolved / self.sites if self.sites else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "definitions": self.definitions,
            "sites": self.sites,
            "resolved": self.resolved,
            "resolution_rate": round(self.resolution_rate, 4),
            "magic": self.magic,
            "linked": self.linked,
            "edges": len(self.snapshot.edges),
            "unresolved_names": dict(
                sorted(self.unresolved_names.items(), key=lambda item: -item[1])[:20]
            ),
        }


# --- running --------------------------------------------------------------


def write_config(
    root: Path,
    work: Path,
    *,
    paths: tuple[str, ...] = (),
    level: int = 0,
    larastan: Path | None = None,
    memory_limit: str = "2G",
) -> Path:
    """Generate the configuration for one run, in the scratch directory.

    Level 0 on purpose. The rules are not wanted here at all; only the
    analyser's type resolution is, and every level above 0 buys reports
    nobody reads at the cost of a slower run.
    """
    work.mkdir(parents=True, exist_ok=True)
    analysed = paths or ("app", "src", "routes", "database", "config", "tests")
    present = [name for name in analysed if (root / name).exists()]
    if not present:
        raise PhpStanError(
            f"none of {', '.join(analysed)} exists under {root}; pass --paths"
        )
    includes = [str((extension_directory() / "extension.neon").resolve())]
    if larastan is not None:
        includes.append(str(larastan.resolve()))
    lines = ["includes:"]
    lines.extend(f"\t- {include}" for include in includes)
    lines.append("parameters:")
    lines.append(f"\tlevel: {level}")
    autoload = root / "vendor" / "autoload.php"
    if autoload.exists():
        # The one thing PHPStan cannot infer from a configuration living
        # outside the project. Without the project's own autoloader every
        # framework class is unknown and every receiver resolves to nothing,
        # which looks like a result and is an empty run.
        lines.append("\tbootstrapFiles:")
        lines.append(f"\t\t- {autoload.resolve()}")
    lines.append(f"\ttmpDir: {(work / 'cache').resolve()}")
    # PHPStan treats stdClass as a crate where every property exists, which
    # is right for its own purpose and wrong for this one: on the test
    # application it turned 3,907 accesses into resolutions that name
    # nothing. A property on stdClass is not knowledge, and counting it as
    # such would have inflated the headline by half.
    # The trailing `!` overwrites rather than merges: NEON merges arrays by
    # default, so without it the line is a no-op that reads like a fix, and
    # the first run measured with it changed nothing at all.
    lines.append("\tuniversalObjectCratesClasses!: []")
    lines.append("\tparallel:")
    # The dump is written once, from the process that holds the collected
    # data, so parallelism is safe; it is capped only because a machine
    # already running a benchmark should not be handed every core.
    lines.append("\t\tmaximumNumberOfProcesses: 4")
    lines.append("\tpaths:")
    lines.extend(f"\t\t- {(root / name).resolve()}" for name in present)
    config = work / "phpstan.neon"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _ = memory_limit
    return config


def run_phpstan(
    root: Path | str,
    work: Path | str,
    *,
    phpstan: str | None = None,
    paths: tuple[str, ...] = (),
    level: int = 0,
    larastan: Path | str | None = None,
    memory_limit: str = "2G",
    timeout: int = 3600,
) -> Path:
    """Analyse ``root`` and return the path of the dump.

    ``phpstan`` defaults to the analysed project's own
    ``vendor/bin/phpstan`` when it has one, because a project's PHPStan
    already knows its autoloader and its extensions; otherwise whatever is
    on PATH.
    """
    root = Path(root).resolve()
    work = Path(work).resolve()
    executable = phpstan or _find_phpstan(root)
    config = write_config(
        root,
        work,
        paths=paths,
        level=level,
        larastan=Path(larastan) if larastan else None,
        memory_limit=memory_limit,
    )
    dump = work / "phpstan.jsonl"
    dump.unlink(missing_ok=True)
    command = [
        executable,
        "analyse",
        "--configuration",
        str(config),
        "--autoload-file",
        str((extension_directory() / "bootstrap.php").resolve()),
        "--memory-limit",
        memory_limit,
        "--no-progress",
        "--error-format",
        "raw",
    ]
    autoload = root / "vendor" / "autoload.php"
    environment = {**os.environ, DUMP_VARIABLE: str(dump)}
    try:
        completed = subprocess.run(
            command,
            cwd=str(root if autoload.exists() else work),
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise PhpStanError(
            f"no phpstan executable at {executable!r}; install it with "
            "`composer require --dev phpstan/phpstan` or pass --phpstan"
        ) from None
    except subprocess.TimeoutExpired:
        raise PhpStanError(f"phpstan did not finish within {timeout}s") from None
    if not dump.exists():
        # A non-zero exit is normal: the analysed project has type errors,
        # and every real one does. A missing dump is not.
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = "\n".join(detail[-8:]) or "no output"
        raise PhpStanError(f"phpstan wrote no dump (exit {completed.returncode}):\n{tail}")
    return dump


def _find_phpstan(root: Path) -> str:
    vendored = root / "vendor" / "bin" / "phpstan"
    if vendored.exists():
        return str(vendored)
    found = shutil.which("phpstan")
    if found:
        return found
    raise PhpStanError(
        f"no phpstan in {root / 'vendor' / 'bin'} and none on PATH; install it with "
        "`composer require --dev phpstan/phpstan` or pass --phpstan"
    )


# --- reading --------------------------------------------------------------


class _Offsets:
    """Byte offsets to line and column, for one file, read once.

    The extension reports byte offsets because that is what PhpParser has
    and because the index this is compared against counts UTF-8 bytes
    within a line. Converting here, from a file already on disk, avoids two
    producers each guessing at an encoding.
    """

    __slots__ = ("starts",)

    def __init__(self, text: bytes) -> None:
        starts = [0]
        position = text.find(b"\n")
        while position >= 0:
            starts.append(position + 1)
            position = text.find(b"\n", position + 1)
        self.starts = starts

    def at(self, offset: int) -> tuple[int, int]:
        line = bisect_right(self.starts, offset) - 1
        return line, offset - self.starts[line]


def _span(offsets: _Offsets | None, record: dict[str, Any]) -> SourceRange | None:
    start, end = record.get("start"), record.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0:
        return None
    if offsets is None:
        # No file to measure against: the line is still known, and a span
        # covering the whole of it matches on any policy but "exact".
        line = int(record.get("line", 0))
        return SourceRange.of(line, 0, line, 0)
    start_line, start_character = offsets.at(start)
    end_line, end_character = offsets.at(max(start, end))
    return SourceRange.of(start_line, start_character, end_line, end_character)


def read_phpstan(dump: Path | str, root: Path | str) -> PhpStanRun:
    """Turn a dump into a snapshot comparable with an index of the same tree.

    Paths arrive absolute, because PHPStan analyses absolute paths, and are
    made relative to ``root``; anything outside it is dropped, which is how
    ``vendor`` stays out of a comparison about this repository's code.
    """
    root = Path(root).resolve()
    records = _read_records(Path(dump))
    offsets: dict[str, _Offsets] = {}

    def measure(path: str) -> _Offsets | None:
        if path not in offsets:
            try:
                offsets[path] = _Offsets((root / path).read_bytes())
            except OSError:
                return None
        return offsets.get(path)

    snapshot = IndexSnapshot(
        encoding=PositionEncoding.UTF8, producer="phpstan", project_root=str(root)
    )
    run = PhpStanRun(snapshot=snapshot)
    # A definition may arrive several times under different names: a trait's
    # method is analysed once per class that uses it. One symbol, many
    # names, and the extra names are exactly what makes the join work.
    by_fqn: dict[str, str] = {}
    definitions = [record for record in records if record.get("kind") == "def"]
    for record in definitions:
        path = _relative(record.get("path", ""), root)
        if path is None:
            continue
        span = _span(measure(path), record)
        if span is None:
            continue
        kind = _SYMBOL_KINDS.get(str(record.get("symbol_kind", "")), SymbolKind.UNKNOWN)
        symbol_id = f"{path}#{span.start.line}:{span.start.character}"
        if symbol_id not in snapshot.symbols:
            snapshot.add_symbol(
                Symbol(
                    id=symbol_id,
                    name=str(record.get("name", "")),
                    kind=kind,
                    path=path,
                    name_range=span,
                    language="php",
                )
            )
            run.definitions += 1
        fqn = str(record.get("fqn", ""))
        if fqn:
            by_fqn.setdefault(fqn, symbol_id)

    modules: dict[str, str] = {}
    for record in records:
        if record.get("kind") == "def":
            continue
        path = _relative(record.get("path", ""), root)
        if path is None:
            continue
        run.sites += 1
        if not record.get("resolved"):
            name = str(record.get("name", ""))
            run.unresolved_names[name] = run.unresolved_names.get(name, 0) + 1
            continue
        run.resolved += 1
        if record.get("magic"):
            run.magic += 1
        target = _target(record, by_fqn)
        if target is None:
            continue
        run.linked += 1
        span = _span(measure(path), record)
        if span is None:
            continue
        snapshot.add_edge(
            Edge(
                src_id=_module(snapshot, modules, path),
                dst_id=target,
                kind=_EDGE_KINDS.get(str(record.get("kind")), EdgeKind.REFERENCES),
                tier=ResolutionTier.ORACLE,
                site_path=path,
                site_range=span,
            )
        )
    return run


def _target(record: dict[str, Any], by_fqn: dict[str, str]) -> str | None:
    """The definition a resolved site reaches, if it is in the analysed files."""
    owner = str(record.get("class", ""))
    if not owner:
        return None
    if record.get("kind") == "new":
        return by_fqn.get(owner)
    return by_fqn.get(f"{owner}::{record.get('name', '')}")


def _module(snapshot: IndexSnapshot, modules: dict[str, str], path: str) -> str:
    """A stand-in symbol for a file, to give every edge a source.

    PHPStan reports where a use site is but not which function encloses it,
    and the comparison only reads the source of an edge when the edge has
    no site of its own. These always have one.
    """
    existing = modules.get(path)
    if existing is not None:
        return existing
    module_id = f"phpstan-module {path}"
    origin = SourceRange.of(0, 0, 0, 0)
    snapshot.add_symbol(
        Symbol(
            id=module_id,
            name=path.rsplit("/", 1)[-1],
            kind=SymbolKind.MODULE,
            path=path,
            name_range=origin,
            full_range=origin,
            language="php",
            synthetic=True,
        )
    )
    modules[path] = module_id
    return module_id


def _relative(path: str, root: Path) -> str | None:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _read_records(dump: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        text = dump.read_text(encoding="utf-8")
    except OSError as exc:
        raise PhpStanError(f"cannot read {dump}: {exc}") from None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PhpStanError(f"{dump}:{number} is not JSON: {exc}") from None
        if isinstance(record, dict):
            records.append(record)
    return records
