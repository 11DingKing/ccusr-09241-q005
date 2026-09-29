"""本地 JSON API 端到端测试（标准库 http.server + urllib）。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from maritime_handover.interfaces.http_api import (
    ApiContext,
    _make_handler,
)
from maritime_handover.adapters.drivers import (
    FixedClock,
    ListObserver,
    SequentialIdGenerator,
)
from maritime_handover.adapters.memory_repo import InMemoryRepository
from maritime_handover.application.planning_service import PlanningService


SCENARIO = {
    "resources": [
        {"id": "SAT1", "kind": "satellite", "name": "波束1", "capacity": 1},
    ],
    "terminals": [
        {"id": "A", "name": "甲船", "cooldown": 0},
    ],
    "tasks": [
        {"id": "T1", "terminal_id": "A", "release": 0, "deadline": 6,
         "demand": 4, "priority": 1},
    ],
    "horizon": 6,
}
PREDICTION = {"version": 1, "windows": [
    {"window_uid": "w1", "terminal_id": "A", "resource_id": "SAT1",
     "start": 0, "end": 6},
]}


class ApiServer:
    def __init__(self) -> None:
        self.repo = InMemoryRepository()
        self.clock = FixedClock(0)
        self.observer = ListObserver()
        service = PlanningService(
            scenarios=self.repo, plans=self.repo, clock=self.clock,
            id_gen=SequentialIdGenerator(), observer=self.observer,
        )
        self.ctx = ApiContext(service, self.clock, self.observer)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         _make_handler(self.ctx))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self) -> "ApiServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path: str, body: dict, idem_key: str | None = None,
             expect_error: bool = False):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if idem_key:
            req.add_header("Idempotency-Key", idem_key)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            if expect_error:
                return exc.code, payload
            raise AssertionError(f"POST {path} 失败: {exc.code} {payload}")

    def get(self, path: str):
        with urllib.request.urlopen(self.url(path)) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))


class HttpApiTests(unittest.TestCase):
    def test_full_flow_and_queries(self) -> None:
        with ApiServer() as srv:
            status, _ = srv.post("/api/scenario", SCENARIO)
            self.assertEqual(status, 201)
            srv.post("/api/predictions", PREDICTION)
            status, plan_resp = srv.post("/api/plans/generate", {})
            self.assertEqual(status, 200)
            self.assertEqual(plan_resp["stats"]["link_ticks"], 4)

            status, health = srv.get("/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(health["prediction_version"], 1)

            status, plan = srv.get("/api/plan")
            self.assertEqual(status, 200)
            self.assertTrue(plan["segments"])
            status, task_plan = srv.get("/api/plans/tasks/T1")
            self.assertTrue(all(e["task_id"] == "T1"
                                for e in task_plan["events"]))

    def test_idempotency_key_replays_first_response(self) -> None:
        with ApiServer() as srv:
            srv.post("/api/scenario", SCENARIO)
            # 同一 Idempotency-Key 提交两次预测：第二次直接回放，
            # 不会产生版本冲突，也不会再次摄入。
            s1, r1 = srv.post("/api/predictions", PREDICTION,
                              idem_key="pred-1")
            s2, r2 = srv.post("/api/predictions", PREDICTION,
                              idem_key="pred-1")
            self.assertEqual(s1, s2)
            self.assertTrue(r2.get("idempotent_replay"))
            # 实际只摄入了一版。
            _, health = srv.get("/api/health")
            self.assertEqual(health["prediction_version"], 1)

    def test_duplicate_prediction_without_idem_key_rejected(self) -> None:
        with ApiServer() as srv:
            srv.post("/api/scenario", SCENARIO)
            srv.post("/api/predictions", PREDICTION)
            status, body = srv.post("/api/predictions", PREDICTION,
                                    expect_error=True)
            self.assertEqual(status, 400)
            self.assertIn("版本", body["error"])

    def test_clock_advance_changes_confirmation_time(self) -> None:
        with ApiServer() as srv:
            srv.post("/api/scenario", SCENARIO)
            srv.post("/api/predictions", PREDICTION)
            srv.post("/api/plans/generate", {})
            _, r = srv.post("/api/clock/advance", {"delta": 3})
            self.assertEqual(r["now"], 3)
            _, health = srv.get("/api/health")
            self.assertEqual(health["now"], 3)

    def test_bad_json_returns_400(self) -> None:
        with ApiServer() as srv:
            req = urllib.request.Request(
                srv.url("/api/scenario"),
                data=b"{not-json", method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req)
                self.fail("应当返回 400")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
