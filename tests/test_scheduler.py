"""调度器行为测试：覆盖、交接、容量、冷却、最短保持、互斥与抢占。"""
import unittest

from maritime_handover.domain.planner import derive_events
from tests.support import make_service, sessions_of, gaps_of, window


class BasicSchedulingTests(unittest.TestCase):
    def test_window_chain_produces_seamless_handovers(self):
        service = make_service(
            links={
                "SAT1": [window("SAT1", 0, 10)],
                "SAT2": [window("SAT2", 10, 20)],
            },
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 3}
            ],
        )
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 20, "min_hold": 2}
        )
        self.assertEqual(
            sessions_of(service, "A"),
            [("SAT1", 0, 10, "window_end"), ("SAT2", 10, 20, "task_complete")],
        )
        self.assertEqual(gaps_of(service, "A"), [])
        events = derive_events(
            list(service.store.plans[-1].sessions), service.store.tasks, set(), 0
        )
        handovers = [e for e in events if e.kind.value == "handover"]
        self.assertEqual(len(handovers), 1)
        self.assertEqual(handovers[0].tick, 10)
        self.assertEqual(handovers[0].link_id, "SAT1")
        self.assertEqual(handovers[0].to_link_id, "SAT2")
        self.assertEqual(handovers[0].reason, "window_end")

    def test_min_hold_rejects_too_short_window(self):
        service = make_service(links={"SAT1": [window("SAT1", 0, 2)]})
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 3}
        )
        self.assertEqual(sessions_of(service, "A"), [])
        self.assertEqual(
            gaps_of(service, "A"), [(0, 10, "insufficient_continuous_window")]
        )

    def test_outside_coverage_reason(self):
        far = {"lat": 50.0, "lon": 160.0, "radius_km": 50}
        service = make_service(
            links={"SAT1": [window("SAT1", 0, 10, footprint=far)]}
        )
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 1}
        )
        self.assertEqual(gaps_of(service, "A"), [(0, 10, "outside_coverage")])

    def test_no_eligible_link_reason(self):
        service = make_service(links={})
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 1}
        )
        self.assertEqual(gaps_of(service, "A"), [(0, 10, "no_eligible_link")])


class CapacityAndExclusionTests(unittest.TestCase):
    def test_capacity_forces_lower_priority_to_wait(self):
        service = make_service(
            links={"SAT1": [window("SAT1", 0, 20, capacity=1)]},
            vessels=["V1", "V2"],
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
                {"terminal_id": "T2", "vessel_id": "V2", "bands": ["ku"], "cooldown": 0},
            ],
        )
        service.submit_task(
            {"task_id": "HI", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 1}
        )
        service.submit_task(
            {"task_id": "LO", "vessel_id": "V2", "priority": 5, "start": 0, "end": 20, "min_hold": 1}
        )
        self.assertEqual(sessions_of(service, "HI"), [("SAT1", 0, 10, "task_complete")])
        self.assertEqual(
            sessions_of(service, "LO"),
            [("SAT1", 10, 20, "task_complete")],
        )
        self.assertEqual(gaps_of(service, "LO"), [(0, 10, "capacity_exhausted")])

    def test_exclusion_group_blocks_simultaneous_use(self):
        service = make_service(
            links={
                "SAT1": [window("SAT1", 0, 10)],
                "SAT2": [window("SAT2", 0, 10)],
            },
            vessels=["V1", "V2"],
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
                {"terminal_id": "T2", "vessel_id": "V2", "bands": ["ku"], "cooldown": 0},
            ],
            exclusions=[{"group_id": "G1", "link_ids": ["SAT1", "SAT2"], "limit": 1}],
        )
        service.submit_task(
            {"task_id": "HI", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 1}
        )
        service.submit_task(
            {"task_id": "LO", "vessel_id": "V2", "priority": 5, "start": 0, "end": 10, "min_hold": 1}
        )
        hi_sessions = sessions_of(service, "HI")
        self.assertEqual(len(hi_sessions), 1)
        self.assertEqual(gaps_of(service, "LO"), [(0, 10, "capacity_exhausted")])

    def test_exclusion_as_sole_blocker_reports_exclusion_reason(self):
        # 链路容量充足（2），但互斥组限 1：唯一阻塞是互斥关系
        service = make_service(
            links={"SAT1": [window("SAT1", 0, 10, capacity=2)]},
            vessels=["V1", "V2"],
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
                {"terminal_id": "T2", "vessel_id": "V2", "bands": ["ku"], "cooldown": 0},
            ],
            exclusions=[{"group_id": "G1", "link_ids": ["SAT1"], "limit": 1}],
        )
        service.submit_task(
            {"task_id": "HI", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 1}
        )
        service.submit_task(
            {"task_id": "LO", "vessel_id": "V2", "priority": 5, "start": 0, "end": 10, "min_hold": 1}
        )
        self.assertEqual(gaps_of(service, "LO"), [(0, 10, "exclusion_blocked")])


class CooldownTests(unittest.TestCase):
    def test_cooldown_blocks_other_task_but_not_handover(self):
        service = make_service(
            links={"SAT1": [window("SAT1", 0, 30)]},
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 5}
            ],
        )
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 1}
        )
        service.submit_task(
            {"task_id": "B", "vessel_id": "V1", "priority": 5, "start": 12, "end": 20, "min_hold": 1}
        )
        # A 在 10 释放终端，冷却 5 -> B 只能等到 15
        self.assertEqual(sessions_of(service, "B"), [("SAT1", 15, 20, "task_complete")])
        self.assertEqual(gaps_of(service, "B"), [(12, 15, "terminal_unavailable")])


class PreemptionTests(unittest.TestCase):
    def test_higher_priority_preempts_unconfirmed(self):
        service = make_service(
            links={"SAT1": [window("SAT1", 0, 50)]},
            vessels=["V1", "V2"],
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
                {"terminal_id": "T2", "vessel_id": "V2", "bands": ["ku"], "cooldown": 0},
            ],
        )
        service.submit_task(
            {"task_id": "LO", "vessel_id": "V1", "priority": 5, "start": 0, "end": 50, "min_hold": 1}
        )
        service.submit_task(
            {"task_id": "HI", "vessel_id": "V2", "priority": 50, "start": 10, "end": 30, "min_hold": 1}
        )
        self.assertEqual(
            sessions_of(service, "LO"),
            [
                ("SAT1", 0, 10, "preempted_by:HI"),
                ("SAT1", 30, 50, "task_complete"),
            ],
        )
        self.assertEqual(sessions_of(service, "HI"), [("SAT1", 10, 30, "task_complete")])

    def test_confirmed_slot_requires_dual_approval(self):
        service = make_service(
            links={"SAT1": [window("SAT1", 0, 50)]},
            vessels=["V1", "V2"],
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
                {"terminal_id": "T2", "vessel_id": "V2", "bands": ["ku"], "cooldown": 0},
            ],
        )
        service.submit_task(
            {"task_id": "EM", "vessel_id": "V1", "priority": 100, "start": 0, "end": 20, "min_hold": 1}
        )
        service.confirm_task("EM")
        service.submit_task(
            {"task_id": "N", "vessel_id": "V2", "priority": 60, "start": 5, "end": 20, "min_hold": 1}
        )
        # 未获批准：普通任务不得夺走已确认紧急时隙
        self.assertEqual(sessions_of(service, "N"), [])
        self.assertEqual(gaps_of(service, "N"), [(5, 20, "capacity_exhausted")])
        # 一名审批人不够
        service.approve(
            {"approval_id": "AP", "task_id": "EM", "requester_task_id": "N", "approver": "op1"}
        )
        self.assertEqual(sessions_of(service, "N"), [])
        # 双人批准后允许抢占
        service.approve(
            {"approval_id": "AP", "task_id": "EM", "requester_task_id": "N", "approver": "op2"}
        )
        self.assertEqual(sessions_of(service, "N"), [("SAT1", 5, 20, "task_complete")])
        self.assertEqual(
            sessions_of(service, "EM"), [("SAT1", 0, 5, "preempted_by:N")]
        )
        self.assertEqual(gaps_of(service, "EM"), [(5, 20, "capacity_exhausted")])

    def test_preemption_respects_victim_min_hold(self):
        service = make_service(
            links={"SAT1": [window("SAT1", 0, 50)]},
            vessels=["V1", "V2"],
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
                {"terminal_id": "T2", "vessel_id": "V2", "bands": ["ku"], "cooldown": 0},
            ],
        )
        service.submit_task(
            {"task_id": "LO", "vessel_id": "V1", "priority": 5, "start": 0, "end": 50, "min_hold": 8}
        )
        service.submit_task(
            {"task_id": "HI", "vessel_id": "V2", "priority": 50, "start": 5, "end": 10, "min_hold": 1}
        )
        # LO 的会话从 0 开始，min_hold=8 -> 在 5 处截断会违反最短保持，推迟到 8
        lo_sessions = sessions_of(service, "LO")
        self.assertEqual(lo_sessions[0], ("SAT1", 0, 8, "preempted_by:HI"))
        self.assertEqual(sessions_of(service, "HI"), [("SAT1", 8, 10, "task_complete")])


if __name__ == "__main__":
    unittest.main()
