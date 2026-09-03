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
    any accuracy number from that language.

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
    result = _build(args.root, use_git=not args.no_git)
    if args.format == "json":
        print(json.dumps(result.as_dict(), indent=2))
    else:
        real = sum(1 for s in result.snapshot.symbols.values() if not s.synthetic)
        print(f"files:      {result.files}")
        print(f"symbols:    {real}")
        print(f"references: {len(result.references)} (unresolved)")
        print(f"elapsed:    {result.duration_seconds:.2f}s "
              f"({result.throughput():.0f} files/s)")
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
        "verify-oracle": _cmd_verify_oracle,
        "compare": _cmd_compare,
    }
    # The subparser is declared required with a fixed set of names, so
    # argparse has already rejected anything that is not a key here.
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
