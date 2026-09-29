"""内存仓储：场景、分版本窗口、计划段/事件、紧急锁、双人批准与幂等键。

线程不安全，面向单进程本地 API 与模拟器；JSON 文件持久化由
``JsonFileRepository`` 在其外层组合。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from ..application.ports import PlanRepository, ScenarioRepository
from ..application.scheduler import diff_windows
from ..domain.enums import EventType
from ..domain.models import (
    ApprovalRecord,
    EmergencyLock,
    PlanSegment,
    TimelineEvent,
    Window,
)

# 同一时刻的事件相位：释放 → 交接 → 接入 → 等待。
_EVENT_PHASE = {
    EventType.RELEASE: 0,
    EventType.HANDOVER: 1,
    EventType.ACCESS: 2,
    EventType.WAIT: 3,
}


class InMemoryRepository(ScenarioRepository, PlanRepository):
    def __init__(self) -> None:
        self._scenario: dict[str, Any] | None = None
        self._windows_by_version: dict[int, list[Window]] = {}
        self._segments: list[PlanSegment] = []
        self._events: dict[str, TimelineEvent] = {}
        self._locks: list[EmergencyLock] = []
        self._approvals: dict[str, ApprovalRecord] = {}
        self._idem: dict[str, Any] = {}
        self._plan_meta: dict[str, Any] = {
            "plan_version": 0,
            "prediction_version": 0,
            "confirmed_at": 0,
        }

    # ------------------------------------------------------------------ #
    # 场景与窗口
    # ------------------------------------------------------------------ #

    def save_scenario(self, resources, terminals, tasks, horizon,
                      group_capacities=None) -> str:
        self._scenario = {
            "resources": list(resources),
            "terminals": list(terminals),
            "tasks": list(tasks),
            "horizon": int(horizon),
            "group_capacities": dict(group_capacities or {}),
        }
        return "scenario"

    def load_scenario(self) -> dict[str, Any]:
        if self._scenario is None:
            raise KeyError("场景尚未建立")
        return deepcopy(self._scenario)

    def put_windows(self, windows: Iterable[Window]) -> dict[str, Any]:
        windows = list(windows)
        if not windows:
            raise ValueError("一版预测至少包含一个窗口")
        version = windows[0].version
        if any(w.version != version for w in windows):
            raise ValueError("同一批次窗口的 version 必须一致")
        if version <= self.latest_version() and self._windows_by_version:
            raise ValueError(
                f"预测版本必须严格递增：已有 {self.latest_version()}，收到 {version}"
            )
        prev = self.list_windows()
        diff = diff_windows(prev, windows)
        self._windows_by_version[version] = list(windows)
        return diff.to_dict()

    def list_windows(self, version: int | None = None) -> list[Window]:
        if not self._windows_by_version:
            return []
        if version is None:
            version = self.latest_version()
        return list(self._windows_by_version.get(version, ()))

    def latest_version(self) -> int:
        return max(self._windows_by_version, default=0)

    # ------------------------------------------------------------------ #
    # 计划段与事件
    # ------------------------------------------------------------------ #

    def save_segments(self, segments, plan_version) -> None:
        self._segments = self._merge(list(segments))

    def list_segments(self, task_id=None) -> list[PlanSegment]:
        out = self._segments
        if task_id is not None:
            out = [s for s in out if s.task_id == task_id]
        return list(out)

    @staticmethod
    def _merge(segs: list[PlanSegment]) -> list[PlanSegment]:
        """合并同任务相邻且状态/资源/原因一致的段（消除 cutoff 接缝）。"""
        segs = sorted(segs, key=lambda s: (s.task_id, s.start, s.end))
        out: list[PlanSegment] = []
        for s in segs:
            if s.end <= s.start:
                continue
            if out:
                p = out[-1]
                if (p.task_id == s.task_id and p.end == s.start
                        and p.state is s.state
                        and p.resource_id == s.resource_id
                        and p.reason is s.reason):
                    out[-1] = PlanSegment(
                        task_id=p.task_id, terminal_id=p.terminal_id,
                        start=p.start, end=s.end, state=p.state,
                        resource_id=p.resource_id, reason=p.reason,
                        predicted_version=max(p.predicted_version,
                                              s.predicted_version),
                    )
                    continue
            out.append(s)
        return out

    def replace_future(self, segments, cutoff, affected_task_ids, plan_version) -> None:
        affected = set(affected_task_ids)
        kept = [
            s for s in self._segments
            if s.task_id not in affected or s.end <= cutoff
        ]
        # 跨 cutoff 的段被整体保留（其未来部分由 fixed 占用日历体现的前提是
        # 该任务不受影响；受影响任务的跨刻度段会在应用层先切分到 cutoff）。
        new_segs = [s for s in segments if s.start >= cutoff and s.end > s.start]
        self._segments = self._merge(kept + new_segs)
        self._segments.sort(key=lambda s: (s.start, s.task_id, s.resource_id or ""))

    def save_events(self, events) -> int:
        added = 0
        for e in events:
            if e.event_key and e.event_key not in self._events:
                self._events[e.event_key] = e
                added += 1
        return added

    def drop_future_events(self, cutoff, affected_task_ids) -> int:
        affected = set(affected_task_ids)
        keys = [
            k for k, e in self._events.items()
            if e.task_id in affected and e.time >= cutoff
        ]
        for k in keys:
            del self._events[k]
        return len(keys)

    def list_events(self) -> list[TimelineEvent]:
        return sorted(
            self._events.values(),
            key=lambda e: (
                e.time,
                _EVENT_PHASE.get(e.event_type, 9),
                e.task_id,
            ),
        )

    def save_locks(self, locks) -> None:
        merged = {
            (l.task_id, l.resource_id, l.start, l.end): l for l in self._locks
        }
        for l in locks:
            merged[(l.task_id, l.resource_id, l.start, l.end)] = l
        self._locks = list(merged.values())

    def replace_locks(self, locks, affected_task_ids, cutoff) -> None:
        affected = set(affected_task_ids)
        kept = [
            l for l in self._locks
            if l.task_id not in affected or l.end <= cutoff
        ]
        new_locks = [l for l in locks if l.start >= cutoff]
        self._locks = kept + new_locks

    def list_locks(self) -> list[EmergencyLock]:
        return list(self._locks)

    # ------------------------------------------------------------------ #
    # 双人批准与幂等
    # ------------------------------------------------------------------ #

    def put_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        prev = self._approvals.get(record.request_id)
        if prev is None:
            self._approvals[record.request_id] = record
            return record
        merged = ApprovalRecord(
            request_id=record.request_id,
            task_id=record.task_id,
            terminal_id=record.terminal_id,
            resource_id=record.resource_id,
            start=record.start,
            end=record.end,
            reason=record.reason,
            approvers=prev.approvers | record.approvers,
            granted=prev.granted or record.granted,
        )
        self._approvals[record.request_id] = merged
        return merged

    def get_approval(self, request_id) -> ApprovalRecord | None:
        return self._approvals.get(request_id)

    def list_approvals(self) -> list[ApprovalRecord]:
        return list(self._approvals.values())

    def seen_idempotency_key(self, key, response) -> bool:
        if key in self._idem:
            return True
        self._idem[key] = deepcopy(response)
        return False

    def stored_response(self, key) -> Any:
        return deepcopy(self._idem.get(key))

    def set_plan_meta(self, meta) -> None:
        self._plan_meta.update(meta)

    def get_plan_meta(self) -> dict[str, Any]:
        return deepcopy(self._plan_meta)
