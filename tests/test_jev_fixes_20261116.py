"""Jev gate fixes 2026-11-16 (Engineer, Chris green t3075s12340). Mocked API only, no paid calls.
Fix 1: a valid named green on needs_approval -> proceed; below gate_confidence_min Jev's choice steps
       to cheaper_lane at most (never hold/ask_chris from confidence alone). No green -> ask_chris.
Fix 2: precheck key present (bool only) + caller name before any Jev call; on a miss skip the call,
       write a decisions row with the error, do not raise.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from src import router, secrets
from tests.test_cli_harness import Base, _fake_result

KEY = "TYPESAFE_API_KEY"


class KeyOK(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(router, "key_present", return_value=True)
        p.start(); self.addCleanup(p.stop)


class TestDecideRule(KeyOK):
    def test_valid_green(self):
        for g in ("t3075s12340", "chris-20261116-0659-t3066s12281-wk10-lineups"):
            self.assertTrue(router.valid_green(g), g)
        # jevfix2: free text is no longer a green (t3082s12374 "real green check")
        for g in ("", "  ", None, "none", "N/A", "pending", "tbd", "unknown", "no", "false", "-",
                  "routine-self-delete-after-run"):
            self.assertFalse(router.valid_green(g), g)

    def test_named_green_overrides_jev_ask_chris(self):
        self.assertEqual(router.decide("needs_approval", "t1s2", "ask_human", 0.98, 0.70), "proceed")
        self.assertEqual(router.decide("needs_approval", "t1s2", "ask_human", 0.53, 0.70), "proceed")
        self.assertEqual(router.decide("costly_action_gate", "t1s2", "ask_human", 0.9, 0.70), "proceed")

    def test_named_green_keeps_stop_retry_hold(self):
        # a green approves the action, it does not clear a retry loop
        self.assertEqual(router.decide("needs_approval", "t1s2", "stop_retry", 0.9, 0.70), "hold")

    def test_no_green_needs_approval_fails_closed(self):
        for act in ("proceed_full", "run_deterministic", "ask_human", "stop_retry"):
            for c in (0.1, 0.69, 0.99, None):
                self.assertEqual(router.decide("needs_approval", "", act, c, 0.70), "ask_chris", (act, c))

    def test_below_cutoff_steps_to_cheaper_lane_not_hold(self):
        self.assertEqual(router.decide("costly_action_gate", "", "proceed_full", 0.5, 0.70), "cheaper_lane")
        self.assertEqual(router.decide("costly_action_gate", "", "run_deterministic", 0.5, 0.70), "cheaper_lane")
        self.assertEqual(router.decide("costly_action_gate", "", "proceed_full", 0.9, 0.70), "proceed")
        # an explicit stop/ask stays (confidence never makes it less cautious)
        self.assertEqual(router.decide("costly_action_gate", "", "stop_retry", 0.5, 0.70), "hold")
        self.assertEqual(router.decide("costly_action_gate", "", "ask_human", 0.5, 0.70), "ask_chris")

    def test_route_named_green_jev_logged_proceed(self):
        self.cfg["gate"] = "enforce"
        with mock.patch.object(router, "system_one", return_value=_fake_result("account", 0.53)):
            rc, out, _ = self.run_cli({"decision_type": "needs_approval", "caller": "Loop",
                                       "context": {"goal": "publish greened post", "action": "publish",
                                                   "green": "t3066s12281"}})
        self.assertEqual(rc, 0)
        body = json.loads(out)
        self.assertEqual((body["choice"], body["jev_choice"]), ("proceed", "proceed"))
        self.assertEqual(self.decisions()[-1]["choice"], "proceed")

    def test_route_no_green_enforce_ask_chris(self):
        self.cfg["gate"] = "enforce"
        with mock.patch.object(router, "system_one", return_value=_fake_result("coding", 0.99)):
            rc, out, _ = self.run_cli({"decision_type": "needs_approval", "caller": "Loop",
                                       "context": {"goal": "publish post", "action": "publish", "green": ""}})
        self.assertEqual(json.loads(out)["choice"], "ask_chris")

    def test_costly_row_records_green(self):
        with mock.patch.object(router, "system_one", return_value=_fake_result("account", 0.9)):
            self.run_cli({"decision_type": "costly_action_gate", "caller": "Loop",
                          "context": {"goal": "open browser to post", "green": "t2070u"}})
        row = self.decisions()[-1]
        self.assertEqual((row["green"], row["choice"]), ("t2070u", "proceed"))


class TestKeyPresent(unittest.TestCase):
    def test_key_present_is_bool_and_never_leaks(self):
        with mock.patch.dict(os.environ, {KEY: "SECRETVALUE123"}):
            self.assertIs(router.key_present(), True)
            self.assertEqual(os.environ[KEY], "SECRETVALUE123")
        env = {k: v for k, v in os.environ.items() if k != KEY}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(secrets, "get_typesafe_api_key", side_effect=secrets.MissingKeyError("x")):
            self.assertIs(router.key_present(), False)


class TestPrecheck(Base):

    def test_missing_key_skips_call_logs_row_no_raise(self):
        with mock.patch.object(router, "key_present", return_value=False), \
             mock.patch.object(router, "system_one", side_effect=AssertionError("Jev called")):
            rc, out, err = self.run_cli({"decision_type": "costly_action_gate", "caller": "Loop",
                                         "context": {"goal": "paid call"}})
        self.assertEqual(rc, 0, err)
        body = json.loads(out)
        self.assertFalse(body["jev_used"])
        row = self.decisions()[-1]
        self.assertEqual(row["error"], "precheck:key_missing")
        self.assertEqual(row["acted_on"], "old")  # log_only

    def test_missing_caller_skips_call(self):
        for caller in (None, "", "  ", "unknown"):
            with mock.patch.object(router, "key_present", return_value=True), \
                 mock.patch.object(router, "system_one", side_effect=AssertionError("Jev called")):
                st = {"decision_type": "costly_action_gate", "context": {"goal": "paid call"}}
                if caller is not None:
                    st["caller"] = caller
                rc, out, err = self.run_cli(st)
            self.assertEqual(rc, 0, (caller, err))
            self.assertEqual(self.decisions()[-1]["error"], "precheck:caller_missing")

    def test_precheck_miss_enforce_fails_closed(self):
        self.cfg["gate"] = "enforce"
        with mock.patch.object(router, "key_present", return_value=False), \
             mock.patch.object(router, "system_one", side_effect=AssertionError("Jev called")):
            _, out, _ = self.run_cli({"decision_type": "costly_action_gate", "caller": "Loop",
                                      "context": {"goal": "paid call"}})
            self.assertEqual(json.loads(out)["choice"], "hold")
            _, out, _ = self.run_cli({"decision_type": "needs_approval", "caller": "Loop",
                                      "context": {"goal": "publish", "green": ""}})
            self.assertEqual(json.loads(out)["choice"], "ask_chris")
            _, out, _ = self.run_cli({"decision_type": "needs_approval", "caller": "Loop",
                                      "context": {"goal": "publish", "green": "t1s2"}})
            self.assertEqual(json.loads(out)["choice"], "proceed")

    def test_no_secret_in_output_or_ledger(self):
        with mock.patch.dict(os.environ, {KEY: "SECRETVALUE123"}), \
             mock.patch.object(router, "system_one", return_value=_fake_result("lookup", 0.9)):
            _, out, err = self.run_cli({"decision_type": "costly_action_gate", "caller": "Loop",
                                        "context": {"goal": "x"}})
        blob = out + err + json.dumps(self.decisions())
        self.assertNotIn("SECRETVALUE123", blob)
