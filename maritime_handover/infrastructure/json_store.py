"""文件快照持久化：把 MemoryStore 全量状态写入 JSON 文件。

采用“写完临时文件再原子替换”的方式，避免中途崩溃留下半个文件。
"""
from __future__ import annotations

import json
import os
from typing import Optional

from ..domain.models import Approval, Plan
from .memory import MemoryStore
from .serialization import (
    approval_from_dict,
    approval_to_dict,
    exclusion_from_dict,
    exclusion_to_dict,
    plan_snapshot_from_dict,
    plan_snapshot_to_dict,
    task_from_dict,
    task_to_dict,
    terminal_from_dict,
    terminal_to_dict,
    window_from_dict,
    window_to_dict,
)


def store_to_dict(store: MemoryStore) -> dict:
    return {
        "now": store.now,
        "windows": {
            link_id: [window_to_dict(w) for w in windows]
            for link_id, windows in store.windows.items()
        },
        "window_versions": store.window_versions,
        "tracks": {
            vessel_id: [
                {"tick": t, "lat": lat, "lon": lon} for t, lat, lon in points
            ]
            for vessel_id, points in store.tracks.items()
        },
        "track_versions": store.track_versions,
        "terminals": [terminal_to_dict(t) for t in store.terminals.values()],
        "exclusions": [exclusion_to_dict(g) for g in store.exclusions.values()],
        "tasks": [task_to_dict(t) for t in store.tasks.values()],
        "cancelled": sorted(store.cancelled),
        "confirmed": sorted(store.confirmed),
        "approvals": [approval_to_dict(a) for a in store.approvals.values()],
        "idempotency": store.idempotency,
        "plans": [plan_snapshot_to_dict(p) for p in store.plans],
    }


def store_from_dict(data: dict) -> MemoryStore:
    store = MemoryStore()
    store.now = int(data.get("now", 0))
    store.window_versions = {
        str(k): int(v) for k, v in data.get("window_versions", {}).items()
    }
    store.windows = {
        str(link_id): [
            window_from_dict(w, link_id, int(w.get("version", 0))) for w in windows
        ]
        for link_id, windows in data.get("windows", {}).items()
    }
    store.track_versions = {
        str(k): int(v) for k, v in data.get("track_versions", {}).items()
    }
    store.tracks = {
        str(vessel_id): [
            (int(p["tick"]), float(p["lat"]), float(p["lon"])) for p in points
        ]
        for vessel_id, points in data.get("tracks", {}).items()
    }
    store.terminals = {
        t.terminal_id: t
        for t in (terminal_from_dict(d) for d in data.get("terminals", ()))
    }
    store.exclusions = {
        g.group_id: g
        for g in (exclusion_from_dict(d) for d in data.get("exclusions", ()))
    }
    store.tasks = {
        t.task_id: t for t in (task_from_dict(d) for d in data.get("tasks", ()))
    }
    store.cancelled = set(data.get("cancelled", ()))
    store.confirmed = set(data.get("confirmed", ()))
    store.approvals = {
        a.approval_id: a
        for a in (approval_from_dict(d) for d in data.get("approvals", ()))
    }
    store.idempotency = {
        str(k): v for k, v in data.get("idempotency", {}).items()
    }
    store.plans = [plan_snapshot_from_dict(p) for p in data.get("plans", ())]
    return store


class JsonFileStore(MemoryStore):
    """每次变更后把全量状态快照到 JSON 文件的内存仓储。"""

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            loaded = store_from_dict(data)
            self.__dict__.update(loaded.__dict__)
            self.path = path

    def save(self) -> None:
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(store_to_dict(self), handle, ensure_ascii=False, indent=1)
        os.replace(tmp_path, self.path)
