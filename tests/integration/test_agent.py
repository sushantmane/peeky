"""Controller regression tests; model decisions are fixtures, not live inference."""

import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path

import pytest

from peeky.agent import Investigation
from peeky.client import Client
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.integration


class AgentControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = secrets.token_urlsafe(32)
        cls.server = make_server(ROOT, cls.token, 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client(f"http://127.0.0.1:{cls.server.server_port}", cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.server.worker.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def runner(self, destination, decide, steps=5):
        return Investigation(
            self.client,
            "examples/pagination_bug.py",
            ROOT / "examples/pagination_bug.py",
            ROOT / "examples/verify_pagination.py",
            destination,
            decide,
            steps,
        )

    def test_evidence_then_independent_verification(self):
        decisions = iter(
            [
                {"action": "observe", "line": 8, "reason": "Inspect calculated start before end"},
                {
                    "action": "intervene",
                    "variable": "start",
                    "value": "0",
                    "reason": "Test first page",
                },
                {
                    "action": "finish",
                    "old": "start = page * page_size",
                    "new": "start = 0",
                    "reason": "Deliberately overfit; checker must reject",
                },
                {
                    "action": "finish",
                    "old": "start = page * page_size",
                    "new": "start = (page - 1) * page_size",
                    "reason": "General fix",
                },
            ]
        )

        def choose(context):
            if len(context["steps"]) == 1:
                snapshot = context["steps"][0]["result"]["snapshot"]
                self.assertTrue(
                    any(
                        v["name"] == "start" and v["value"] == "3"
                        for s in snapshot["scopes"]
                        for v in s["variables"]
                    )
                )
            if len(context["steps"]) == 3:
                self.assertNotEqual(context["steps"][-1]["result"]["exit_code"], 0)
            return next(decisions)

        with tempfile.TemporaryDirectory() as tmp:
            report = self.runner(tmp, choose).run("Fix one-based pagination")
            self.assertEqual(report["status"], "verified")
            self.assertIn("PAGE: [10, 20, 30]", report["steps"][1]["result"]["output"])
            self.assertEqual(report["verification"]["exit_code"], 0)
            self.assertTrue(report["source_unchanged"])
            self.assertTrue(report["check_unchanged"])

    def test_no_fix_without_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(
                tmp,
                lambda _: {
                    "action": "finish",
                    "reason": "Skip evidence",
                    "old": "start = page * page_size",
                    "new": "start = 0",
                },
                1,
            )
            report = runner.run("Fix pagination")
            self.assertEqual(report["status"], "budget_exhausted")
            self.assertIn("runtime intervention", report["steps"][0]["error"])
            self.assertFalse((Path(tmp) / "candidate.py").exists())

    def test_model_failure_preserves_report(self):
        def fail(_):
            raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(tmp, fail)
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                runner.run("Fix pagination")
            report = json.loads((Path(tmp) / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["source_unchanged"])
            self.assertIsNone(runner.session)


class SourceIdentityTests(unittest.TestCase):
    def test_mismatched_worker_source_closes_session(self):
        class Remote:
            closed = False

            def call(self, operation, **kwargs):
                if operation == "launch":
                    return {"session_id": "remote", "source_sha256": "different"}
                if operation == "close":
                    self.closed = True
                    return {}
                raise AssertionError(operation)

        client = Remote()
        with tempfile.TemporaryDirectory() as tmp:
            runner = Investigation(
                client,
                "examples/pagination_bug.py",
                ROOT / "examples/pagination_bug.py",
                ROOT / "examples/verify_pagination.py",
                tmp,
                lambda _: None,
            )
            with self.assertRaisesRegex(ValueError, "differs from local source"):
                runner.observe(8)
            self.assertTrue(client.closed)
            self.assertIsNone(runner.session)


if __name__ == "__main__":
    unittest.main()
