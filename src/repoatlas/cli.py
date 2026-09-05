"""Command line entry point.

Three commands, matching the three things you do with an oracle:

``inspect``
    Read an index and print what is in it. The first thing to run against a
    new indexer, because an empty or path-mangled index looks like a broken
    extractor otherwise.

``verify-oracle``
    Check that this project's binary SCIP reader agrees with the SCIP CLI on
    the same index. Run once per indexer version before trusting any number
    the binary path produces.

``index``
    Parse a repository and report what came out, including the per-language
    syntax-error rate, which is the first thing to check before believing
    any accuracy number from that language. With ``--store`` it writes a
    SQLite index and re-parses only what changed since last time.

``search``
    Look a symbol up in a stored index, by substring.

``map``
    Render the most important symbols of a repository within a token
    budget. With ``--focus`` the ranking is steered toward the files
    being worked on, which turns a map of the repository into a map of
    the task.

``calibrate``
    Count a few real answers with the provider's tokenizer and record the
    constant in the index, so a budget of two thousand tokens means two
    thousand on the model that will read them.

``bench``
    Index a repository cold, again untouched, again with one file changed,
    and time every tool, writing the result as JSON beside the commit. With
    ``--synthetic N`` it first generates a repository of that many files.

``localize``
    Score the map against the repository's own history: for each recent
    commit, does a map of the tree *before* it, drawn around the words of
    its message, name the symbols the commit went on to change. Walks a
    scratch clone so the repository it reads is never touched. The number
    the ranking weights are settled against.

``serve``
    Run the MCP server over stdio, so an agent can query the index
    directly.

``compare``
    Score one index against another and write the report. Either side may
    be a repository directory, which is parsed on the spot, so scoring the
    extractor against a compiler-backed oracle is a single command.

The harness itself needs nothing beyond the standard library, so oracle
comparison runs in CI without a parser toolchain. Only ``index``, and
``compare`` when handed a directory, require the ``parse`` extra.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .eval.compare import ComparisonOptions, compare_snapshots
from .eval.report import to_json, to_markdown
from .model import IndexSnapshot
from .oracle.phpstan import PhpStanError, read_phpstan, run_phpstan
from .oracle.scip import ScipError, cross_check, read_scip

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from .parse.build import BuildResult

__all__ = ["build_parser", "main"]

_EXIT_OK = 0
_EXIT_FAILED_CHECK = 1
# Malformed arguments exit 2, which argparse does on its own.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repoatlas",
        description="Oracle-verified code index tooling.",
    )
    parser.add_argument("--version", action="version", version=f"repoatlas {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    inspect = subcommands.add_parser("inspect", help="summarise an index")
    inspect.add_argument("index", type=Path, help="a .scip file or scip print --json output")
    inspect.add_argument(
        "--paths", action="store_true", help="list every file the index covers"
    )

    verify = subcommands.add_parser(
        "verify-oracle",
        help="check the binary SCIP reader against the SCIP CLI on one index",
    )
    verify.add_argument("binary", type=Path, help="the .scip file")
    verify.add_argument("json_dump", type=Path, help="output of scip print --json")

    index = subcommands.add_parser("index", help="parse a repository and report on it")
    index.add_argument("root", type=Path, help="the repository to parse")
    index.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    index.add_argument(
        "--no-git",
        action="store_true",
        help="walk the filesystem instead of asking git which files are tracked",
    )
    index.add_argument(
        "--max-error-rate",
        type=float,
        help="exit non-zero when more than this share of files fail to parse",
    )
    index.add_argument(
        "--store",
        type=Path,
        help="write to this SQLite index, re-parsing only what changed",
    )
    index.add_argument(
        "--rehash",
        action="store_true",
        help="hash every file instead of trusting size and mtime",
    )

    search = subcommands.add_parser("search", help="find a symbol in a stored index")
    search.add_argument("store", type=Path, help="the SQLite index to read")
    search.add_argument("query", help="a substring of the symbol name")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument(
        "--kind",
        action="append",
        default=[],
        help="restrict to a symbol kind; repeatable",
    )
    search.add_argument("--format", choices=("text", "json"), default="text")

    agentbench = subcommands.add_parser(
        "agentbench",
        help="tier 4: the same tasks through Claude Code with and without the index",
    )
    agentbench.add_argument("root", type=Path, help="a git repository with history")
    agentbench.add_argument("--work", type=Path, help="scratch clone; a temp dir by default")
    agentbench.add_argument("--commits", type=int, default=20, help="recent commits to pose")
    agentbench.add_argument(
        "--arms", default="grep,repoatlas", help="comma-separated: grep, repoatlas"
    )
    agentbench.add_argument("--repeats", type=int, default=1, help="runs per task and arm")
    agentbench.add_argument("--claude", default="claude", help="the Claude Code executable")
    agentbench.add_argument("--serena", help="the serena executable, for the serena arm")
    agentbench.add_argument("--model", help="model for the agent; the CLI default otherwise")
    agentbench.add_argument("--max-turns", type=int, default=30)
    agentbench.add_argument("--max-files", type=int, default=8)
    agentbench.add_argument("--timeout", type=int, default=900, help="seconds per run")
    agentbench.add_argument("--out", type=Path, help="write the JSON result here")
    agentbench.add_argument(
        "--resume",
        type=Path,
        help="a previous --out --with-runs file; its scored runs are kept and not "
        "paid for again, which is how a walk survives a usage window",
    )
    agentbench.add_argument(
        "--budget",
        type=int,
        default=2000,
        help="tokens of context for the map and skeleton arms",
    )
    agentbench.add_argument(
        "--with-runs", action="store_true", help="include every run in the JSON"
    )
    agentbench.add_argument("--format", choices=("text", "json"), default="text")

    sitebench = subcommands.add_parser(
        "sitebench",
        help="tier 4, the other question: who uses this symbol, scored against a SCIP oracle",
    )
    sitebench.add_argument("root", type=Path, help="a working tree the oracle describes")
    sitebench.add_argument("oracle", type=Path, help="a .scip index of that tree")
    sitebench.add_argument("--store", type=Path, help="index to serve; built beside the oracle")
    sitebench.add_argument("--arms", default="grep,repoatlas")
    sitebench.add_argument("--limit", type=int, default=20, help="symbols to ask about")
    sitebench.add_argument("--repeats", type=int, default=1)
    sitebench.add_argument("--claude", default="claude")
    sitebench.add_argument("--serena", help="the serena executable, for the serena arm")
    sitebench.add_argument("--model")
    sitebench.add_argument("--max-turns", type=int, default=30)
    sitebench.add_argument("--timeout", type=int, default=900)
    sitebench.add_argument("--out", type=Path)
    sitebench.add_argument("--with-runs", action="store_true")
    sitebench.add_argument("--format", choices=("text", "json"), default="text")

    tokens = subcommands.add_parser(
        "tokens", help="where an index's tokens go: the skeleton's cost by directory"
    )
    tokens.add_argument("store", type=Path, help="the SQLite index to read")
    tokens.add_argument("--depth", type=int, default=2, help="directory levels to show")
    tokens.add_argument("--top", type=int, default=25, help="entries per level")
    tokens.add_argument(
        "--max-total",
        type=int,
        help="exit 1 when the whole skeleton costs more tokens than this; a CI gate",
    )
    tokens.add_argument("--format", choices=("text", "json"), default="text")

    repo_map = subcommands.add_parser(
        "map", help="render a ranked, budgeted map of a repository"
    )
    repo_map.add_argument(
        "source", type=Path, help="a repository to parse, or a stored index"
    )
    repo_map.add_argument(
        "--budget", type=int, default=2000, help="target size in tokens"
    )
    repo_map.add_argument(
        "--focus",
        action="append",
        default=[],
        help="a file to rank around; repeatable",
    )
    repo_map.add_argument(
        "--mention",
        action="append",
        default=[],
        help="a symbol name or file stem the task talks about; repeatable",
    )
    repo_map.add_argument(
        "--max-files", type=int, default=0, help="list at most this many files"
    )
    repo_map.add_argument(
        "--show-scores", action="store_true", help="append each entry's rank"
    )
    repo_map.add_argument(
        "--chars-per-token",
        type=float,
        help="calibrate the token estimate for a particular model",
    )
    repo_map.add_argument("--out", type=Path, help="write the map here")
    repo_map.add_argument(
        "--no-git",
        action="store_true",
        help="walk the filesystem instead of asking git",
    )
    repo_map.add_argument("--format", choices=("text", "json"), default="text")

    calibrate = subcommands.add_parser(
        "calibrate",
        help="measure the token estimate against a model's real tokenizer",
    )
    calibrate.add_argument("store", type=Path, help="the SQLite index to calibrate")
    calibrate.add_argument(
        "--model",
        required=True,
        help="the model that will read the answers, e.g. claude-fable-5-1",
    )
    calibrate.add_argument(
        "--api-key-env",
        default="ANTHROPIC_API_KEY",
        help="environment variable holding the API key",
    )
    calibrate.add_argument("--format", choices=("text", "json"), default="text")

    bench = subcommands.add_parser(
        "bench", help="index a repository cold, warm and touched, and time every tool"
    )
    bench.add_argument("root", type=Path, help="the repository to measure")
    bench.add_argument(
        "--synthetic",
        type=int,
        metavar="FILES",
        help="generate this many synthetic source files into ROOT first",
    )
    bench.add_argument("--store", type=Path, help="where to write the index (removed first)")
    bench.add_argument("--focus", help="a file to use for the focused map and outline")
    bench.add_argument("--out", type=Path, help="write the JSON result here")
    bench.add_argument("--format", choices=("text", "json"), default="text")
    bench.add_argument(
        "--no-git", action="store_true", help="walk the filesystem instead of asking git"
    )

    localize = subcommands.add_parser(
        "localize",
        help="score the map against the repository's own commit history",
    )
    localize.add_argument("root", type=Path, help="a git repository")
    localize.add_argument(
        "--work",
        type=Path,
        help="where to keep the scratch clone; defaults beside the index",
    )
    localize.add_argument("--commits", type=int, default=200, help="how many commits to walk")
    localize.add_argument("--budget", type=int, default=2000, help="map budget in tokens")
    localize.add_argument(
        "--max-files",
        type=int,
        default=8,
        help="skip commits touching more files than this; they are refactors",
    )
    localize.add_argument(
        "--spread",
        type=float,
        default=None,
        help="override the map's per-file spread, to measure it",
    )
    localize.add_argument("--out", type=Path, help="write the JSON result here")
    localize.add_argument(
        "--with-cases",
        action="store_true",
        help="include per-commit subjects in the JSON; off because they are "
        "the repository's own content",
    )
    localize.add_argument("--format", choices=("text", "json"), default="text")

    serve = subcommands.add_parser("serve", help="run the MCP server over stdio")
    serve.add_argument("root", type=Path, help="the repository to serve")
    serve.add_argument(
        "--store",
        type=Path,
        help="where to keep the index; defaults to .repoatlas/index.db in the root",
    )
    serve.add_argument(
        "--no-refresh",
        action="store_true",
        help="serve the stored index as it is, without re-indexing first",
    )
    serve.add_argument(
        "--no-git",
        action="store_true",
        dest="serve_no_git",
        help="walk the filesystem instead of asking git which files are tracked",
    )

    phpstan = subcommands.add_parser(
        "phpstan",
        help="resolve a PHP project with PHPStan, as an oracle and a type source",
    )
    phpstan.add_argument("root", type=Path, help="the PHP project to analyse")
    phpstan.add_argument(
        "--work",
        type=Path,
        help="scratch directory for the generated config and cache "
        "(default: a temporary one; nothing is written into the project)",
    )
    phpstan.add_argument(
        "--out", type=Path, help="write the dump here instead of leaving it in --work"
    )
    phpstan.add_argument("--phpstan", help="the phpstan executable")
    phpstan.add_argument(
        "--larastan",
        type=Path,
        help="path to larastan's extension.neon, for Eloquent's undeclared columns",
    )
    phpstan.add_argument(
        "--paths",
        nargs="*",
        default=(),
        help="directories under the root to analyse (default: the usual Laravel ones)",
    )
    phpstan.add_argument("--level", type=int, default=0)
    phpstan.add_argument("--memory-limit", default="2G")
    phpstan.add_argument("--timeout", type=int, default=3600)
    phpstan.add_argument(
        "--read",
        type=Path,
        help="read an existing dump instead of running phpstan",
    )
    phpstan.add_argument("--format", choices=("text", "json"), default="text")

    enrich = subcommands.add_parser(
        "enrich",
        help="fold a type engine's answers into an index that could not infer them",
    )
    enrich.add_argument("store", type=Path, help="the index to add edges to")
    enrich.add_argument("--phpstan", type=Path, required=True, help="a phpstan dump")
    enrich.add_argument(
        "--phpstan-root",
        type=Path,
        required=True,
        help="the directory phpstan analysed; its paths are relative to this",
    )
    enrich.add_argument(
        "--prefix",
        default="",
        help="where that directory sits in the indexed repository, e.g. backend",
    )
    enrich.add_argument(
        "--dry-run", action="store_true", help="report what would be added, add nothing"
    )
    enrich.add_argument("--format", choices=("text", "json"), default="text")

    compare = subcommands.add_parser("compare", help="score a candidate index against an oracle")
    compare.add_argument(
        "candidate", type=Path, help="a SCIP index, or a repository to parse"
    )
    compare.add_argument("oracle", type=Path)
    compare.add_argument(
        "--format", choices=("markdown", "json"), default="markdown"
    )
    compare.add_argument("--out", type=Path, help="write the report here instead of stdout")
    compare.add_argument(
        "--policy",
        choices=("overlap", "exact", "line"),
        default="overlap",
        help="how strictly a predicted location must match the oracle",
    )
    compare.add_argument(
        "--all-paths",
        action="store_true",
        help="score files the oracle does not cover, instead of skipping them",
    )
    compare.add_argument(
        "--fold-case",
        action="store_true",
        help="treat paths differing only in case as the same file",
    )
    compare.add_argument(
        "--split-kinds",
        action="store_true",
        help="score each edge kind separately instead of grouping reference-like ones",
    )
    compare.add_argument("--resamples", type=int, default=2000)
    compare.add_argument("--seed", type=int, default=20260903)
    compare.add_argument(
        "--min-f1",
        type=float,
        help="exit non-zero when reference F1 falls below this, for use as a CI gate",
    )
    return parser


def _build(root: Path, *, use_git: bool = True) -> BuildResult:
    """Parse a repository, reporting a missing parser extra clearly.

    The import is deferred so the oracle commands keep working when the
    parse extra is not installed.
    """
    try:
        from .parse.build import build_snapshot
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise SystemExit(
            f"repoatlas: parsing needs the extra: pip install 'repoatlas[parse]' ({exc})"
        ) from None
    try:
        return build_snapshot(root, use_git=use_git)
    except NotADirectoryError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None


def _load(path: Path, root: Path | None = None) -> IndexSnapshot:
    """Read a SCIP index or a PHPStan dump, or parse a directory into one."""
    if path.is_dir():
        return _build(path).snapshot
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        # A PHPStan dump holds absolute paths, so it needs to be told which
        # root they are relative to; the candidate's is the right one, since
        # the comparison is only meaningful over the same tree.
        try:
            return read_phpstan(path, root or path.parent).snapshot
        except PhpStanError as exc:
            raise SystemExit(f"repoatlas: {exc}") from None
    try:
        return read_scip(path)
    except FileNotFoundError:
        raise SystemExit(f"repoatlas: no such file: {path}") from None
    except ScipError as exc:
        raise SystemExit(f"repoatlas: cannot read {path}: {exc}") from None


def _cmd_index(args: argparse.Namespace) -> int:
    if args.store is not None:
        return _cmd_index_store(args)
    result = _build(args.root, use_git=not args.no_git)
    if args.format == "json":
        print(json.dumps(result.as_dict(), indent=2))
    else:
        real = sum(1 for s in result.snapshot.symbols.values() if not s.synthetic)
        print(f"files:      {result.files}")
        print(f"symbols:    {real}")
        resolution = result.resolution
        print(
            f"references: {len(result.references)} "
            f"({resolution.resolution_rate:.0%} resolved)"
        )
        print(f"edges:      {len(result.snapshot.edges)}")
        print(
            f"elapsed:    {result.duration_seconds:.2f}s parse "
            f"+ {result.resolve_seconds:.2f}s resolve "
            f"({result.throughput():.0f} files/s)"
        )
        if resolution.resolved:
            print()
            print("resolution tiers")
            for tier, count in sorted(
                resolution.by_tier.items(), key=lambda pair: -pair[1]
            ):
                share = count / resolution.resolved
                print(f"  {tier:<14}{count:>7}{share:>8.0%}")
            if resolution.external:
                print(f"  {'external':<14}{resolution.external:>7}")
            if resolution.unresolved:
                print(f"  {'unresolved':<14}{resolution.unresolved:>7}")
        if result.by_language:
            print()
            print(f"{'language':<12}{'files':>7}{'symbols':>9}{'refs':>8}{'errors':>9}")
            for name, stats in sorted(result.by_language.items()):
                print(
                    f"{name:<12}{stats.files:>7}{stats.symbols:>9}"
                    f"{stats.references:>8}{stats.error_rate:>8.1%}"
                )
        skipped = result.walk
        if skipped.too_large or skipped.unreadable:
            print()
            print(
                f"skipped: {skipped.too_large} too large, "
                f"{skipped.unreadable} unreadable"
            )
        if result.failures:
            print()
            print(f"{len(result.failures)} file(s) failed to parse:", file=sys.stderr)
            for path, reason in result.failures[:10]:
                print(f"  - {path}: {reason}", file=sys.stderr)
    if args.max_error_rate is not None and result.error_rate > args.max_error_rate:
        print(
            f"parse error rate {result.error_rate:.1%} exceeds the allowed "
            f"{args.max_error_rate:.1%}",
            file=sys.stderr,
        )
        return _EXIT_FAILED_CHECK
    return _EXIT_OK


def _cmd_index_store(args: argparse.Namespace) -> int:
    """Index into a SQLite store, parsing only what changed."""
    try:
        from .store import IndexStore, update_store
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise SystemExit(f"repoatlas: {exc}") from None
    from .store import StoreError

    try:
        store = IndexStore(args.store)
    except StoreError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    with store:
        try:
            result = update_store(
                args.root,
                store,
                use_git=not args.no_git,
                trust_mtime=not args.rehash,
            )
        except NotADirectoryError as exc:
            raise SystemExit(f"repoatlas: {exc}") from None
        counts = store.counts()
        if args.format == "json":
            payload = result.as_dict()
            payload["store"] = {**counts, "bytes": store.size_bytes()}
            print(json.dumps(payload, indent=2))
        else:
            changes = result.changes
            if changes.full_rebuild and not changes.was_empty:
                print("parser or queries changed; rebuilt from scratch")
            print(
                f"changed:    {len(changes.added)} added, "
                f"{len(changes.modified)} modified, "
                f"{len(changes.removed)} removed, "
                f"{len(changes.unchanged)} unchanged"
            )
            print(f"parsed:     {result.parsed} files")
            print(
                f"stored:     {counts['files']} files, {counts['symbols']} symbols, "
                f"{counts['edges']} edges ({store.size_bytes() / 1024:.0f} KiB)"
            )
            print(
                f"elapsed:    {result.parse_seconds:.2f}s parse "
                f"+ {result.resolve_seconds:.2f}s resolve"
            )
            if result.failures:
                print(
                    f"{len(result.failures)} file(s) failed to parse:",
                    file=sys.stderr,
                )
                for path, reason in result.failures[:10]:
                    print(f"  - {path}: {reason}", file=sys.stderr)
        if (
            args.max_error_rate is not None
            and _error_rate(result) > args.max_error_rate
        ):
            print(
                f"parse error rate {_error_rate(result):.1%} exceeds the allowed "
                f"{args.max_error_rate:.1%}",
                file=sys.stderr,
            )
            return _EXIT_FAILED_CHECK
    return _EXIT_OK


def _error_rate(result: object) -> float:
    """Share of freshly parsed files that had a syntax error."""
    stats = getattr(result, "by_language", {}).values()
    files = sum(item.files for item in stats)
    failed = sum(item.files_with_errors for item in stats)
    return failed / files if files else 0.0


def _cmd_agentbench(args: argparse.Namespace) -> int:
    """Pose recent commits to Claude Code with and without the index, and score both."""
    import tempfile

    from .agentbench import AgentRun, iter_arms, run_agentbench

    _AGENT_RUN_FIELDS = {field.name for field in dataclasses.fields(AgentRun)}
    from .localize import HistoryError

    root: Path = args.root.resolve()
    if not (root / ".git").exists():
        raise SystemExit(f"repoatlas: not a git repository: {root}")
    work = args.work or Path(tempfile.gettempdir()) / f"repoatlas-agentbench-{root.name}"

    def progress(run: AgentRun) -> None:
        state = f"recall {run.symbol_recall:.2f}, {run.tokens} tokens" if run.ok else run.reason
        print(f"{run.sha} {run.arm:>9} #{run.repeat}: {state}", file=sys.stderr)

    earlier: list[AgentRun] = []
    if args.resume is not None and args.resume.exists():
        earlier = [
            AgentRun(
                **{
                    key: value
                    for key, value in row.items()
                    if key in _AGENT_RUN_FIELDS and key != "tool_calls"
                },
                tool_calls=dict(row.get("tool_calls") or {}),
            )
            for row in json.loads(args.resume.read_text(encoding="utf-8")).get("runs", [])
            if row.get("ok")
        ]
        print(f"resuming past {len(earlier)} scored run(s)", file=sys.stderr)
    try:
        result = run_agentbench(
            root,
            work=work,
            commits=args.commits,
            arms=tuple(iter_arms(args.arms)),
            repeats=max(1, args.repeats),
            claude=args.claude,
            model=args.model,
            max_turns=args.max_turns,
            max_files=args.max_files,
            timeout=args.timeout,
            serena=args.serena,
            budget=args.budget,
            done={(run.sha, run.arm, run.repeat) for run in earlier},
            progress=progress,
        )
    except HistoryError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    # The resumed runs are part of the answer, not history: put them back
    # before anything is scored or written.
    result.runs = [*earlier, *result.runs]
    if args.format == "json" or args.out:
        payload = json.dumps(result.as_dict(include_runs=args.with_runs), indent=2) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload, encoding="utf-8")
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(payload, end="")
    if args.format == "text":
        print(result.as_text(), end="")
    return _EXIT_OK


def _cmd_sitebench(args: argparse.Namespace) -> int:
    """Ask both arms where a symbol is used, and score against a compiler."""
    from .agentbench import iter_arms
    from .callsites import SiteRun, run_sitebench
    from .localize import HistoryError
    from .store import IndexStore, update_store

    root: Path = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"repoatlas: not a directory: {root}")
    if not args.oracle.exists():
        raise SystemExit(f"repoatlas: no such oracle: {args.oracle}")
    store_path = args.store or args.oracle.with_suffix(".sitebench.db")
    with IndexStore(store_path) as store:
        update_store(root, store, use_git=False)

    def progress(run: SiteRun) -> None:
        state = f"F1 {run.f1:.2f} (P {run.precision:.2f} R {run.recall:.2f})" if run.ok else run.reason
        print(f"{run.name[:24]:<24} {run.arm:>9}: {state}", file=sys.stderr)

    try:
        result = run_sitebench(
            root,
            args.oracle,
            store=store_path,
            arms=tuple(iter_arms(args.arms)),
            limit=args.limit,
            repeats=max(1, args.repeats),
            claude=args.claude,
            model=args.model,
            max_turns=args.max_turns,
            timeout=args.timeout,
            serena=args.serena,
            progress=progress,
        )
    except HistoryError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    if args.format == "json" or args.out:
        payload = json.dumps(result.as_dict(include_runs=args.with_runs), indent=2) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload, encoding="utf-8")
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(payload, end="")
    if args.format == "text":
        print(result.as_text(), end="")
    return _EXIT_OK


def _cmd_tokens(args: argparse.Namespace) -> int:
    """Print the skeleton's token cost per directory, and gate on the total."""
    from .cost import token_tree
    from .store import IndexStore, StoreError

    if not args.store.exists():
        raise SystemExit(f"repoatlas: no such index: {args.store}")
    try:
        store = IndexStore(args.store)
    except StoreError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    with store:
        tree = token_tree(store, depth=max(1, args.depth))
    if args.format == "json":
        print(json.dumps(tree.as_dict(), indent=2))
    else:
        print(tree.as_text(top=max(1, args.top)), end="")
    if args.max_total is not None and tree.total > args.max_total:
        print(
            f"skeleton costs {tree.total} tokens, above the {args.max_total} allowed",
            file=sys.stderr,
        )
        return _EXIT_FAILED_CHECK
    return _EXIT_OK


def _cmd_search(args: argparse.Namespace) -> int:
    """Look a symbol up in a stored index."""
    from .store import IndexStore, StoreError

    if not args.store.exists():
        raise SystemExit(f"repoatlas: no such index: {args.store}")
    try:
        store = IndexStore(args.store)
    except StoreError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    with store:
        hits = store.search(args.query, limit=args.limit, kinds=tuple(args.kind))
        if args.format == "json":
            print(
                json.dumps(
                    [
                        {
                            "id": symbol.id,
                            "name": symbol.name,
                            "kind": symbol.kind.value,
                            "path": symbol.path,
                            "line": symbol.name_range.start.line + 1,
                            "qualified_name": symbol.qualified_name,
                        }
                        for symbol in hits
                    ],
                    indent=2,
                )
            )
        elif not hits:
            print(f"nothing matches {args.query!r}")
        else:
            for symbol in hits:
                print(
                    f"{symbol.kind.value:<12}"
                    f"{symbol.qualified_name or symbol.name:<40}"
                    f"{symbol.path}:{symbol.name_range.start.line + 1}"
                )
    return _EXIT_OK


def _cmd_map(args: argparse.Namespace) -> int:
    """Render a ranked map of a repository, within a token budget."""
    from .rank import MapOptions, RankOptions, make_estimator, rank_symbols, render_map

    estimator = (
        make_estimator(args.chars_per_token) if args.chars_per_token else None
    )
    if args.source.is_dir():
        snapshot = _build(args.source, use_git=not args.no_git).snapshot
    else:
        from .store import IndexStore, StoreError

        if not args.source.exists():
            raise SystemExit(f"repoatlas: no such repository or index: {args.source}")
        try:
            with IndexStore(args.source) as store:
                snapshot = store.snapshot()
                # A stored calibration is what the store's tools would use,
                # so the CLI map should fit the same budget the same way.
                estimator = estimator or store.estimator()
        except StoreError as exc:
            raise SystemExit(f"repoatlas: {exc}") from None

    focus_paths = {_normalise_focus(item) for item in args.focus}
    focus_symbols: set[str] = set()
    if args.mention:
        wanted = {item.strip().lower() for item in args.mention if item.strip()}
        for symbol in snapshot.symbols.values():
            if symbol.synthetic or symbol.local:
                continue
            stem = symbol.path.rsplit("/", 1)[-1].split(".", 1)[0].lower()
            if symbol.name.lower() in wanted:
                focus_symbols.add(symbol.id)
            if stem in wanted:
                focus_paths.add(symbol.path)
    ranked = rank_symbols(
        snapshot, focus_paths=focus_paths, focus_symbols=focus_symbols, options=RankOptions()
    )
    rendered = render_map(
        ranked,
        MapOptions(
            budget=args.budget,
            max_files=args.max_files,
            show_scores=args.show_scores,
        ),
        estimator=estimator,
    )

    if args.format == "json":
        payload = rendered.as_dict()
        payload["text"] = rendered.text
        output = json.dumps(payload, indent=2)
    else:
        output = rendered.text
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(output, end="" if output.endswith("\n") else "\n")
    if args.format == "text":
        print(
            f"\n{rendered.tokens} tokens, {rendered.included} of "
            f"{rendered.total} symbols, {rendered.files} files",
            file=sys.stderr,
        )
    return _EXIT_OK


def _normalise_focus(value: str) -> str:
    """Accept a focus path however the shell spelled it.

    Symbols carry repository-relative POSIX paths, so a Windows
    separator or a leading `./` from tab completion would otherwise
    match nothing and silently produce an unfocused map.
    """
    cleaned = value.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _cmd_calibrate(args: argparse.Namespace) -> int:
    """Pin the token estimate to a model's tokenizer and record it."""
    import os

    from .rank import calibrate as calibration
    from .store import IndexStore, StoreError

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise SystemExit(
            f"repoatlas: no API key in ${args.api_key_env}; the counting endpoint "
            "is free but needs one"
        )
    if not args.store.exists():
        raise SystemExit(f"repoatlas: no such index: {args.store}")
    try:
        with IndexStore(args.store) as store:
            before = store.chars_per_token()
            result = calibration.calibrate_store(
                store, args.model, calibration.anthropic_counter(args.model, api_key)
            )
    except StoreError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    except calibration.CalibrationError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None

    if args.format == "json":
        print(json.dumps(result.as_dict(), indent=2))
        return _EXIT_OK
    print(f"model:            {result.model}")
    print(f"samples:          {result.samples} ({result.counted_tokens} tokens counted)")
    print(f"default estimate: {result.estimated_before} tokens ({result.error_before:+.1%})")
    if before:
        print(f"previous:         {before:.3f} chars per token")
    print(f"calibrated:       {result.chars_per_token:.3f} chars per token, recorded in the index")
    return _EXIT_OK


def _cmd_bench(args: argparse.Namespace) -> int:
    """Measure the pipeline on one repository and report it."""
    from .bench import generate_synthetic, main_json, run_benchmark

    root: Path = args.root
    if args.synthetic:
        written = generate_synthetic(root, args.synthetic)
        print(f"generated {written} files under {root}", file=sys.stderr)
    if not root.is_dir():
        raise SystemExit(f"repoatlas: not a directory: {root}")
    store_path = args.store or root / ".repoatlas" / "bench.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_benchmark(
        root, store_path, use_git=not args.no_git and not args.synthetic, focus=args.focus
    )
    if args.format == "json" or args.out:
        main_json(result, args.out)
        if args.out:
            print(f"wrote {args.out}", file=sys.stderr)
    if args.format == "text":
        print(result.as_text(), end="")
    return _EXIT_OK


def _cmd_localize(args: argparse.Namespace) -> int:
    """Measure whether the map names the code recent commits changed."""
    import tempfile

    from .localize import HistoryError, LocalizeResult, to_json, walk
    from .rank import MapOptions

    root: Path = args.root.resolve()
    if not (root / ".git").exists():
        raise SystemExit(f"repoatlas: not a git repository: {root}")
    work = args.work or Path(tempfile.gettempdir()) / f"repoatlas-localize-{root.name}"
    map_options = MapOptions(budget=args.budget)
    if args.spread is not None:
        map_options = MapOptions(budget=args.budget, spread=args.spread)

    result = LocalizeResult(budget=args.budget)
    try:
        for _commit, case in walk(
            root,
            work=work,
            commits=args.commits,
            budget=args.budget,
            max_files=args.max_files,
            map_options=map_options,
        ):
            result.walked += 1
            if case.symbols:
                result.cases.append(case)
            if result.walked % 25 == 0:
                print(
                    f"walked {result.walked}, scored {len(result.cases)}",
                    file=sys.stderr,
                )
    except HistoryError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None

    if args.format == "json" or args.out:
        payload = to_json(result, include_cases=args.with_cases)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload, encoding="utf-8")
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(payload, end="")
    if args.format == "text":
        print(result.as_text(), end="")
    return _EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the MCP server over stdio."""
    from .server.app import serve as run_server

    root = args.root
    if not root.is_dir():
        raise SystemExit(f"repoatlas: not a directory: {root}")
    store_path = args.store or root / ".repoatlas" / "index.db"
    try:
        run_server(
            root,
            store_path,
            refresh=not args.no_refresh,
            use_git=not args.serve_no_git,
        )
    except RuntimeError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    return _EXIT_OK


def _cmd_inspect(args: argparse.Namespace) -> int:
    snapshot = _load(args.index)
    print(f"producer: {snapshot.producer or 'unknown'}")
    declared = "" if snapshot.encoding_declared else " (assumed; not declared by the index)"
    print(f"encoding: {snapshot.encoding.value}{declared}")
    if snapshot.project_root:
        print(f"root:     {snapshot.project_root}")
    for key, value in snapshot.summary().items():
        print(f"{key + ':':<18}{value}")
    synthetic = sum(1 for s in snapshot.symbols.values() if s.synthetic)
    if synthetic:
        print(f"{'synthetic:':<18}{synthetic}")
    if args.paths:
        print()
        for path in sorted(snapshot.paths):
            print(path)
    return _EXIT_OK


def _cmd_verify_oracle(args: argparse.Namespace) -> int:
    try:
        problems = cross_check(args.binary, args.json_dump)
    except FileNotFoundError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    except ScipError as exc:
        raise SystemExit(f"repoatlas: {exc}") from None
    if not problems:
        print("Binary and JSON readings agree. The SCIP field numbers are corroborated.")
        return _EXIT_OK
    print(f"{len(problems)} disagreement(s) between the two readings:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return _EXIT_FAILED_CHECK


def _load_candidate(
    path: Path,
) -> tuple[IndexSnapshot, dict[tuple[str, int, int], str] | None]:
    """A candidate index, and what each of its references reached through.

    Only a parsed repository knows its receiver shapes; a SCIP file
    carries edges alone, and is scored without the shape gate.
    """
    if not path.is_dir():
        return _load(path), None
    from .resolve.cascade import receiver_shape

    build = _build(path)
    shapes = {
        (site_path, reference.span.start.line, reference.span.start.character): receiver_shape(
            reference
        )
        for site_path, reference in build.references
    }
    return build.snapshot, shapes


def _cmd_phpstan(args: argparse.Namespace) -> int:
    """Resolve a PHP project with a type engine, and say what it found.

    Two uses, and the summary is written for both. As an oracle it grades
    this index on the half `scip-php` cannot see. As a type source it says
    what a receiver holds where nothing declares it, which is the largest
    category this index leaves unresolved on a Laravel application.
    """
    root = args.root.resolve()
    with tempfile.TemporaryDirectory(prefix="repoatlas-phpstan-") as temporary:
        work = args.work.resolve() if args.work else Path(temporary)
        try:
            if args.read is not None:
                dump = args.read
            else:
                dump = run_phpstan(
                    root,
                    work,
                    phpstan=args.phpstan,
                    paths=tuple(args.paths),
                    level=args.level,
                    larastan=args.larastan,
                    memory_limit=args.memory_limit,
                    timeout=args.timeout,
                )
            result = read_phpstan(dump, root)
        except PhpStanError as exc:
            raise SystemExit(f"repoatlas: {exc}") from None
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dump, args.out)
    if args.format == "json":
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(f"definitions: {result.definitions}")
        print(f"sites:       {result.sites}")
        print(
            f"resolved:    {result.resolved} ({result.resolution_rate:.1%}), "
            f"{result.linked} of them onto a definition in these files"
        )
        print(
            f"undeclared:  {result.magic} resolved to a member nothing writes down"
        )
        if args.out is not None:
            print(f"wrote {args.out}")
    return _EXIT_OK


def _cmd_enrich(args: argparse.Namespace) -> int:
    """Add what a type engine resolved and the cascade could not."""
    from .enrich import enrich_from_phpstan
    from .store import IndexStore

    if not args.store.exists():
        raise SystemExit(f"repoatlas: no index at {args.store}")
    with IndexStore(args.store) as store:
        before = store.counts().get("edges", 0)
        try:
            result = enrich_from_phpstan(
                store,
                args.phpstan,
                phpstan_root=args.phpstan_root,
                prefix=args.prefix,
                dry_run=args.dry_run,
            )
        except PhpStanError as exc:
            raise SystemExit(f"repoatlas: {exc}") from None
        after = store.counts().get("edges", 0)
    if args.format == "json":
        print(json.dumps({**result.as_dict(), "edges_before": before, "edges_after": after}, indent=2))
        return _EXIT_OK
    print(f"sites in the dump:  {result.sites}")
    print(f"  resolved:         {result.resolved}")
    print(f"  already in the index: {result.already_known}")
    print(f"  target outside it:    {result.outside_index}")
    print(f"  target unplaceable:   {result.unplaceable}")
    verb = "would add" if args.dry_run else "added"
    print(
        f"{verb}: {result.added} edge(s), {result.magic_added} of them to a member "
        "nothing declares"
    )
    if not args.dry_run:
        print(f"edges: {before} -> {after}")
    return _EXIT_OK


def _cmd_compare(args: argparse.Namespace) -> int:
    candidate, site_shapes = _load_candidate(args.candidate)
    oracle = _load(args.oracle, args.candidate if args.candidate.is_dir() else None)
    options = ComparisonOptions(
        policy=args.policy,
        case_fold_paths=args.fold_case,
        collapse_edge_kinds=not args.split_kinds,
        restrict_to_oracle_paths=not args.all_paths,
        bootstrap_resamples=args.resamples,
        bootstrap_seed=args.seed,
        site_shapes=site_shapes,
    )
    result = compare_snapshots(candidate, oracle, options)
    text = to_json(result) if args.format == "json" else to_markdown(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    if args.min_f1 is not None and result.references.f1 < args.min_f1:
        print(
            f"reference F1 {result.references.f1:.3f} is below the required "
            f"{args.min_f1:.3f}",
            file=sys.stderr,
        )
        return _EXIT_FAILED_CHECK
    return _EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    # Reports carry repository paths, which are not always ASCII, and a
    # redirected stdout on a Windows host may default to a legacy code
    # page that cannot encode them.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "inspect": _cmd_inspect,
        "index": _cmd_index,
        "search": _cmd_search,
        "map": _cmd_map,
        "tokens": _cmd_tokens,
        "agentbench": _cmd_agentbench,
        "sitebench": _cmd_sitebench,
        "calibrate": _cmd_calibrate,
        "bench": _cmd_bench,
        "localize": _cmd_localize,
        "serve": _cmd_serve,
        "verify-oracle": _cmd_verify_oracle,
        "phpstan": _cmd_phpstan,
        "enrich": _cmd_enrich,
        "compare": _cmd_compare,
    }
    # The subparser is declared required with a fixed set of names, so
    # argparse has already rejected anything that is not a key here.
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
