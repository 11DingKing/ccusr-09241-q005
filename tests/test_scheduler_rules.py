"""排程引擎规则测试：优先级、最短保持、冷却、容量、互斥与无解说明。"""

from __future__ import annotations

import unittest

from maritime_handover.application.scheduler import (
    ScheduleRequest,
    SchedulerEngine,
)
from maritime_handover.domain.enums import EventType, LinkKind, Reason, SegmentState
from maritime_handover.domain.models import (
    Resource,
    Task,
    Terminal,
    Window,
)


def _request(**overrides) -> ScheduleRequest:
    resources = {
        "SAT": Resource("SAT", LinkKind.SATELLITE, "波束", capacity=1),
        "GND": Resource("GND", LinkKind.TERRESTRIAL, "地面", capacity=1),
    }
    terminals = {
        "a": Terminal("a", "甲船", cooldown=0),
        "b": Terminal("b", "乙船", cooldown=0),
    }
    base = {
        "resources": resources,
        "terminals": terminals,
        "tasks": [],
        "windows": [],
        "horizon": 10,
    }
    base.update(overrides)
    return ScheduleRequest(**base)


def link_segments(result, task_id: str):
    return [(s.start, s.end, s.resource_id) for s in result.segments
            if s.task_id == task_id and s.state is SegmentState.LINK]


class SchedulerRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SchedulerEngine()

    def test_continuous_windows_produce_seamless_handover(self) -> None:
        req = _request(
            tasks=[Task("A", "a", 0, 10, 8, 1)],
            windows=[
                Window("w1", "a", "SAT", 0, 4, 1),
                Window("w2", "a", "GND", 4, 10, 1),
            ],
        )
        result = self.engine.schedule(req)
        self.assertEqual(link_segments(result, "A"),
                         [(0, 4, "SAT"), (4, 8, "GND")])
        ho = [e for e in result.events if e.event_type is EventType.HANDOVER]
        self.assertEqual(len(ho), 1)
        self.assertEqual((ho[0].time, ho[0].resource_id), (4, "GND"))
        self.assertIs(ho[0].reason, Reason.WINDOW_END_HANDOVER)

    def test_cooldown_blocks_reaccess_after_real_drop(self) -> None:
        terminals = {"a": Terminal("a", "甲船", cooldown=2)}
        req = _request(
            terminals=terminals,
            tasks=[Task("A", "a", 0, 12, 10, 1)],
            windows=[
                Window("w1", "a", "SAT", 0, 4, 1),
                Window("w2", "a", "GND", 5, 12, 1),
            ],
            horizon=12,
        )
        result = self.engine.schedule(req)
        # 4 掉线，冷却到 6，GND 自 6 起接入；需求 10：4+6，占用到 12。
        self.assertEqual(link_segments(result, "A"),
                         [(0, 4, "SAT"), (6, 12, "GND")])
        reasons = {(e.time, e.reason) for e in result.events}
        self.assertIn((4, Reason.COOLDOWN), reasons)
        self.assertIn((6, Reason.COOLDOWN_READY), reasons)

    def test_seamless_handover_is_exempt_from_cooldown(self) -> None:
        terminals = {"a": Terminal("a", "甲船", cooldown=5)}
        req = _request(
            terminals=terminals,
            tasks=[Task("A", "a", 0, 12, 10, 1)],
            windows=[
                Window("w1", "a", "SAT", 0, 4, 1),
                Window("w2", "a", "GND", 4, 12, 1),
            ],
            horizon=12,
        )
        result = self.engine.schedule(req)
        self.assertEqual(link_segments(result, "A"),
                         [(0, 4, "SAT"), (4, 10, "GND")])

    def test_min_hold_skips_too_short_window(self) -> None:
        req = _request(
            tasks=[Task("A", "a", 0, 12, 9, 1, min_hold=4)],
            windows=[
                Window("w1", "a", "SAT", 0, 3, 1),
                Window("w2", "a", "GND", 3, 12, 1),
            ],
            horizon=12,
        )
        result = self.engine.schedule(req)
        # SAT 只剩 3 刻度 < min_hold=4，放弃，等 GND。
        self.assertEqual(link_segments(result, "A"), [(3, 12, "GND")])

    def test_capacity_shared_when_beam_room_allows(self) -> None:
        resources = {"SAT": Resource("SAT", LinkKind.SATELLITE, "波束", capacity=2)}
        req = _request(
            resources=resources,
            tasks=[Task("A", "a", 0, 8, 8, 2), Task("B", "b", 0, 8, 8, 3)],
            windows=[
                Window("wa", "a", "SAT", 0, 8, 1),
                Window("wb", "b", "SAT", 0, 8, 1),
            ],
            horizon=8,
        )
        result = self.engine.schedule(req)
        self.assertEqual(result.stats()["link_ticks"], 16)
        self.assertEqual(result.unschedulable, [])

    def test_higher_priority_preempts_only_after_commit_expires(self) -> None:
        req = _request(
            tasks=[
                Task("LO", "a", 0, 10, 10, 5, min_hold=3),
                Task("HI", "b", 2, 10, 4, 1, emergency=True, min_hold=1),
            ],
            windows=[
                Window("wa", "a", "SAT", 0, 10, 1),
                Window("wb", "b", "SAT", 0, 10, 1),
            ],
            horizon=10,
        )
        result = self.engine.schedule(req)
        # LO 在 0 接入并承诺到 3，HI 在 2 到达也必须等到 3。
        self.assertEqual(link_segments(result, "LO")[0], (0, 3, "SAT"))
        self.assertEqual(link_segments(result, "HI")[0], (3, 7, "SAT"))

    def test_no_window_task_is_reported_unschedulable(self) -> None:
        req = _request(tasks=[Task("A", "a", 0, 6, 6, 1)], horizon=6)
        result = self.engine.schedule(req)
        self.assertEqual(len(result.unschedulable), 1)
        report = result.unschedulable[0]
        self.assertEqual((report.demanded, report.served, report.shortfall),
                         (6, 0, 6))
        self.assertIn("no_window", report.blocker_ticks)

    def test_segment_time_is_half_open_and_complete(self) -> None:
        # 需求超过可获得量，段必须铺满整个时间窗（含末尾等待）。
        req = _request(
            tasks=[Task("A", "a", 0, 10, 20, 1)],
            windows=[Window("w1", "a", "SAT", 0, 6, 1)],
        )
        result = self.engine.schedule(req)
        segs = [s for s in result.segments if s.task_id == "A"]
        cursor = 0
        for s in sorted(segs, key=lambda s: s.start):
            self.assertEqual(s.start, cursor)
            cursor = s.end
        self.assertEqual(cursor, 10)
        self.assertEqual(segs[0].state, SegmentState.LINK)
        self.assertEqual(segs[-1].state, SegmentState.WAIT)

    def test_same_terminal_tasks_cannot_overlap(self) -> None:
        terminals = {"a": Terminal("a", "甲船")}
        req = _request(
            terminals=terminals,
            tasks=[
                Task("A", "a", 0, 8, 8, 1),
                Task("B", "a", 0, 8, 8, 2),
            ],
            windows=[Window("w1", "a", "SAT", 0, 8, 1)],
            horizon=8,
        )
        result = self.engine.schedule(req)
        ticks: dict[int, int] = {}
        for s in result.segments:
            if s.state is SegmentState.LINK:
                for t in range(s.start, s.end):
                    ticks[t] = ticks.get(t, 0) + 1
        self.assertTrue(all(v == 1 for v in ticks.values()))


if __name__ == "__main__":
    unittest.main()
