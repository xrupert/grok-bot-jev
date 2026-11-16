"""creative_qc decision type (2026-11-16 Engineer, Chris green t3082s12374 'retire the second door').
process-gate moves off the retired 127.0.0.1:8787 service onto this CLI. Same semantics as the
8787 service (jev-decision-layer questions.py + jev_decide.policy):
  verdict pass | conditional | fail | human; only pass gets through (process-gate's rule).
  confidence < 0.5 (thresholds.ask_chris_below) -> human; pass with blocking >= 0.5 -> human.
  Jev down / timed out / key missing / over the $5 cap -> human (exit 0 with decision human, or nonzero).
Every path writes a decisions.jsonl row (caller, cost_cents, model, usage). Mocked API only.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import jev_client, router
from tests.test_cli_harness import Base


def qc_result(choice="pass", conf=0.95, blocking=0.1, severity=0, tin=900, tout=30, model="jev-1.13.0"):
    return SimpleNamespace(
        choices={"verdict": SimpleNamespace(choice=choice, confidence=conf, probabilities={choice: conf})},
        nouls={"blocking": SimpleNamespace(noul=blocking)},
        scores={"severity": SimpleNamespace(score=severity)},
        model=model, usage=SimpleNamespace(input_tokens=tin, output_tokens=tout))


def qc(text="## Gate 1: PASS\n- No defects found.", caller="process-gate", **ctx):
    return {"decision_type": "creative_qc", "caller": caller,
            "context": {"goal": "process-gate QA verdict for /x/piece", "text": text, "item_id": "/x/piece", **ctx}}


class QC(Base):
    def setUp(self):
        super().setUp()
        self.cfg["gate"] = "enforce"
        self.cfg["model"] = "jev-1.13.0"

    def call(self, state, result=None, **patch):
        kw = patch or {"return_value": result or qc_result()}
        with mock.patch.object(router, "system_one", **kw) as m:
            rc, out, err = self.run_cli(state)
        self.mock = m
        return rc, (json.loads(out) if out.strip() else {}), err


class TPolicy(QC):
    def test_pass(self):
        rc, out, _ = self.call(qc())
        self.assertEqual(rc, 0)
        self.assertEqual(out["decision"], "pass")
        self.assertEqual(out["verdict"], {"choice": "pass", "confidence": 0.95})
        self.assertTrue(out["jev_used"])

    def test_conditional_and_fail_pass_through_as_is(self):
        for v in ("conditional", "fail"):
            _, out, _ = self.call(qc(), qc_result(v, 0.9, blocking=0.9))
            self.assertEqual(out["decision"], v)

    def test_low_confidence_is_human(self):
        _, out, _ = self.call(qc(), qc_result("pass", 0.49))
        self.assertEqual((out["decision"], out["reason"]), ("human", "low_confidence"))
        _, out, _ = self.call(qc(), qc_result("pass", 0.5))
        self.assertEqual(out["decision"], "pass")

    def test_none_confidence_is_human(self):
        _, out, _ = self.call(qc(), qc_result("pass", None))
        self.assertEqual(out["decision"], "human")

    def test_pass_with_blocking_defect_is_human(self):
        _, out, _ = self.call(qc(), qc_result("pass", 0.95, blocking=0.5))
        self.assertEqual((out["decision"], out["reason"]), ("human", "pass_with_blocking_defect"))

    def test_question_set_and_state_match_8787(self):
        self.call(qc(text="1. fix logo 0:12 score 7/10"))
        args, kwargs = self.mock.call_args
        state, questions = args[0], args[1]
        self.assertEqual(set(questions), {"verdict", "blocking", "severity"})
        self.assertEqual(state["review"], "1. fix logo 0:12 score 7/10")
        self.assertEqual(state["facts_from_code"], {"numbered_fixes": 1, "timecodes": 1, "score_10": 7})
        self.assertEqual(kwargs.get("model"), "jev-1.13.0")
        self.assertEqual(kwargs.get("timeout_s"), 3.0)

    def test_text_capped_at_3500(self):
        self.call(qc(text="a" * 5000 + "END"))
        self.assertEqual(len(self.mock.call_args[0][0]["review"]), 3500)
        self.assertTrue(self.mock.call_args[0][0]["review"].endswith("END"))


class TFailClosed(QC):
    def test_timeout_is_human(self):
        rc, out, _ = self.call(qc(), side_effect=jev_client.JevTimeout("3s"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(out["decision"], "human")
        row = self.decisions()[-1]
        self.assertEqual((row["error"], row["choice"], row["acted_on"]), ("jev_timeout", "human", "fail_closed"))

    def test_api_error_is_human(self):
        rc, out, _ = self.call(qc(), side_effect=RuntimeError("boom"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(out["decision"], "human")
        self.assertTrue(self.decisions()[-1]["error"].startswith("jev_error"))

    def test_key_missing_is_human_no_call(self):
        with mock.patch.object(router, "key_present", return_value=False):
            rc, out, _ = self.call(qc())
        self.assertEqual((out["decision"], out["reason"]), ("human", "precheck:key_missing"))
        self.mock.assert_not_called()

    def test_caller_missing_is_human_no_call(self):
        rc, out, _ = self.call(qc(caller=""))
        self.assertEqual(out["decision"], "human")
        self.mock.assert_not_called()

    def test_over_cap_is_human_no_call(self):
        dec = Path(self.tmp.name) / "decisions.jsonl"
        from datetime import datetime, timezone
        dec.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "cost_cents": 500.0}) + "\n")
        rc, out, _ = self.call(qc())
        self.assertEqual((out["decision"], out["reason"]), ("human", "cost_cap"))
        self.mock.assert_not_called()
        self.assertEqual(self.decisions()[-1]["error"].split(":")[0], "cost_cap")

    def test_gate_off_is_human(self):
        self.cfg["gate"] = "off"
        _, out, _ = self.call(qc())
        self.assertEqual(out["decision"], "human")
        self.mock.assert_not_called()

    def test_empty_text_is_human_no_call(self):
        _, out, _ = self.call(qc(text="  "))
        self.assertEqual(out["decision"], "human")
        self.mock.assert_not_called()

    def test_goal_defaults_when_missing(self):
        st = qc(); del st["context"]["goal"]
        rc, out, _ = self.call(st)
        self.assertEqual((rc, out["decision"]), (0, "pass"))


class TLedger(QC):
    def test_row_fields(self):
        self.call(qc())
        row = self.decisions()[-1]
        self.assertEqual(row["decision_type"], "creative_qc")
        self.assertEqual(row["caller"], "process-gate")
        self.assertEqual(row["choice"], "pass")
        self.assertEqual(row["model"], "jev-1.13.0")
        self.assertEqual(row["usage"], {"input_tokens": 900, "output_tokens": 30})
        self.assertGreater(row["cost_cents"], 0)
        self.assertEqual(row["written_by"], "jev-cli")
        self.assertEqual(row["verdict"], {"choice": "pass", "confidence": 0.95})
        self.assertNotIn("text", json.dumps(row.get("action", "")))
        self.assertIsNone(row.get("committed_cents"))

    def test_cost_counts_toward_cap(self):
        from src import cost
        self.call(qc())
        dec = Path(self.tmp.name) / "decisions.jsonl"
        self.assertGreater(cost.spent_cents(dec), 0)

    def test_runs_jsonl_has_caller(self):
        self.call(qc())
        runs = [json.loads(l) for l in (Path(self.tmp.name) / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(runs[-1]["caller"], "process-gate")
        self.assertEqual(runs[-1]["event"], "creative_qc")

    def test_existing_types_unchanged(self):
        self.assertIn("costly_action_gate", router.DECISION_TYPES)
        self.assertIn("needs_approval", router.DECISION_TYPES)


if __name__ == "__main__":
    unittest.main()
