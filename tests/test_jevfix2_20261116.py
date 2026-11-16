"""jevfix2 2026-11-16 (Engineer, Chris green t3082s12374). Mocked API only; no paid calls.
1 floor 0.5 -> ask_chris; 0.5-0.70 steps to cheaper_lane (config values)
2 a recorded green proceeds on publish/delete at any confidence (beats the floor)
3 real green check: registry + standing list; unknown -> ask_chris reason green_not_recorded; append CLI
4 $5 cap (500 cents, calendar month ET) and estimate over remaining -> ask_chris reason cost_cap
5 cost_cents, model, usage on every row (null + cost_reason when the API gives none)
6 3 s timeout -> error row, fail closed
7 CLI writes its own row on every path, legacy fields kept
8 caller in runs.jsonl
"""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import cli, cost, greens, jev_client, router
from tests.test_cli_harness import Base, _fake_result

LEGACY = ("caller", "decision_type", "action", "input_hash", "choice", "confidence", "mode", "acted_on",
          "green", "outcome", "cost_cents", "latency_ms", "error", "ts")


def _result_with_usage(intent="coding", conf=0.9, tin=1200, tout=40, model="jev-1.13.0"):
    r = _fake_result(intent, conf)
    r.model = model
    r.usage = SimpleNamespace(input_tokens=tin, output_tokens=tout)
    return r


def na(goal="publish greened post", green="", action="publish", **kw):
    return {"decision_type": "needs_approval", "caller": "Loop",
            "context": {"goal": goal, "action": action, "green": green, **kw}}


def cg(goal="open browser", green="", **kw):
    return {"decision_type": "costly_action_gate", "caller": "Loop", "context": {"goal": goal, "green": green, **kw}}


class Enforce(Base):
    def setUp(self):
        super().setUp()
        self.cfg["gate"] = "enforce"

    def call(self, state, result=None, **patch):
        with mock.patch.object(router, "system_one", return_value=result or _fake_result("coding", 0.9), **patch):
            return self.run_cli(state)


class T1Floor(Enforce):
    def test_rule(self):
        d = router.decide_ex
        self.assertEqual(d("costly_action_gate", "", "proceed_full", 0.49, 0.70, 0.5), ("ask_chris", "low_confidence"))
        self.assertEqual(d("costly_action_gate", "", "proceed_full", 0.5, 0.70, 0.5), ("cheaper_lane", "step_down"))
        self.assertEqual(d("costly_action_gate", "", "proceed_full", 0.69, 0.70, 0.5)[0], "cheaper_lane")
        self.assertEqual(d("costly_action_gate", "", "proceed_full", 0.70, 0.70, 0.5)[0], "proceed")
        self.assertEqual(d("costly_action_gate", "", "run_deterministic", 0.2, 0.70, 0.5)[0], "ask_chris")

    def test_cli_conf_04_no_green_asks_chris(self):
        _, out, _ = self.call(cg(), _fake_result("coding", 0.4))
        body = json.loads(out)
        self.assertEqual(body["choice"], "ask_chris")
        self.assertEqual(self.decisions()[-1]["reason"], "low_confidence")

    def test_thresholds_from_config(self):
        self.cfg["thresholds"] = {"ask_chris_below": 0.3, "gate_confidence_min": 0.45}
        _, out, _ = self.call(cg(), _fake_result("coding", 0.4))
        self.assertEqual(json.loads(out)["choice"], "cheaper_lane")
        _, out, _ = self.call(cg(), _fake_result("coding", 0.5))
        self.assertEqual(json.loads(out)["choice"], "proceed")

    def test_config_example_documents_both(self):
        import yaml
        from src import config
        ex = yaml.safe_load(config.EXAMPLE_PATH.read_text(encoding="utf-8"))
        thr = ex["thresholds"]
        self.assertEqual((thr["ask_chris_below"], thr["gate_confidence_min"]), (0.5, 0.70))
        self.assertEqual((ex["cost"]["cap_cents"], ex["cost"]["window"]), (500, "calendar_month_et"))
        self.assertEqual(ex["limits"]["jev_timeout_s"], 3)


class T2GreenBeatsFloor(Enforce):
    def test_publish_delete_any_confidence(self):
        for action in ("publish", "delete"):
            for conf in (0.05, 0.3, 0.49, 0.99):
                _, out, _ = self.call(na(green="t3066s12281", action=action), _fake_result("account", conf))
                self.assertEqual(json.loads(out)["choice"], "proceed", (action, conf))
        self.assertTrue(self.decisions()[-1]["reason"].startswith("green_"))

    def test_standing_permission_counts(self):
        _, out, _ = self.call(na(green="standing t2070u breaking news"), _fake_result("account", 0.2))
        self.assertEqual(json.loads(out)["choice"], "proceed")
        self.assertEqual(self.decisions()[-1]["green_status"], "standing")

    def test_stop_retry_stays_hold(self):
        self.assertEqual(router.decide("needs_approval", "t1s2", "stop_retry", 0.1), "hold")

    def test_no_green_needs_approval_still_ask_chris(self):
        _, out, _ = self.call(na(), _fake_result("coding", 0.99))
        self.assertEqual(json.loads(out)["choice"], "ask_chris")
        self.assertEqual(self.decisions()[-1]["reason"], "no_green")


class T3RealGreenCheck(Enforce):
    def test_unknown_and_free_text_rejected(self):
        for g in ("t9999s9999", "routine-self-delete-after-run", "Chris said ok earlier",
                  "chris-20990101-0000-made-up"):
            for st in (na(green=g), cg(green=g)):
                _, out, _ = self.call(st, _fake_result("coding", 0.99))
                self.assertEqual(json.loads(out)["choice"], "ask_chris", g)
                row = self.decisions()[-1]
                self.assertEqual((row["reason"], row["green_status"]), ("green_not_recorded", "not_recorded"), g)

    def test_placeholders_are_no_green(self):
        for g in ("n/a (read-only)", "none needed (no send/publish/spend)", "N/A", "pending"):
            _, out, _ = self.call(cg(green=g), _fake_result("coding", 0.99))
            self.assertEqual(json.loads(out)["choice"], "proceed", g)
            self.assertEqual(self.decisions()[-1]["green_status"], "none")

    def test_id_inside_text(self):
        reg = greens.load()
        self.assertEqual(reg.check("chris t1s2 2026-11-16 jev")[0], "valid")
        self.assertEqual(reg.check("Chris card t3066s12281 6:59 AM via Primo")[1], "t3066s12281")
        self.assertEqual(reg.check("t1s20")[0], "not_recorded")  # no prefix match

    def test_unreadable_registry_fails_closed(self):
        self.greens_path.write_text("entries: [unclosed", encoding="utf-8")
        _, out, _ = self.call(na(green="t1s2"), _fake_result("account", 0.99))
        self.assertEqual(json.loads(out)["choice"], "ask_chris")
        self.assertEqual(self.decisions()[-1]["reason"], "green_registry_unreadable")

    def test_cli_add_check_revoke_append_only(self):
        before = self.greens_path.read_bytes()
        rc, out, err = self.run_cli_raw(["greens", "add", "t3099s12400", "--source", "Chris card 11:00 AM ET", "--by", "Primo"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.greens_path.read_bytes().startswith(before))  # append-only
        rc, out, _ = self.run_cli_raw(["greens", "check", "chris t3099s12400"])
        self.assertTrue(json.loads(out)["valid"])
        _, out, _ = self.call(na(green="t3099s12400"), _fake_result("account", 0.3))
        self.assertEqual(json.loads(out)["choice"], "proceed")
        rc, _, _ = self.run_cli_raw(["greens", "revoke", "t3099s12400", "--source", "wrong card", "--by", "Primo"])
        self.assertEqual(rc, 0)
        _, out, _ = self.run_cli_raw(["greens", "check", "t3099s12400"])
        self.assertFalse(json.loads(out)["valid"])
        rc, _, _ = self.run_cli_raw(["greens", "add", "t3100u", "--standing", "--source", "s", "--by", "Primo"])
        _, out, _ = self.run_cli_raw(["greens", "check", "t3100u"])
        self.assertEqual(json.loads(out)["status"], "standing")

    def test_cli_add_rejects_free_text_and_missing_source(self):
        for args in (["greens", "add", "routine-self-delete-after-run", "--source", "x", "--by", "Primo"],
                     ["greens", "add", "none", "--source", "x", "--by", "Primo"],
                     ["greens", "add", "t3099s12400", "--by", "Primo"]):
            rc, _, _ = self.run_cli_raw(args)
            self.assertEqual(rc, 2, args)

    def run_cli_raw(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()


class T4CostCap(Enforce):
    def seed(self, cents, ts=None):
        ts = ts or datetime.now(timezone.utc).isoformat()
        p = Path(self.tmp.name) / "decisions.jsonl"
        with p.open("a") as f:
            f.write(json.dumps({"caller": "x", "cost_cents": cents, "ts": ts}) + "\n")

    def test_cap_reached_stops_before_jev(self):
        self.seed(300); self.seed(200)
        with mock.patch.object(router, "system_one", side_effect=AssertionError("Jev called")):
            _, out, _ = self.run_cli(cg())
        body = json.loads(out)
        self.assertEqual(body["choice"], "ask_chris")
        self.assertEqual(self.decisions()[-1]["reason"], "cost_cap")

    def test_cap_beats_green(self):
        self.seed(500)
        with mock.patch.object(router, "system_one", side_effect=AssertionError("Jev called")):
            _, out, _ = self.run_cli(na(green="t1s2"))
        self.assertEqual(json.loads(out)["choice"], "ask_chris")

    def test_last_month_does_not_count(self):
        self.seed(10_000, "2020-01-15T12:00:00+00:00")
        _, out, _ = self.call(cg(), _fake_result("coding", 0.9))
        self.assertEqual(json.loads(out)["choice"], "proceed")

    def test_estimate_over_remaining(self):
        self.seed(450)
        with mock.patch.object(router, "system_one", side_effect=AssertionError("Jev called")):
            _, out, _ = self.run_cli(cg(est_cost_cents=60))
        self.assertEqual(json.loads(out)["choice"], "ask_chris")
        _, out, _ = self.call({**cg(), "budget_cents_cap": 51})
        self.assertEqual(json.loads(out)["choice"], "ask_chris")
        _, out, _ = self.call(cg(est_cost_cents=40), _fake_result("coding", 0.9))
        self.assertEqual(json.loads(out)["choice"], "proceed")
        self.assertEqual(self.decisions()[-1]["committed_cents"], 40)
        # committed 40 now counts: 490 spent, 20 more is over the remaining 10
        with mock.patch.object(router, "system_one", side_effect=AssertionError("Jev called")):
            _, out, _ = self.run_cli(cg(est_cost_cents=20))
        self.assertEqual(json.loads(out)["choice"], "ask_chris")

    def test_window_and_corrections(self):
        p = Path(self.tmp.name) / "d.jsonl"
        p.write_text("\n".join([
            json.dumps({"cost_cents": 1.5, "ts": "2026-11-02T10:00:00+00:00"}),
            json.dumps({"committed_cents": 2, "ts": "2026-11-03T10:00:00-05:00"}),
            json.dumps({"cost_cents": 99, "ts": "2026-10-31T23:00:00-04:00"}),  # Oct ET
            json.dumps({"event": "correction", "cost_cents": 50, "ts": "2026-11-04T10:00:00+00:00"}),
            "not json", ""]))
        now = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(cost.spent_cents(p, "calendar_month_et", now), 3.5)
        self.assertAlmostEqual(cost.spent_cents(p, "all", now), 102.5)

    def test_test_rows_do_not_commit(self):
        _, out, _ = self.call({**cg(est_cost_cents=40), "test": True}, _fake_result("coding", 0.9))
        row = self.decisions()[-1]
        self.assertTrue(row["test"])
        self.assertIsNone(row["committed_cents"])


class T5CostTracking(Enforce):
    def test_model_usage_cost_logged(self):
        self.call(cg(), _result_with_usage(tin=1_000_000, tout=10))
        row = self.decisions()[-1]
        self.assertEqual(row["model"], "jev-1.13.0")
        self.assertEqual(row["usage"], {"input_tokens": 1_000_000, "output_tokens": 10})
        self.assertAlmostEqual(row["cost_cents"], 4.2)  # 0.042 USD per 1M input tokens
        self.assertEqual(row["cost_reason"], "")

    def test_null_with_reason_when_api_gives_none(self):
        self.call(cg(), _fake_result("coding", 0.9))
        row = self.decisions()[-1]
        self.assertIsNone(row["cost_cents"]); self.assertIsNone(row["usage"])
        self.assertEqual(row["cost_reason"], "no_usage_from_api")

    def test_every_row_has_cost_fields(self):
        self.cfg["gate"] = "off"
        self.run_cli(cg())
        row = self.decisions()[-1]
        for k in ("cost_cents", "model", "usage", "cost_reason"):
            self.assertIn(k, row)
        self.assertEqual(row["cost_reason"], "no_jev_call")


class T6Timeout(Enforce):
    def test_timeout_fails_closed_and_logs(self):
        for st, want in ((na(green=""), "ask_chris"), (na(green="t1s2"), "ask_chris"), (cg(), "hold")):
            with mock.patch.object(router, "system_one", side_effect=jev_client.JevTimeout("3s")):
                rc, out, _ = self.run_cli(st)
            self.assertEqual(rc, cli.EXIT_JEV_ERROR)
            self.assertEqual(json.loads(out)["choice"], want)
            row = self.decisions()[-1]
            self.assertEqual((row["error"], row["choice"], row["acted_on"]), ("jev_timeout", want, "fail_closed"))

    def test_router_passes_3s(self):
        with mock.patch.object(router, "system_one", return_value=_fake_result()) as m:
            self.run_cli(cg())
        self.assertEqual(m.call_args.kwargs["timeout_s"], 3.0)

    def test_client_wall_clock(self):
        class Slow:
            def __init__(self, **kw): Slow.kw = kw
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def system_one(self, **kw): time.sleep(2); return "late"
        with mock.patch.object(jev_client, "TypeSafeClient", Slow), \
             mock.patch.object(jev_client, "ensure_typesafe_api_key", return_value=True):
            t = time.monotonic()
            with self.assertRaises(jev_client.JevTimeout):
                jev_client.system_one({}, {}, model="m", timeout_s=0.2)
            self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(Slow.kw["timeout"], 0.2)
        self.assertEqual(Slow.kw["retry"].max_retries, 0)


class T7RowOnEveryPath(Enforce):
    def test_bad_json_and_empty_goal_write_rows(self):
        self.run_cli_raw = T3RealGreenCheck.run_cli_raw.__get__(self)
        self.run_cli_raw(["{not json"])
        self.assertEqual(self.decisions()[-1]["error"], "bad_input")
        self.run_cli({"decision_type": "needs_approval", "caller": "Loop", "context": {"goal": " "}})
        row = self.decisions()[-1]
        self.assertEqual((row["error"], row["choice"]), ("goal_required", "ask_chris"))

    def test_legacy_fields_on_every_row(self):
        self.call(cg(), _fake_result("coding", 0.9))
        self.call(na(green="t1s2"), _fake_result("account", 0.9))
        with mock.patch.object(router, "key_present", return_value=False):
            self.run_cli(cg())
        self.cfg["gate"] = "off"; self.run_cli(cg())
        for row in self.decisions():
            for k in LEGACY:
                self.assertIn(k, row)
            self.assertEqual(row["written_by"], "jev-cli")


class T8RunsCaller(Enforce):
    def test_runs_jsonl_has_caller(self):
        self.call(cg(), _fake_result("coding", 0.9))
        runs = [json.loads(l) for l in (Path(self.tmp.name) / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(runs[-1]["caller"], "Loop")


if __name__ == "__main__":
    unittest.main()
