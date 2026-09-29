"""模拟器与核对器测试：四项核对、文件往返、示例场景全绿。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from maritime_handover.adapters.json_repo import JsonFileRepository
from maritime_handover.application.planning_service import PlanningService
from maritime_handover.adapters.drivers import (
    FixedClock,
    ListObserver,
    SequentialIdGenerator,
)
from maritime_handover.simulator.simulator import run_file, run_simulation


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "rescue_scenario.json")


class SimulatorVerificationTests(unittest.TestCase):
    def test_bundled_example_passes_all_checks(self) -> None:
        report = run_file(EXAMPLE)
        checks = report["verification"]
        self.assertTrue(checks["seamless_handover"]["passed"],
                        checks["seamless_handover"]["violations"])
        self.assertTrue(checks["capacity_conservation"]["passed"],
                        checks["capacity_conservation"]["over_capacity"])
        self.assertTrue(checks["conflict_waits"]["passed"],
                        checks["conflict_waits"]["unjustified"])
        self.assertTrue(checks["unschedulable"]["passed"])
        self.assertTrue(report["summary"]["verification_passed"])
        # 至少存在一次无缝交接与一条冲突等待证据。
        self.assertGreaterEqual(checks["seamless_handover"]["handover_count"], 1)
        self.assertGreaterEqual(checks["conflict_waits"]["wait_events"], 1)

    def test_report_explains_unschedulable_task(self) -> None:
        report = run_file(EXAMPLE)
        ids = {row["task_id"]
               for row in report["verification"]["unschedulable"]["unschedulable"]}
        # 示例中 T_B 因容量竞争无法满足全部需求，必须有明确说明。
        self.assertIn("T_B", ids)

    def test_capacity_violation_is_detected(self) -> None:
        """构造超容量场景：核对器应能发现超额占用（引擎不会产生，手工构造）。"""
        from maritime_handover.domain.enums import SegmentState
        from maritime_handover.domain.models import (
            PlanSegment, Resource, Task, Terminal,
        )
        from maritime_handover.domain.enums import LinkKind
        from maritime_handover.simulator.verifier import Verifier

        resources = {"SAT": Resource("SAT", LinkKind.SATELLITE, "B", capacity=1)}
        terminals = {"a": Terminal("a", "A"), "b": Terminal("b", "B")}
        tasks = {"A": Task("A", "a", 0, 4, 4, 1),
                 "B": Task("B", "b", 0, 4, 4, 1)}
        bad_segments = [
            PlanSegment("A", "a", 0, 4, SegmentState.LINK, "SAT"),
            PlanSegment("B", "b", 0, 4, SegmentState.LINK, "SAT"),
        ]
        verifier = Verifier(resources, terminals, tasks, [],
                            bad_segments, [], 4)
        out = verifier.check_capacity()
        self.assertFalse(out["passed"])
        self.assertEqual(len(out["over_capacity"]), 4)

    def test_handover_gap_is_detected(self) -> None:
        from maritime_handover.domain.enums import (
            EventType,
            LinkKind,
            Reason,
            SegmentState,
        )
        from maritime_handover.domain.models import (
            PlanSegment, Resource, Task, Terminal, TimelineEvent,
        )
        from maritime_handover.simulator.verifier import Verifier

        resources = {"SAT": Resource("SAT", LinkKind.SATELLITE, "S", 1),
                     "GND": Resource("GND", LinkKind.TERRESTRIAL, "G", 1)}
        terminals = {"a": Terminal("a", "A")}
        tasks = {"A": Task("A", "a", 0, 8, 8, 1)}
        # 旧段 [0,4) 结束，新段 [5,9) 才开始——时刻 4 有空档。
        segments = [
            PlanSegment("A", "a", 0, 4, SegmentState.LINK, "SAT"),
            PlanSegment("A", "a", 5, 9, SegmentState.LINK, "GND"),
        ]
        events = [TimelineEvent(
            time=5, event_type=EventType.HANDOVER, task_id="A",
            terminal_id="a", resource_id="GND", reason=Reason.WINDOW_END_HANDOVER,
            event_key="k")]
        verifier = Verifier(resources, terminals, tasks, [], segments, events, 9)
        out = verifier.check_seamless_handover()
        self.assertFalse(out["passed"])

    def test_simulation_runs_without_predictions(self) -> None:
        spec = {
            "scenario": {
                "resources": [
                    {"id": "SAT1", "kind": "satellite", "name": "B",
                     "capacity": 1}],
                "terminals": [{"id": "A", "name": "甲船"}],
                "tasks": [{"id": "T1", "terminal_id": "A", "release": 0,
                           "deadline": 4, "demand": 4, "priority": 1}],
                "horizon": 4,
            },
            "predictions": [],
        }
        report = run_simulation(spec)
        # 无窗口：任务无解，但核对（含无解说明）通过。
        self.assertTrue(report["summary"]["verification_passed"])
        self.assertEqual(len(report["verification"]["unschedulable"]["unschedulable"]), 1)


class JsonPersistenceTests(unittest.TestCase):
    def test_state_round_trips_through_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "state.json")
            repo = JsonFileRepository(path)
            clock = FixedClock(0)
            service = PlanningService(
                scenarios=repo, plans=repo, clock=clock,
                id_gen=SequentialIdGenerator(), observer=ListObserver())
            service.setup_scenario({
                "resources": [{"id": "SAT1", "kind": "satellite",
                               "name": "B", "capacity": 1}],
                "terminals": [{"id": "A", "name": "甲船"}],
                "tasks": [{"id": "T1", "terminal_id": "A", "release": 0,
                           "deadline": 4, "demand": 4, "priority": 1}],
                "horizon": 4,
            })
            service.ingest_prediction({"version": 1, "windows": [
                {"window_uid": "w1", "terminal_id": "A",
                 "resource_id": "SAT1", "start": 0, "end": 4}]})
            service.generate_initial_plan()
            repo.flush()
            self.assertTrue(os.path.exists(path))

            reopened = JsonFileRepository(path)
            self.assertEqual(reopened.latest_version(), 1)
            # 需求恰好填满窗口：单个 LINK 段。
            self.assertEqual(len(reopened.list_segments()), 1)
            self.assertTrue(reopened.list_events())


if __name__ == "__main__":
    unittest.main()
