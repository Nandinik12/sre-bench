"""Chaos injector CLI.

Usage (from repo root):
    python -m chaos.inject list
    python -m chaos.inject break dead-dependency
    python -m chaos.inject restore dead-dependency
    python -m chaos.inject restore-all
    python -m chaos.inject status
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from .failures import FAILURE_MODES

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def execute(steps, root: pathlib.Path = REPO_ROOT, dry_run: bool = False) -> list:
    """Execute a step plan. Returns a log of what happened."""
    log = []
    for step in steps:
        kind = step[0]
        if kind == "run":
            argv = step[1]
            log.append({"run": argv})
            if not dry_run:
                subprocess.run(argv, cwd=root, check=True)
        elif kind == "write_file":
            _, relpath, content = step
            target = root / relpath
            log.append({"write_file": str(relpath)})
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        else:
            raise ValueError(f"unknown step kind: {kind!r}")
    return log


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="chaos", description="break and restore sre-bench")
    p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    b = sub.add_parser("break")
    b.add_argument("mode", choices=sorted(FAILURE_MODES))
    r = sub.add_parser("restore")
    r.add_argument("mode", choices=sorted(FAILURE_MODES))
    sub.add_parser("restore-all")
    sub.add_parser("status")
    args = p.parse_args(argv)

    if args.cmd == "list":
        for m in FAILURE_MODES.values():
            print(f"{m.name:18} {m.description}\n{'':18} blast radius: {m.blast_radius}")
        return 0
    if args.cmd == "break":
        mode = FAILURE_MODES[args.mode]
        print(f"breaking: {mode.name} — {mode.description}")
        execute(mode.break_steps, dry_run=args.dry_run)
        return 0
    if args.cmd == "restore":
        mode = FAILURE_MODES[args.mode]
        print(f"restoring: {mode.name}")
        execute(mode.restore_steps, dry_run=args.dry_run)
        return 0
    if args.cmd == "restore-all":
        for mode in FAILURE_MODES.values():
            print(f"restoring: {mode.name}")
            execute(mode.restore_steps, dry_run=args.dry_run)
        return 0
    if args.cmd == "status":
        sys.path.insert(0, str(REPO_ROOT))
        from bench.probes import probe_environment

        print(json.dumps(probe_environment(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
