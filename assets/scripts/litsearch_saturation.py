#!/usr/bin/env python3
"""Track recursive arXiv litsearch fan-out and aggregate terminal votes.

The tracker is deliberately pack-level: formula steps enqueue child work before
emitting on_complete output, and each paper records one terminal vote. The
search can pass its saturation step only after the queue is empty and no vote
failed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
OUTCOMES = {"success", "skipped", "failed"}


class TrackerError(Exception):
    """Raised when tracker state violates the fan-out protocol."""


def seed_work_id(arxiv_id: str) -> str:
    return f"seed|{arxiv_id}"


def new_state(seeds: list[str]) -> dict[str, Any]:
    pending: dict[str, dict[str, str]] = {}
    for arxiv_id in seeds:
        pending.setdefault(seed_work_id(arxiv_id), {"arxiv_id": arxiv_id})
    return {"schema_version": SCHEMA_VERSION, "pending": pending, "votes": {}}


def _read_state(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {"schema_version": SCHEMA_VERSION, "pending": {}, "votes": {}}
        raise TrackerError(f"tracker state does not exist: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackerError(f"cannot read tracker state {path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise TrackerError(f"unsupported or malformed tracker state: {path}")
    if not isinstance(state.get("pending"), dict) or not isinstance(state.get("votes"), dict):
        raise TrackerError(f"malformed pending/votes map in tracker state: {path}")
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def locked_state(path: Path, *, missing_ok: bool = False) -> Iterator[dict[str, Any]]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_state(path, missing_ok=missing_ok)
        yield state
        _write_state(path, state)


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    votes = state["votes"]
    counts = {outcome: 0 for outcome in OUTCOMES}
    for vote in votes.values():
        outcome = vote.get("outcome") if isinstance(vote, dict) else None
        if outcome not in counts:
            raise TrackerError(f"invalid vote outcome in tracker state: {outcome!r}")
        counts[outcome] += 1
    pending = state["pending"]
    saturated = not pending
    failed = counts["failed"] > 0
    status = "failed" if saturated and failed else "saturated" if saturated else "pending"
    return {
        "status": status,
        "saturated": saturated and not failed,
        "pending_count": len(pending),
        "pending_work": sorted(pending),
        "counts": counts,
        "total_votes": len(votes),
        "failed_work": sorted(
            work_id for work_id, vote in votes.items()
            if isinstance(vote, dict) and vote.get("outcome") == "failed"
        ),
    }


def status_exit_code(summary: dict[str, Any]) -> int:
    if summary["pending_count"]:
        return 2
    return 1 if summary["counts"]["failed"] else 0


def _print_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, sort_keys=True))


def init_tracker(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    seeds = list(dict.fromkeys(args.seed))
    with locked_state(state_path, missing_ok=True) as state:
        if state["votes"] or state["pending"]:
            for arxiv_id in seeds:
                work_id = seed_work_id(arxiv_id)
                existing = state["pending"].get(work_id)
                vote = state["votes"].get(work_id)
                recorded_id = existing.get("arxiv_id") if existing else vote.get("arxiv_id") if vote else None
                if recorded_id is not None and recorded_id != arxiv_id:
                    raise TrackerError(f"seed work id collision for {work_id!r}")
                if existing is None and vote is None:
                    state["pending"][work_id] = {"arxiv_id": arxiv_id}
        else:
            state.update(new_state(seeds))

    checker = Path(args.check_script)
    checker.parent.mkdir(parents=True, exist_ok=True)
    checker_contents = "#!/bin/sh\nexec python3 " + shlex.quote(str(Path(args.checker).resolve()))
    checker_contents += " check --state " + shlex.quote(str(state_path.resolve())) + "\n"
    checker.write_text(checker_contents, encoding="utf-8")
    checker.chmod(0o755)
    with locked_state(state_path) as state:
        _print_summary(summarize(state))
    return 0


def enqueue(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    with locked_state(state_path) as state:
        pending = state["pending"]
        votes = state["votes"]
        existing = pending.get(args.work_id)
        prior_vote = votes.get(args.work_id)
        recorded_id = existing.get("arxiv_id") if existing else prior_vote.get("arxiv_id") if prior_vote else None
        if recorded_id is not None:
            if recorded_id != args.arxiv_id:
                raise TrackerError(f"work id collision for {args.work_id!r}")
        else:
            pending[args.work_id] = {"arxiv_id": args.arxiv_id}
        summary = summarize(state)
    _print_summary(summary)
    return 0


def finish(args: argparse.Namespace) -> int:
    if args.outcome not in OUTCOMES:
        raise TrackerError(f"unsupported outcome: {args.outcome}")
    state_path = Path(args.state)
    with locked_state(state_path) as state:
        pending = state["pending"]
        votes = state["votes"]
        prior_vote = votes.get(args.work_id)
        if prior_vote is not None:
            if prior_vote.get("arxiv_id") != args.arxiv_id or prior_vote.get("outcome") != args.outcome:
                raise TrackerError(f"conflicting terminal vote for {args.work_id!r}")
        elif args.work_id not in pending:
            raise TrackerError(f"work was not scheduled: {args.work_id!r}")
        else:
            if pending[args.work_id].get("arxiv_id") != args.arxiv_id:
                raise TrackerError(f"arXiv ID does not match scheduled work {args.work_id!r}")
            del pending[args.work_id]
            votes[args.work_id] = {
                "arxiv_id": args.arxiv_id,
                "outcome": args.outcome,
                "reason": args.reason,
            }
        summary = summarize(state)
    _print_summary(summary)
    return 0


def inspect(args: argparse.Namespace, *, enforce_success: bool) -> int:
    with locked_state(Path(args.state)) as state:
        summary = summarize(state)
    _print_summary(summary)
    if enforce_success:
        return status_exit_code(summary)
    return 0


def wait_for_saturation(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        with locked_state(state_path) as state:
            summary = summarize(state)
        if summary["pending_count"] == 0:
            _print_summary(summary)
            return status_exit_code(summary)
        if time.monotonic() >= deadline:
            summary["status"] = "timeout"
            summary["saturated"] = False
            summary["timeout_seconds"] = args.timeout_seconds
            _print_summary(summary)
            return 1
        time.sleep(min(args.poll_seconds, max(0.0, deadline - time.monotonic())))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="initialize or resume a search ledger")
    init.add_argument("--state", required=True)
    init.add_argument("--check-script", required=True)
    init.add_argument("--checker", required=True, help="absolute path to this helper")
    init.add_argument("--seed", action="append", default=[])
    init.set_defaults(handler=init_tracker)

    add = subparsers.add_parser("enqueue", help="register a child before on_complete fan-out")
    add.add_argument("--state", required=True)
    add.add_argument("--work-id", required=True)
    add.add_argument("--arxiv-id", required=True)
    add.set_defaults(handler=enqueue)

    vote = subparsers.add_parser("finish", help="record one terminal vote")
    vote.add_argument("--state", required=True)
    vote.add_argument("--work-id", required=True)
    vote.add_argument("--arxiv-id", required=True)
    vote.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    vote.add_argument("--reason", default="")
    vote.set_defaults(handler=finish)

    for name, help_text, enforce_success in (
        ("status", "show current queue and vote totals", False),
        ("check", "exit successfully only for clean saturation", True),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--state", required=True)
        command.set_defaults(handler=lambda args, enforce=enforce_success: inspect(args, enforce_success=enforce))

    wait = subparsers.add_parser("wait", help="wait for every scheduled paper to vote")
    wait.add_argument("--state", required=True)
    wait.add_argument("--timeout-seconds", type=float, default=86400)
    wait.add_argument("--poll-seconds", type=float, default=15)
    wait.set_defaults(handler=wait_for_saturation)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "wait" and (args.timeout_seconds <= 0 or args.poll_seconds <= 0):
        parser.error("wait timeout and poll intervals must be positive")
    try:
        return args.handler(args)
    except TrackerError as exc:
        print(f"litsearch saturation tracker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
