"""应用服务测试：观测版本控制、幂等、增量重算、确认与双人批准。"""
import unittest

from maritime_handover.application.services import DomainError
from maritime_handover.domain.planner import derive_events
from tests.support import make_service, sessions_of, gaps_of, window


class VersioningTests(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_stale_and_duplicate_versions_ignored(self):
        svc = self.service
        r1 = svc.put_link_windows(
            "SAT1", {"version": 2, "windows": [window("SAT1", 0, 10)]}
        )
        self.assertEqual(r1["status"], "accepted")
        r2 = svc.put_link_windows(
            "SAT1", {"version": 2, "windows": [window("SAT1", 0, 10)]}
        )
        self.assertEqual(r2["status"], "duplicate_ignored")
        r3 = svc.put_link_windows(
            "SAT1", {"version": 1, "windows": [window("SAT1", 0, 10)]}
        )
        self.assertEqual(r3["status"], "stale_ignored")
        self.assertEqual(svc.store.window_versions["SAT1"], 2)

    def test_track_versioning(self):
        svc = self.service
        svc.put_track(
            "V1",
            {
                "version": 5,
                "points": [
                    {"tick": 0, "lat": 10.0, "lon": 100.0},
                    {"tick": 100, "lat": 10.0, "lon": 100.0},
                ],
            },
        )
        r = svc.put_track(
            "V1",
            {
                "version": 4,
                "points": [{"tick": 0, "lat": 11.0, "lon": 100.0}],
            },
        )
        self.assertEqual(r["status"], "stale_ignored")

    def test_overlapping_windows_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.put_link_windows(
                "SAT1",
                {
                    "version": 1,
                    "windows": [window("SAT1", 0, 10), window("SAT1", 5, 15)],
                },
            )
        self.assertEqual(ctx.exception.code, "windows_overlap")


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.service = make_service(links={"SAT1": [window("SAT1", 0, 50)]})

    def test_request_id_replay_returns_cached_response(self):
        svc = self.service
        payload = {
            "task_id": "A",
            "vessel_id": "V1",
            "priority": 10,
            "start": 0,
            "end": 10,
            "min_hold": 1,
        }
        r1 = svc.submit_task(payload, request_id="req-1")
        plan_count = len(svc.store.plans)
        r2 = svc.submit_task(payload, request_id="req-1")
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(r1["plan_version"], r2["plan_version"])
        self.assertEqual(len(svc.store.plans), plan_count)
        self.assertEqual(len(svc.store.tasks), 1)

    def test_duplicate_task_payload_idempotent_without_request_id(self):
        svc = self.service
        payload = {
            "task_id": "A",
            "vessel_id": "V1",
            "priority": 10,
            "start": 0,
            "end": 10,
            "min_hold": 1,
        }
        svc.submit_task(payload)
        r = svc.submit_task(payload)
        self.assertEqual(r["status"], "duplicate_ignored")

    def test_conflicting_task_payload_rejected(self):
        svc = self.service
        svc.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 10, "min_hold": 1}
        )
        with self.assertRaises(DomainError) as ctx:
            svc.submit_task(
                {"task_id": "A", "vessel_id": "V1", "priority": 99, "start": 0, "end": 10, "min_hold": 1}
            )
        self.assertEqual(ctx.exception.code, "task_exists")

    def test_duplicate_approval_does_not_count_twice(self):
        svc = self.service
        svc.submit_task(
            {"task_id": "EM", "vessel_id": "V1", "priority": 100, "start": 0, "end": 10, "min_hold": 1}
        )
        svc.submit_task(
            {"task_id": "N", "vessel_id": "V1", "priority": 60, "start": 0, "end": 10, "min_hold": 1}
        )
        svc.confirm_task("EM")
        r1 = svc.approve(
            {"approval_id": "AP", "task_id": "EM", "requester_task_id": "N", "approver": "op1"}
        )
        self.assertEqual(r1["status"], "recorded")
        r2 = svc.approve(
            {"approval_id": "AP", "task_id": "EM", "requester_task_id": "N", "approver": "op1"}
        )
        self.assertEqual(r2["status"], "duplicate_ignored")
        self.assertEqual(len(svc.store.approvals["AP"].approvers), 1)
        r3 = svc.approve(
            {"approval_id": "AP", "task_id": "EM", "requester_task_id": "N", "approver": "op2"}
        )
        self.assertEqual(r3["status"], "granted")
        # 批准生效后再加审批人不改变状态
        r4 = svc.approve(
            {"approval_id": "AP", "task_id": "EM", "requester_task_id": "N", "approver": "op3"}
        )
        self.assertEqual(r4["status"], "granted")

    def test_approval_conflict_on_different_task(self):
        svc = self.service
        svc.submit_task(
            {"task_id": "EM", "vessel_id": "V1", "priority": 100, "start": 0, "end": 10, "min_hold": 1}
        )
        svc.submit_task(
            {"task_id": "N", "vessel_id": "V1", "priority": 60, "start": 0, "end": 10, "min_hold": 1}
        )
        svc.submit_task(
            {"task_id": "M", "vessel_id": "V1", "priority": 60, "start": 0, "end": 10, "min_hold": 1}
        )
        svc.approve(
            {"approval_id": "AP", "task_id": "EM", "requester_task_id": "N", "approver": "op1"}
        )
        with self.assertRaises(DomainError) as ctx:
            svc.approve(
                {"approval_id": "AP", "task_id": "EM", "requester_task_id": "M", "approver": "op2"}
            )
        self.assertEqual(ctx.exception.code, "approval_conflict")

    def test_confirm_is_idempotent(self):
        svc = self.service
        svc.submit_task(
            {"task_id": "EM", "vessel_id": "V1", "priority": 100, "start": 0, "end": 10, "min_hold": 1}
        )
        svc.confirm_task("EM")
        r = svc.confirm_task("EM")
        self.assertEqual(r["status"], "duplicate_ignored")


class IncrementalReplanTests(unittest.TestCase):
    def test_only_affected_segments_recomputed(self):
        # V1 用 SAT1，V2 用 SAT3（覆盖不交叉）；SAT1 窗口缩短只应影响 V1 的任务
        service = make_service(
            links={
                "SAT1": [window("SAT1", 0, 60)],
                "SAT2": [
                    window(
                        "SAT2", 30, 60,
                        footprint={"lat": 10.0, "lon": 100.0, "radius_km": 500},
                    )
                ],
                "SAT3": [
                    window(
                        "SAT3", 0, 60,
                        footprint={"lat": 20.0, "lon": 110.0, "radius_km": 100},
                    )
                ],
            },
            vessels=["V1", "V2"],
            terminals=[
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
                {"terminal_id": "T2", "vessel_id": "V2", "bands": ["ku"], "cooldown": 0},
            ],
            tracks={"V2": (20.0, 110.0)},
        )
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 60, "min_hold": 2}
        )
        service.submit_task(
            {"task_id": "B", "vessel_id": "V2", "priority": 5, "start": 0, "end": 60, "min_hold": 2}
        )
        service.advance_time(20)
        before = derive_events(
            list(service.store.plans[-1].sessions), service.store.tasks, set(), 20
        )
        b_events_before = {e.event_id for e in before if e.task_id == "B"}
        a_events_before = {e.event_id for e in before if e.task_id == "A"}

        # SAT1 窗口从 [0,60) 缩短为 [0,30)
        service.put_link_windows(
            "SAT1", {"version": 2, "windows": [window("SAT1", 0, 30)]}
        )
        after = derive_events(
            list(service.store.plans[-1].sessions), service.store.tasks, set(), 20
        )
        b_events_after = {e.event_id for e in after if e.task_id == "B"}
        a_events_after = {e.event_id for e in after if e.task_id == "A"}

        # 未受影响任务 B 的事件完全不变
        self.assertEqual(b_events_before, b_events_after)
        # 受影响任务 A 重排：30 处交接到 SAT2
        self.assertNotEqual(a_events_before, a_events_after)
        self.assertIn("H:A:SAT1->SAT2:30", a_events_after)
        # A 的已执行接入事件（tick 0 < now=20）保留
        self.assertIn("A:A:SAT1:0", a_events_after)

    def test_new_window_opportunity_fills_previous_gap(self):
        service = make_service(links={"SAT1": [window("SAT1", 0, 10)]})
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 30, "min_hold": 1}
        )
        self.assertEqual(gaps_of(service, "A"), [(10, 30, "no_eligible_link")])
        # 新版本把窗口延长到 30：缺口区段应被重算覆盖
        service.put_link_windows(
            "SAT1", {"version": 2, "windows": [window("SAT1", 0, 30)]}
        )
        self.assertEqual(gaps_of(service, "A"), [])
        self.assertEqual(
            sessions_of(service, "A"), [("SAT1", 0, 30, "task_complete")]
        )

    def test_confirmed_slot_survives_window_shrink_with_note(self):
        service = make_service(links={"SAT1": [window("SAT1", 0, 60)]})
        service.submit_task(
            {"task_id": "EM", "vessel_id": "V1", "priority": 100, "start": 0, "end": 60, "min_hold": 1}
        )
        service.confirm_task("EM")
        service.put_link_windows(
            "SAT1", {"version": 2, "windows": [window("SAT1", 0, 30)]}
        )
        # 已确认时隙不因窗口缩短而被自动移动，但计划备注会指出失效
        self.assertEqual(
            sessions_of(service, "EM"), [("SAT1", 0, 60, "task_complete")]
        )
        notes = service.store.plans[-1].notes
        self.assertTrue(any("locked_session_window_invalid" in n for n in notes))


class LifecycleTests(unittest.TestCase):
    def test_cancel_task_releases_future_sessions(self):
        service = make_service(links={"SAT1": [window("SAT1", 0, 50)]})
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 50, "min_hold": 1}
        )
        service.advance_time(10)
        service.cancel_task("A")
        self.assertEqual(
            sessions_of(service, "A"), [("SAT1", 0, 10, "cancelled")]
        )
        r = service.cancel_task("A")
        self.assertEqual(r["status"], "duplicate_ignored")

    def test_time_regression_rejected(self):
        service = make_service()
        service.advance_time(10)
        with self.assertRaises(DomainError) as ctx:
            service.advance_time(5)
        self.assertEqual(ctx.exception.code, "time_regression")

    def test_event_status_transitions(self):
        service = make_service(links={"SAT1": [window("SAT1", 0, 50)]})
        service.submit_task(
            {"task_id": "A", "vessel_id": "V1", "priority": 10, "start": 0, "end": 50, "min_hold": 1}
        )
        service.advance_time(10)
        view = service.current_plan_view()
        access = next(e for e in view["events"] if e["kind"] == "access")
        release = next(e for e in view["events"] if e["kind"] == "release")
        self.assertEqual(access["status"], "executed")
        self.assertEqual(release["status"], "planned")
        service.confirm_task("A")
        view = service.current_plan_view()
        release = next(e for e in view["events"] if e["kind"] == "release")
        self.assertEqual(release["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
