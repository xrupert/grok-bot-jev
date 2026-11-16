"""Harness tests for src.cli (2026-11-12 Engineer). No network, no key values printed.
Run: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import cli, config, router, secrets


def _fake_result(intent="lookup", conf=0.9):
    return SimpleNamespace(
        choices={"intent": SimpleNamespace(choice=intent, confidence=conf, probabilities={intent: conf})},
        nouls={k: SimpleNamespace(noul=0.0) for k in ("reuse_cache", "needs_subagent", "stop_retry")},
        scores={"complexity": SimpleNamespace(score=1)},
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"enabled": True, "mode": "active", "model": "m", "thresholds": {}, "limits": {},
                    "logging": {"path": str(Path(self.tmp.name) / "runs.jsonl"),
                                "decisions_path": str(Path(self.tmp.name) / "decisions.jsonl")},
                    "gate": "log_only"}
        p = mock.patch.object(router, "load_config", lambda: dict(self.cfg))
        p.start(); self.addCleanup(p.stop)
        # 2026-11-16 Engineer: tests never read the real box keyring (Fix 2 precheck).
        k = mock.patch.object(router, "key_present", return_value=True)
        k.start(); self.addCleanup(k.stop)

    def run_cli(self, state):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main([json.dumps(state)])
        return rc, out.getvalue(), err.getvalue()

    def decisions(self):
        p = Path(self.tmp.name) / "decisions.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


class TestKeyPath(unittest.TestCase):
    def test_env_first(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "x" * 8}):
            with mock.patch.object(secrets, "_keyring_lookup", side_effect=AssertionError("no keyring")):
                self.assertTrue(secrets.get_typesafe_api_key())

    def test_keyring_fallback_pins_bus(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TYPESAFE_API_KEY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")}
        with tempfile.TemporaryDirectory() as d:
            sock = Path(d) / "bus"
            sock.touch()
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(secrets, "BUS_SOCKET", sock), \
                 mock.patch.object(secrets, "_is_socket", lambda p: True), \
                 mock.patch.object(secrets, "_keyring_lookup", return_value="k" * 8):
                self.assertEqual(secrets.get_typesafe_api_key(), "k" * 8)
                self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={sock}")
                self.assertEqual(os.environ["XDG_RUNTIME_DIR"], d)

    def test_missing_key_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(secrets, "_keyring_lookup", side_effect=RuntimeError("no backend")), \
             mock.patch.object(secrets, "_box_bus_lookup", side_effect=RuntimeError("no backend")):
            with self.assertRaises(secrets.MissingKeyError):
                secrets.get_typesafe_api_key()

    def test_live_bus_without_keyring_falls_back_to_box_bus(self):
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        with tempfile.TemporaryDirectory() as d:
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={d}/agent-bus"
            sock = Path(d) / "bus"
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(secrets, "BUS_SOCKET", sock), \
                 mock.patch.object(secrets, "_is_socket", lambda p: True), \
                 mock.patch.object(secrets, "_keyring_lookup", side_effect=RuntimeError("NoKeyringError")), \
                 mock.patch.object(secrets, "_box_bus_lookup", return_value="k" * 8):
                self.assertEqual(secrets.get_typesafe_api_key(), "k" * 8)
                self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={sock}")


class TestGoal(Base):
    def test_empty_goal_rejected(self):
        for goal in ("", "   ", None):
            rc, out, err = self.run_cli({"decision_type": "costly_action_gate", "context": {"goal": goal}})
            self.assertEqual(rc, 2, (goal, err))
            self.assertIn("goal", err.lower())

    def test_missing_goal_rejected(self):
        rc, _, err = self.run_cli({"decision_type": "costly_action_gate"})
        self.assertEqual(rc, 2)

    def test_context_goal_accepted(self):
        self.cfg["gate"] = "off"
        rc, out, err = self.run_cli({"decision_type": "costly_action_gate", "caller": "t",
                                     "context": {"goal": "open browser for X"}})
        self.assertEqual(rc, 0, err)

    def test_bad_json(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["{not json"])
        self.assertEqual(rc, 2)


class TestMissingKeyFailsClosed(Base):
    def test_exit_nonzero_and_logged(self):
        with mock.patch.object(router, "system_one", side_effect=secrets.MissingKeyError("missing")):
            rc, out, err = self.run_cli({"decision_type": "costly_action_gate", "caller": "t",
                                         "context": {"goal": "paid call"}})
        self.assertEqual(rc, cli.EXIT_KEY_MISSING)
        self.assertNotEqual(rc, 0)
        self.assertIn("TYPESAFE_API_KEY", err)
        body = json.loads(out)
        self.assertNotIn(body.get("choice"), ("proceed",))
        rows = self.decisions()
        self.assertEqual(rows[-1]["error"], "key_missing")


class TestGateModes(Base):
    state = {"decision_type": "costly_action_gate", "caller": "t", "context": {"goal": "browse 3 sites"}}

    def test_config_ships_log_only(self):
        self.assertEqual(config.normalize_gate(config.load_config().get("gate")), "log_only")

    def test_yaml_off_boolean(self):
        self.assertEqual(config.normalize_gate(False), "off")
        self.assertEqual(config.normalize_gate("OFF"), "off")
        self.assertEqual(config.normalize_gate("enforce"), "enforce")
        self.assertEqual(config.normalize_gate("garbage"), "log_only")

    def test_off_skips_jev_and_writes_row(self):
        self.cfg["gate"] = "off"
        with mock.patch.object(router, "system_one", side_effect=AssertionError("called")):
            rc, out, _ = self.run_cli(self.state)
        self.assertEqual(rc, 0)
        body = json.loads(out)
        self.assertEqual(body["gate"], "off")
        self.assertFalse(body["jev_used"])
        self.assertEqual(self.decisions()[-1]["mode"], "off")

    def test_log_only_logs_both_acted_on_old(self):
        with mock.patch.object(router, "system_one", return_value=_fake_result("lookup", 0.9)):
            rc, out, _ = self.run_cli(self.state)
        self.assertEqual(rc, 0)
        body = json.loads(out)
        self.assertEqual(body["gate"], "log_only")
        self.assertEqual(body["acted_on"], "old")
        self.assertEqual(body["jev_choice"], "cheaper_lane")
        row = self.decisions()[-1]
        self.assertEqual((row["mode"], row["acted_on"], row["choice"]), ("log_only", "old", "cheaper_lane"))
        self.assertIn("input_hash", row)
        self.assertNotIn("goal", row)

    def test_enforce_follows_jev_with_stepdown(self):
        self.cfg["gate"] = "enforce"
        with mock.patch.object(router, "system_one", return_value=_fake_result("coding", 0.5)):
            rc, out, _ = self.run_cli(self.state)
        body = json.loads(out)
        self.assertEqual(body["acted_on"], "jev")
        # proceed below 0.70 steps down to cheaper_lane
        self.assertEqual(body["choice"], "cheaper_lane")

    def test_per_type_override(self):
        self.cfg["gate"] = "log_only"
        self.cfg["gate_types"] = {"costly_action_gate": "off"}
        with mock.patch.object(router, "system_one", side_effect=AssertionError("called")):
            rc, out, _ = self.run_cli(self.state)
        self.assertEqual(json.loads(out)["gate"], "off")

    def test_needs_approval_without_green_is_ask_chris_even_off(self):
        self.cfg["gate"] = "off"
        rc, out, _ = self.run_cli({"decision_type": "needs_approval", "caller": "t",
                                   "context": {"goal": "publish post", "action": "publish", "green": ""}})
        self.assertEqual(json.loads(out)["choice"], "ask_chris")

    def test_needs_approval_with_green_records_green(self):
        self.cfg["gate"] = "off"
        rc, out, _ = self.run_cli({"decision_type": "needs_approval", "caller": "t",
                                   "context": {"goal": "publish post", "action": "publish", "green": "t1u"}})
        self.assertEqual(json.loads(out)["choice"], "proceed")
        self.assertEqual(self.decisions()[-1]["green"], "t1u")


if __name__ == "__main__":
    unittest.main()
