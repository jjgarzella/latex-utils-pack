#!/usr/bin/env python3
"""Focused tests for recursive litsearch saturation aggregation."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import litsearch_saturation


class LitsearchSaturationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state = self.root / "papers" / "fixture" / "saturation.json"
        self.check_script = self.root / "papers" / "fixture" / "saturation-check.sh"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, dict[str, object] | None]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = litsearch_saturation.main(list(argv))
        output = stdout.getvalue().strip()
        payload = json.loads(output) if output else None
        return code, payload

    def initialize(self) -> None:
        code, _ = self.run_cli(
            "init",
            "--state",
            str(self.state),
            "--check-script",
            str(self.check_script),
            "--checker",
            str(Path(litsearch_saturation.__file__).resolve()),
            "--seed",
            "2301.00001",
        )
        self.assertEqual(code, 0)

    def test_recursive_descendants_must_vote_before_saturation(self) -> None:
        self.initialize()
        seed_work = "seed|2301.00001"
        child_work = f"{seed_work}|2301.00002"
        grandchild_work = f"{child_work}|2301.00003"

        code, payload = self.run_cli("check", "--state", str(self.state))
        self.assertEqual(code, 2)
        self.assertEqual(payload["pending_count"], 1)

        # Reserve a descendant before the parent votes, matching formula order:
        # enqueue new_frontier, emit on_complete, then finish this paper.
        self.run_cli(
            "enqueue", "--state", str(self.state), "--work-id", child_work,
            "--arxiv-id", "2301.00002",
        )
        self.run_cli(
            "finish", "--state", str(self.state), "--work-id", seed_work,
            "--arxiv-id", "2301.00001", "--outcome", "success",
        )
        code, payload = self.run_cli("check", "--state", str(self.state))
        self.assertEqual(code, 2)
        self.assertEqual(payload["pending_work"], [child_work])

        self.run_cli(
            "enqueue", "--state", str(self.state), "--work-id", grandchild_work,
            "--arxiv-id", "2301.00003",
        )
        self.run_cli(
            "finish", "--state", str(self.state), "--work-id", child_work,
            "--arxiv-id", "2301.00002", "--outcome", "success",
        )
        code, payload = self.run_cli("check", "--state", str(self.state))
        self.assertEqual(code, 2)
        self.assertEqual(payload["pending_work"], [grandchild_work])

        self.run_cli(
            "finish", "--state", str(self.state), "--work-id", grandchild_work,
            "--arxiv-id", "2301.00003", "--outcome", "success",
        )
        code, payload = self.run_cli("check", "--state", str(self.state))
        self.assertEqual(code, 0)
        self.assertTrue(payload["saturated"])
        self.assertEqual(payload["pending_count"], 0)
        self.assertEqual(payload["counts"]["success"], 3)

        # The generated compiler check wrapper enforces the same pass condition.
        result = subprocess.run(["sh", str(self.check_script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_vote_cannot_report_success(self) -> None:
        self.initialize()
        code, _ = self.run_cli(
            "finish", "--state", str(self.state), "--work-id", "seed|2301.00001",
            "--arxiv-id", "2301.00001", "--outcome", "failed", "--reason", "fixture failure",
        )
        self.assertEqual(code, 0)

        code, payload = self.run_cli("check", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["saturated"])
        self.assertEqual(payload["failed_work"], ["seed|2301.00001"])

        result = subprocess.run(["sh", str(self.check_script)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_unfinished_vote_times_out_as_failure(self) -> None:
        self.initialize()
        code, payload = self.run_cli(
            "wait", "--state", str(self.state), "--timeout-seconds", "0.02",
            "--poll-seconds", "0.005",
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual(payload["pending_count"], 1)


if __name__ == "__main__":
    unittest.main()
