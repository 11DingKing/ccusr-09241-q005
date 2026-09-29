"""场景、窗口、计划与审批记录的 JSON 序列化（仅标准库）。"""

from __future__ import annotations

from typing import Any

from ..domain.enums import (
    ApprovalDecision,
    EventType,
    LinkKind,
    Reason,
    SegmentState,
)
from ..domain.models import (
    ApprovalRecord,
    EmergencyLock,
    PlanSegment,
    Resource,
    Task,
    Terminal,
    TimelineEvent,
    Window,
)


class ValidationError(ValueError):
    """输入数据不满足领域约束。"""


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ValidationError(f"{ctx} 缺少字段 {key}")
    return d[key]


def resource_from(d: dict) -> Resource:
    ctx = f"资源 {d.get('id', '?')}"
    return Resource(
        id=str(_require(d, "id", ctx)),
        kind=LinkKind(_require(d, "kind", ctx)),
        name=str(d.get("name") or d["id"]),
        capacity=int(d.get("capacity", 1)),
        mutex_group=d.get("mutex_group"),
    )


def terminal_from(d: dict) -> Terminal:
    ctx = f"终端 {d.get('id', '?')}"
    kinds = d.get("supported_kinds")
    supported = (frozenset(LinkKind(k) for k in kinds)
                 if kinds is not None
                 else frozenset({LinkKind.SATELLITE, LinkKind.TERRESTRIAL}))
    return Terminal(
        id=str(_require(d, "id", ctx)),
        name=str(d.get("name") or d["id"]),
        cooldown=int(d.get("cooldown", 0)),
        supported_kinds=supported,
    )


def task_from(d: dict) -> Task:
    ctx = f"任务 {d.get('id', '?')}"
    return Task(
        id=str(_require(d, "id", ctx)),
        terminal_id=str(_require(d, "terminal_id", ctx)),
        release=int(_require(d, "release", ctx)),
        deadline=int(_require(d, "deadline", ctx)),
        demand=int(_require(d, "demand", ctx)),
        priority=int(d.get("priority", 100)),
        min_hold=int(d.get("min_hold", 1)),
        emergency=bool(d.get("emergency", False)),
        name=str(d.get("name") or d["id"]),
    )


def window_from(d: dict, default_version: int) -> Window:
    ctx = f"窗口 {d.get('window_uid', '?')}"
    return Window(
        window_uid=str(_require(d, "window_uid", ctx)),
        terminal_id=str(_require(d, "terminal_id", ctx)),
        resource_id=str(_require(d, "resource_id", ctx)),
        start=int(_require(d, "start", ctx)),
        end=int(_require(d, "end", ctx)),
        version=int(d.get("version", default_version)),
    )


def segment_from(d: dict) -> PlanSegment:
    return PlanSegment(
        task_id=d["task_id"],
        terminal_id=d["terminal_id"],
        start=int(d["start"]),
        end=int(d["end"]),
        state=SegmentState(d["state"]),
        resource_id=d.get("resource_id"),
        reason=Reason(d["reason"]) if d.get("reason") else None,
        predicted_version=int(d.get("predicted_version", 0)),
    )


def event_from(d: dict) -> TimelineEvent:
    return TimelineEvent(
        time=int(d["time"]),
        event_type=EventType(d["event_type"]),
        task_id=d["task_id"],
        terminal_id=d["terminal_id"],
        resource_id=d.get("resource_id"),
        reason=Reason(d["reason"]),
        detail=d.get("detail", ""),
        event_key=d.get("event_key", ""),
        plan_version=int(d.get("plan_version", 1)),
    )


def lock_from(d: dict) -> EmergencyLock:
    return EmergencyLock(
        task_id=d["task_id"],
        terminal_id=d["terminal_id"],
        resource_id=d["resource_id"],
        start=int(d["start"]),
        end=int(d["end"]),
        mutex_domain=d["mutex_domain"],
    )


def approval_from(d: dict) -> ApprovalRecord:
    return ApprovalRecord(
        request_id=d["request_id"],
        task_id=d["task_id"],
        terminal_id=d["terminal_id"],
        resource_id=d["resource_id"],
        start=int(d["start"]),
        end=int(d["end"]),
        reason=d.get("reason", ""),
        approvers=frozenset(d.get("approvers", ())),
        granted=bool(d.get("granted", False)),
    )


def approval_to_dict(a: ApprovalRecord) -> dict:
    return a.to_dict()


def decision_from(value: str) -> ApprovalDecision:
    return ApprovalDecision(value)
