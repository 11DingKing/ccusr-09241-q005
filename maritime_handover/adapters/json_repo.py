"""JSON 文件持久化：把内存仓储的全部状态快照到单个本地 JSON 文件。

运行数据不写入源码目录；路径由使用方（API 启动参数 / 模拟器配置）指定。
"""

from __future__ import annotations

import json
import os
import tempfile

from ..application.serialization import (
    approval_from,
    event_from,
    lock_from,
    resource_from,
    segment_from,
    task_from,
    terminal_from,
)
from ..domain.models import Window
from .memory_repo import InMemoryRepository


class JsonFileRepository(InMemoryRepository):
    """内存仓储 + 显式 ``flush()`` / ``load()`` 的 JSON 快照。"""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        if os.path.exists(path):
            self.load()

    def flush(self) -> None:
        data = {
            "scenario": None
            if self._scenario is None
            else {
                "resources": [r.to_dict() for r in self._scenario["resources"]],
                "terminals": [t.to_dict() for t in self._scenario["terminals"]],
                "tasks": [t.to_dict() for t in self._scenario["tasks"]],
                "horizon": self._scenario["horizon"],
                "group_capacities": self._scenario.get("group_capacities", {}),
            },
            "windows": {
                str(v): [w.to_dict() for w in ws]
                for v, ws in self._windows_by_version.items()
            },
            "segments": [s.to_dict() for s in self._segments],
            "events": [e.to_dict() for e in self._events.values()],
            "locks": [l.to_dict() for l in self._locks],
            "approvals": [a.to_dict() for a in self._approvals.values()],
            "idem": self._idem,
            "plan_meta": self._plan_meta,
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".plan-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("scenario"):
            sc = data["scenario"]
            self.save_scenario(
                [resource_from(d) for d in sc["resources"]],
                [terminal_from(d) for d in sc["terminals"]],
                [task_from(d) for d in sc["tasks"]],
                sc["horizon"],
                sc.get("group_capacities", {}),
            )
        for _version, ws in data.get("windows", {}).items():
            self._windows_by_version[int(_version)] = [
                Window(
                    window_uid=w["window_uid"],
                    terminal_id=w["terminal_id"],
                    resource_id=w["resource_id"],
                    start=w["start"], end=w["end"], version=w["version"],
                )
                for w in ws
            ]
        self._segments = [segment_from(d) for d in data.get("segments", [])]
        self._events = {}
        for d in data.get("events", []):
            e = event_from(d)
            self._events[e.event_key] = e
        self._locks = [lock_from(d) for d in data.get("locks", [])]
        self._approvals = {
            d["request_id"]: approval_from(d) for d in data.get("approvals", [])
        }
        self._idem = data.get("idem", {})
        self._plan_meta = data.get("plan_meta", self._plan_meta)
