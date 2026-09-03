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

``compare``
    Score one index against another and write the report.

No dependencies beyond the standard library, so the harness runs in CI
without installing a parser toolchain.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .eval.compare import ComparisonOptions, compare_snapshots
from .eval.report import to_json, to_markdown
from .model import IndexSnapshot
from .oracle.scip import ScipError, cross_check, read_scip

__all__ = ["main", "build_parser"]

_EXIT_OK = 0
_EXIT_FAILED_CHECK = 1
_EXIT_BAD_INPUT = 2


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

    compare = subcommands.add_parser("compare", help="score a candidate index against an oracle")
    compare.add_argument("candidate", type=Path)
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


def _load(path: Path) -> IndexSnapshot:
    try:
        return read_scip(path)
    except FileNotFoundError:
        raise SystemExit(f"repoatlas: no such file: {path}") from None
    except ScipError as exc:
        raise SystemExit(f"repoatlas: cannot read {path}: {exc}") from None


def _cmd_inspect(args: argparse.Namespace) -> int:
    snapshot = _load(args.index)
    print(f"producer: {snapshot.producer or 'unknown'}")
    print(f"encoding: {snapshot.encoding.value}")
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
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "inspect": _cmd_inspect,
        "verify-oracle": _cmd_verify_oracle,
        "compare": _cmd_compare,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects this first
        parser.error(f"unknown command: {args.command}")
        return _EXIT_BAD_INPUT
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
