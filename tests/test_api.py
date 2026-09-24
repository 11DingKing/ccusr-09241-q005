"""本地 JSON API 测试：在随机端口起服务，用 http.client 走完整流程。"""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from maritime_handover.interfaces.api import Router, build_service, make_handler


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service = build_service()
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(Router(service))
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_full_planner_flow(self):
        # 健康检查
        status, data = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

        # 接入链路窗口（版本化）
        status, data = self.request(
            "POST",
            "/v1/links/SAT1/windows",
            {
                "version": 1,
                "windows": [
                    {
                        "kind": "satellite",
                        "band": "ku",
                        "start": 0,
                        "end": 30,
                        "capacity": 1,
                        "footprint": {"lat": 10.0, "lon": 100.0, "radius_km": 500},
                    }
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "accepted")
        # 同版本重放幂等
        status, data2 = self.request(
            "POST",
            "/v1/links/SAT1/windows",
            {
                "version": 1,
                "windows": [
                    {
                        "kind": "satellite",
                        "band": "ku",
                        "start": 0,
                        "end": 30,
                        "capacity": 1,
                        "footprint": {"lat": 10.0, "lon": 100.0, "radius_km": 500},
                    }
                ],
            },
        )
        self.assertEqual(data2["status"], "duplicate_ignored")

        # 船位、终端、任务
        self.request(
            "POST",
            "/v1/vessels/V1/tracks",
            {
                "version": 1,
                "points": [
                    {"tick": 0, "lat": 10.0, "lon": 100.0},
                    {"tick": 100, "lat": 10.0, "lon": 100.0},
                ],
            },
        )
        self.request(
            "PUT",
            "/v1/terminals/T1",
            {"vessel_id": "V1", "bands": ["ku"], "cooldown": 0},
        )
        status, data = self.request(
            "POST",
            "/v1/tasks",
            {
                "task_id": "A",
                "vessel_id": "V1",
                "priority": 10,
                "start": 0,
                "end": 20,
                "min_hold": 1,
                "request_id": "api-req-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "accepted")
        # 相同 request_id 重放
        _, replay = self.request(
            "POST",
            "/v1/tasks",
            {
                "task_id": "A",
                "vessel_id": "V1",
                "priority": 10,
                "start": 0,
                "end": 20,
                "min_hold": 1,
                "request_id": "api-req-1",
            },
        )
        self.assertTrue(replay["idempotent_replay"])

        # 计划视图：事件带时间与原因
        status, plan = self.request("GET", "/v1/plans/current")
        self.assertEqual(status, 200)
        kinds = [e["kind"] for e in plan["events"]]
        self.assertEqual(kinds, ["access", "release"])
        self.assertEqual(plan["events"][0]["reason"], "schedule")
        self.assertEqual(plan["events"][1]["reason"], "task_complete")

        # 任务视图
        status, schedule = self.request("GET", "/v1/tasks/A/schedule")
        self.assertEqual(status, 200)
        self.assertEqual(len(schedule["sessions"]), 1)

        # 时间推进与状态
        status, data = self.request("POST", "/v1/time", {"now": 10})
        self.assertEqual(status, 200)
        status, plan = self.request("GET", "/v1/plans/current")
        self.assertEqual(plan["events"][0]["status"], "executed")

        # 历史版本可查
        status, old = self.request("GET", "/v1/plans/1")
        self.assertEqual(status, 200)

        # 状态汇总
        status, summary = self.request("GET", "/v1/state/summary")
        self.assertEqual(status, 200)
        self.assertIn("SAT1", summary["links"])

    def test_error_responses(self):
        status, data = self.request("GET", "/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")

        status, data = self.request("GET", "/v1/tasks/GHOST/schedule")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "task_not_found")

        status, data = self.request("POST", "/v1/time", {"now": -999})
        self.assertEqual(status, 409)

        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/v1/tasks", body="{not json", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(data["error"]["code"], "bad_json")


if __name__ == "__main__":
    unittest.main()
