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
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .eval.compare import ComparisonOptions, compare_snapshots
from .eval.report import to_json, to_markdown
from .model import IndexSnapshot
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


def _load(path: Path) -> IndexSnapshot:
    """Read a SCIP index, or parse a directory into one."""
    if path.is_dir():
        return _build(path).snapshot
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

    if args.source.is_dir():
        snapshot = _build(args.source, use_git=not args.no_git).snapshot
    else:
        from .store import IndexStore, StoreError

        if not args.source.exists():
            raise SystemExit(f"repoatlas: no such repository or index: {args.source}")
        try:
            with IndexStore(args.source) as store:
                snapshot = store.snapshot()
        except StoreError as exc:
            raise SystemExit(f"repoatlas: {exc}") from None

    focus_paths = {_normalise_focus(item) for item in args.focus}
    ranked = rank_symbols(
        snapshot, focus_paths=focus_paths, options=RankOptions()
    )
    estimator = (
        make_estimator(args.chars_per_token) if args.chars_per_token else None
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


def _cmd_compare(args: argparse.Namespace) -> int:
    candidate = _load(args.candidate)
    oracle = _load(args.oracle)
    options = ComparisonOptions(
        policy=args.policy,
        case_fold_paths=args.fold_case,
        collapse_edge_kinds=not args.split_kinds,
        restrict_to_oracle_paths=not args.all_paths,
        bootstrap_resamples=args.resamples,
        bootstrap_seed=args.seed,
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
        "verify-oracle": _cmd_verify_oracle,
        "compare": _cmd_compare,
    }
    # The subparser is declared required with a fixed set of names, so
    # argparse has already rejected anything that is not a key here.
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
