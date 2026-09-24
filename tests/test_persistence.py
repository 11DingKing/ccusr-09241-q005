"""文件快照持久化测试：状态可落盘并在恢复后继续工作。"""
import os
import tempfile
import unittest

from maritime_handover.application.services import HandoverService
from maritime_handover.infrastructure.json_store import JsonFileStore
from tests.support import sessions_of, window


class JsonFileStoreTests(unittest.TestCase):
    def test_state_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            service = HandoverService(JsonFileStore(path))
            service.put_track(
                "V1",
                {
                    "version": 1,
                    "points": [
                        {"tick": 0, "lat": 10.0, "lon": 100.0},
                        {"tick": 100, "lat": 10.0, "lon": 100.0},
                    ],
                },
            )
            service.put_terminal(
                {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0}
            )
            service.put_link_windows(
                "SAT1", {"version": 1, "windows": [window("SAT1", 0, 50)]}
            )
            service.submit_task(
                {
                    "task_id": "A",
                    "vessel_id": "V1",
                    "priority": 10,
                    "start": 0,
                    "end": 20,
                    "min_hold": 1,
                },
                request_id="req-persist",
            )
            service.confirm_task("A")
            service.advance_time(5)
            self.assertTrue(os.path.exists(path))

            # 重新载入：计划、确认状态、幂等记录都在
            restored = HandoverService(JsonFileStore(path))
            self.assertEqual(
                sessions_of(restored, "A"), [("SAT1", 0, 20, "task_complete")]
            )
            self.assertIn("A", restored.store.confirmed)
            self.assertEqual(restored.store.now, 5)
            replay = restored.submit_task(
                {
                    "task_id": "A",
                    "vessel_id": "V1",
                    "priority": 10,
                    "start": 0,
                    "end": 20,
                    "min_hold": 1,
                },
                request_id="req-persist",
            )
            self.assertTrue(replay["idempotent_replay"])
            # 恢复后窗口版本继续生效：旧版本 ingest 被忽略
            stale = restored.put_link_windows(
                "SAT1", {"version": 1, "windows": [window("SAT1", 0, 50)]}
            )
            self.assertEqual(stale["status"], "duplicate_ignored")


if __name__ == "__main__":
    unittest.main()
