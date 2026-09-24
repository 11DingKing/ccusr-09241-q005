"""模拟器场景测试：核对无缝交接、容量守恒、冲突等待与无解说明。"""
import json
import os
import unittest

from maritime_handover.interfaces.simulator import run_scenario

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def load(name: str) -> dict:
    with open(os.path.join(EXAMPLES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


class BasicScenarioTests(unittest.TestCase):
    def setUp(self):
        self.report = run_scenario(load("scenario_basic.json"))

    def test_seamless_handover_chain(self):
        checks = self.report["checks"]
        self.assertTrue(checks["seamless_handover"]["ok"])
        task_a = checks["seamless_handover"]["tasks"]["A"]
        self.assertEqual(task_a["handovers"], 2)
        self.assertEqual(task_a["gaps"], [])

    def test_capacity_conserved(self):
        self.assertTrue(self.report["checks"]["capacity_conservation"]["ok"])

    def test_events_carry_time_and_reason(self):
        events = self.report["events"]
        handovers = [e for e in events if e["kind"] == "handover"]
        self.assertEqual([h["tick"] for h in handovers], [20, 40])
        self.assertTrue(all(h["reason"] == "window_end" for h in handovers))
        self.assertTrue(self.report["ok"])


class ConflictScenarioTests(unittest.TestCase):
    def setUp(self):
        self.report = run_scenario(load("scenario_conflict.json"))

    def test_capacity_conserved_under_preemption(self):
        check = self.report["checks"]["capacity_conservation"]
        self.assertTrue(check["ok"], msg=str(check["violations"]))

    def test_conflict_waiting_reported(self):
        waiting = {
            t["task_id"]: t["waited_ticks"]
            for t in self.report["checks"]["conflict_waiting"]["tasks"]
        }
        self.assertEqual(waiting["WAITER"], 19)

    def test_infeasible_tasks_explained(self):
        infeasible = self.report["checks"]["infeasible_tasks"]
        by_task = {t["task_id"]: t for t in infeasible["tasks"]}
        self.assertIn("LOW", by_task)
        self.assertIn("EMERG", by_task)
        self.assertEqual(by_task["EMERG"]["uncovered"], [[40, 50]])
        self.assertEqual(by_task["EMERG"]["reasons"], ["capacity_exhausted"])

    def test_dual_approval_flow_visible_in_actions(self):
        approvals = [a for a in self.report["actions"] if a["action"] == "approve"]
        self.assertEqual(
            [a["status"] for a in approvals],
            ["recorded", "duplicate_ignored", "granted"],
        )

    def test_emergency_slot_taken_only_after_approval(self):
        events = self.report["events"]
        release = next(
            e for e in events if e["task_id"] == "EMERG" and e["kind"] == "release"
        )
        self.assertEqual(release["reason"], "preempted_by:NORMAL2")
        self.assertEqual(release["tick"], 40)


class ReplanScenarioTests(unittest.TestCase):
    def setUp(self):
        self.report = run_scenario(load("scenario_replan.json"))

    def test_late_window_version_triggers_handover(self):
        events = self.report["events"]
        handovers = [e for e in events if e["kind"] == "handover"]
        self.assertEqual(len(handovers), 1)
        self.assertEqual(handovers[0]["tick"], 30)
        self.assertEqual(handovers[0]["link_id"], "SAT1")
        self.assertEqual(handovers[0]["to_link_id"], "SAT2")

    def test_unaffected_task_events_stable_across_plans(self):
        # B 的事件在所有计划版本中都应相同（只重算受影响区段）
        events = self.report["events"]
        b_events = [e for e in events if e["task_id"] == "B"]
        self.assertEqual(
            [(e["kind"], e["tick"], e["link_id"]) for e in b_events],
            [("access", 0, "SAT3"), ("release", 60, "SAT3")],
        )
        self.assertTrue(self.report["ok"])

    def test_replan_happened_at_ingest_time(self):
        versions = [(p["version"], p["generated_at"]) for p in self.report["plans"]]
        self.assertIn((4, 20), versions)


if __name__ == "__main__":
    unittest.main()
