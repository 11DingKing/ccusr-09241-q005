"""应用服务测试：差量、增量重算、紧急保护、双人批准与幂等。"""

from __future__ import annotations

import unittest

from maritime_handover.adapters.drivers import (
    FixedClock,
    ListObserver,
    SequentialIdGenerator,
)
from maritime_handover.adapters.memory_repo import InMemoryRepository
from maritime_handover.application.planning_service import PlanningService
from maritime_handover.application.scheduler import diff_windows
from maritime_handover.domain.enums import SegmentState
from maritime_handover.domain.models import Window


def make_service(clock: FixedClock | None = None):
    clock = clock or FixedClock(0)
    repo = InMemoryRepository()
    observer = ListObserver()
    service = PlanningService(
        scenarios=repo, plans=repo, clock=clock,
        id_gen=SequentialIdGenerator(), observer=observer,
    )
    return service, repo, clock, observer


def basic_scenario(service: PlanningService) -> None:
    service.setup_scenario({
        "resources": [
            {"id": "SAT1", "kind": "satellite", "name": "波束1", "capacity": 1},
            {"id": "GND1", "kind": "terrestrial", "name": "地面站", "capacity": 1},
        ],
        "terminals": [
            {"id": "A", "name": "甲船", "cooldown": 0},
            {"id": "B", "name": "乙船", "cooldown": 0},
        ],
        "tasks": [
            {"id": "T1", "terminal_id": "A", "release": 0, "deadline": 12,
             "demand": 10, "priority": 1},
            {"id": "T2", "terminal_id": "B", "release": 0, "deadline": 12,
             "demand": 10, "priority": 2},
        ],
        "horizon": 12,
    })


class WindowDiffTests(unittest.TestCase):
    def test_add_remove_change_by_identity(self) -> None:
        old = [
            Window("w1", "A", "SAT1", 0, 6, 1),
            Window("w2", "A", "GND1", 6, 12, 1),
        ]
        new = [
            Window("w1", "A", "SAT1", 0, 4, 2),       # changed
            Window("w3", "A", "GND1", 4, 12, 2),      # added
            # w2 removed
        ]
        diff = diff_windows(old, new)
        self.assertEqual([w.window_uid for w in diff.added], ["w3"])
        self.assertEqual([w.window_uid for w in diff.removed], ["w2"])
        self.assertEqual([w.window_uid for w, _ in diff.changed], ["w1"])
        self.assertEqual(diff.affected_terminals(), {"A"})


class IncrementalRecomputeTests(unittest.TestCase):
    def test_late_prediction_only_recomputes_affected_terminal(self) -> None:
        service, repo, clock, _ = make_service()
        basic_scenario(service)
        service.ingest_prediction({"version": 1, "windows": [
            {"window_uid": "a-s", "terminal_id": "A", "resource_id": "SAT1",
             "start": 0, "end": 12},
            {"window_uid": "b-s", "terminal_id": "B", "resource_id": "SAT1",
             "start": 0, "end": 12},
        ]})
        first = service.generate_initial_plan()
        self.assertEqual(set(first["affected_task_ids"]), {"T1", "T2"})

        clock.set(3)
        second = service.ingest_prediction({"version": 2, "windows": [
            {"window_uid": "a-s", "terminal_id": "A", "resource_id": "SAT1",
             "start": 0, "end": 5},
            {"window_uid": "a-g", "terminal_id": "A", "resource_id": "GND1",
             "start": 5, "end": 12},
            {"window_uid": "b-s", "terminal_id": "B", "resource_id": "SAT1",
             "start": 0, "end": 12},
        ]})
        # 只有 A 船的窗口变化，T2 不重算。
        self.assertEqual(second["affected_task_ids"], ["T1"])
        segs_t1 = repo.list_segments("T1")
        # cutoff 之前的段保留（被合并为 [0,5) 的 SAT 历史视图）。
        self.assertTrue(any(s.start == 0 and s.end >= 3 for s in segs_t1))
        # T2 段完全未被触碰，仍只在 SAT1。
        t2_resources = {s.resource_id for s in repo.list_segments("T2")
                        if s.state is SegmentState.LINK}
        self.assertEqual(t2_resources, {"SAT1"})

    def test_identical_prediction_does_not_recompute(self) -> None:
        service, repo, clock, _ = make_service()
        basic_scenario(service)
        service.ingest_prediction({"version": 1, "windows": [
            {"window_uid": "a-s", "terminal_id": "A", "resource_id": "SAT1",
             "start": 0, "end": 12},
            {"window_uid": "b-s", "terminal_id": "B", "resource_id": "SAT1",
             "start": 0, "end": 12},
        ]})
        service.generate_initial_plan()
        events_before = len(repo.list_events())
        # 同内容、新版本：差量为空，不重算。
        out = service.ingest_prediction({"version": 2, "windows": [
            {"window_uid": "a-s", "terminal_id": "A", "resource_id": "SAT1",
             "start": 0, "end": 12},
            {"window_uid": "b-s", "terminal_id": "B", "resource_id": "SAT1",
             "start": 0, "end": 12},
        ]})
        self.assertFalse(out["changed"])
        self.assertEqual(len(repo.list_events()), events_before)

    def test_prediction_version_must_be_strictly_increasing(self) -> None:
        service, _, _, _ = make_service()
        basic_scenario(service)
        service.ingest_prediction({"version": 1, "windows": [
            {"window_uid": "a-s", "terminal_id": "A", "resource_id": "SAT1",
             "start": 0, "end": 12},
        ]})
        with self.assertRaises(ValueError):
            service.ingest_prediction({"version": 1, "windows": [
                {"window_uid": "a-s", "terminal_id": "A", "resource_id": "SAT1",
                 "start": 0, "end": 11},
            ]})


class EmergencyProtectionTests(unittest.TestCase):
    def _emergency_service(self):
        service, repo, clock, _ = make_service()
        service.setup_scenario({
            "resources": [
                {"id": "SAT1", "kind": "satellite", "name": "波束",
                 "capacity": 1},
            ],
            "terminals": [
                {"id": "E", "name": "急救船"},
                {"id": "N", "name": "普通船"},
            ],
            "tasks": [
                {"id": "EM", "terminal_id": "E", "release": 0, "deadline": 10,
                 "demand": 8, "priority": 1, "emergency": True},
                {"id": "NM", "terminal_id": "N", "release": 0, "deadline": 10,
                 "demand": 8, "priority": 9},
            ],
            "horizon": 10,
        })
        service.ingest_prediction({"version": 1, "windows": [
            {"window_uid": "we", "terminal_id": "E", "resource_id": "SAT1",
             "start": 0, "end": 10},
            {"window_uid": "wn", "terminal_id": "N", "resource_id": "SAT1",
             "start": 0, "end": 10},
        ]})
        service.generate_initial_plan()
        return service, repo, clock

    def test_emergency_task_keeps_beam_and_creates_lock(self) -> None:
        _, repo, _ = self._emergency_service()
        em_link = [s for s in repo.list_segments("EM")
                   if s.state is SegmentState.LINK]
        self.assertTrue(em_link)
        self.assertTrue(any(l.task_id == "EM" for l in repo.list_locks()))
        nm_link = [s for s in repo.list_segments("NM")
                   if s.state is SegmentState.LINK]
        # 普通任务在紧急任务完成前完全拿不到波束。
        self.assertTrue(all(s.start >= 8 for s in nm_link))

    def test_single_approver_does_not_grant(self) -> None:
        service, _, clock = self._emergency_service()
        clock.set(2)
        out = service.submit_approval({
            "request_id": "R1", "approver": "甲", "task_id": "NM",
            "resource_id": "SAT1", "start": 2, "end": 6,
        })
        self.assertFalse(out["granted"])
        self.assertNotIn("recompute", out)

    def test_duplicate_same_approver_is_idempotent(self) -> None:
        service, _, clock = self._emergency_service()
        clock.set(2)
        payload = {
            "request_id": "R1", "approver": "甲", "task_id": "NM",
            "resource_id": "SAT1", "start": 2, "end": 6,
        }
        a = service.submit_approval(dict(payload))
        b = service.submit_approval(dict(payload))
        self.assertEqual(a["approvers"], ["甲"])
        self.assertEqual(b["approvers"], ["甲"])

    def test_two_distinct_approvers_override_and_recompute(self) -> None:
        service, repo, clock = self._emergency_service()
        clock.set(2)
        base = {"task_id": "NM", "resource_id": "SAT1",
                "start": 2, "end": 6}
        service.submit_approval({"request_id": "R1", "approver": "甲", **base})
        out = service.submit_approval(
            {"request_id": "R1", "approver": "乙", **base})
        self.assertTrue(out["granted"])
        # 受益任务与被挤让紧急任务都纳入重算。
        self.assertEqual(set(out["recompute"]["affected_task_ids"]),
                         {"EM", "NM"})
        nm_link = [(s.start, s.end) for s in repo.list_segments("NM")
                   if s.state is SegmentState.LINK]
        self.assertIn((2, 6), nm_link)


if __name__ == "__main__":
    unittest.main()
