"""JSON 序列化映射：领域对象 <-> 字典。

API 层、模拟器场景加载与文件快照共用同一套映射，保证格式一致。
"""
from __future__ import annotations

from ..domain.models import (
    Approval,
    EventKind,
    EventStatus,
    ExclusionGroup,
    Footprint,
    Gap,
    LinkKind,
    LinkWindow,
    Plan,
    PlanEvent,
    Session,
    Task,
    Terminal,
)


def window_from_dict(data: dict, link_id: str, version: int) -> LinkWindow:
    footprint = data["footprint"]
    start = int(data["start"])
    return LinkWindow(
        window_id=str(data.get("window_id") or f"{link_id}@{start}"),
        link_id=link_id,
        kind=LinkKind(data.get("kind", LinkKind.SATELLITE.value)),
        band=str(data["band"]),
        start=start,
        end=int(data["end"]),
        capacity=int(data.get("capacity", 1)),
        footprint=Footprint(
            lat=float(footprint["lat"]),
            lon=float(footprint["lon"]),
            radius_km=float(footprint["radius_km"]),
        ),
        version=version,
    )


def window_to_dict(window: LinkWindow) -> dict:
    return {
        "window_id": window.window_id,
        "link_id": window.link_id,
        "kind": window.kind.value,
        "band": window.band,
        "start": window.start,
        "end": window.end,
        "capacity": window.capacity,
        "footprint": {
            "lat": window.footprint.lat,
            "lon": window.footprint.lon,
            "radius_km": window.footprint.radius_km,
        },
        "version": window.version,
    }


def terminal_from_dict(data: dict) -> Terminal:
    return Terminal(
        terminal_id=str(data["terminal_id"]),
        vessel_id=str(data["vessel_id"]),
        bands=tuple(str(b) for b in data.get("bands", ())),
        cooldown=int(data.get("cooldown", 0)),
    )


def terminal_to_dict(terminal: Terminal) -> dict:
    return {
        "terminal_id": terminal.terminal_id,
        "vessel_id": terminal.vessel_id,
        "bands": list(terminal.bands),
        "cooldown": terminal.cooldown,
    }


def exclusion_from_dict(data: dict) -> ExclusionGroup:
    return ExclusionGroup(
        group_id=str(data["group_id"]),
        link_ids=tuple(str(x) for x in data.get("link_ids", ())),
        limit=int(data.get("limit", 1)),
    )


def exclusion_to_dict(group: ExclusionGroup) -> dict:
    return {
        "group_id": group.group_id,
        "link_ids": list(group.link_ids),
        "limit": group.limit,
    }


def task_from_dict(data: dict, submitted_at: int = 0) -> Task:
    return Task(
        task_id=str(data["task_id"]),
        vessel_id=str(data["vessel_id"]),
        priority=int(data.get("priority", 0)),
        start=int(data["start"]),
        end=int(data["end"]),
        min_hold=int(data.get("min_hold", 1)),
        submitted_at=int(data.get("submitted_at", submitted_at)),
    )


def task_to_dict(task: Task) -> dict:
    return {
        "task_id": task.task_id,
        "vessel_id": task.vessel_id,
        "priority": task.priority,
        "start": task.start,
        "end": task.end,
        "min_hold": task.min_hold,
        "submitted_at": task.submitted_at,
    }


def session_to_dict(session: Session) -> dict:
    return {
        "task_id": session.task_id,
        "terminal_id": session.terminal_id,
        "link_id": session.link_id,
        "start": session.start,
        "end": session.end,
        "end_reason": session.end_reason,
        "priority": session.priority,
        "locked": session.locked,
    }


def session_from_dict(data: dict) -> Session:
    return Session(
        task_id=str(data["task_id"]),
        terminal_id=str(data["terminal_id"]),
        link_id=str(data["link_id"]),
        start=int(data["start"]),
        end=int(data["end"]),
        end_reason=str(data["end_reason"]),
        priority=int(data.get("priority", 0)),
        locked=bool(data.get("locked", False)),
    )


def gap_to_dict(gap: Gap) -> dict:
    return {
        "task_id": gap.task_id,
        "start": gap.start,
        "end": gap.end,
        "reason": gap.reason,
    }


def gap_from_dict(data: dict) -> Gap:
    return Gap(
        task_id=str(data["task_id"]),
        start=int(data["start"]),
        end=int(data["end"]),
        reason=str(data["reason"]),
    )


def event_to_dict(event: PlanEvent) -> dict:
    return {
        "event_id": event.event_id,
        "tick": event.tick,
        "kind": event.kind.value,
        "task_id": event.task_id,
        "terminal_id": event.terminal_id,
        "link_id": event.link_id,
        "to_link_id": event.to_link_id,
        "reason": event.reason,
        "status": event.status.value,
    }


def plan_to_dict(plan: Plan, events: list[PlanEvent], now: int) -> dict:
    return {
        "version": plan.version,
        "generated_at": plan.generated_at,
        "now": now,
        "events": [event_to_dict(e) for e in events],
        "sessions": [session_to_dict(s) for s in plan.sessions],
        "gaps": [gap_to_dict(g) for g in plan.gaps],
        "notes": list(plan.notes),
    }


def plan_snapshot_to_dict(plan: Plan) -> dict:
    return {
        "version": plan.version,
        "generated_at": plan.generated_at,
        "sessions": [session_to_dict(s) for s in plan.sessions],
        "gaps": [gap_to_dict(g) for g in plan.gaps],
        "notes": list(plan.notes),
    }


def plan_snapshot_from_dict(data: dict) -> Plan:
    return Plan(
        version=int(data["version"]),
        generated_at=int(data["generated_at"]),
        sessions=tuple(session_from_dict(s) for s in data.get("sessions", ())),
        gaps=tuple(gap_from_dict(g) for g in data.get("gaps", ())),
        notes=tuple(str(n) for n in data.get("notes", ())),
    )


def approval_to_dict(approval: Approval) -> dict:
    return {
        "approval_id": approval.approval_id,
        "task_id": approval.task_id,
        "requester_task_id": approval.requester_task_id,
        "decision": approval.decision,
        "approvers": list(approval.approvers),
        "granted": approval.granted,
    }


def approval_from_dict(data: dict) -> Approval:
    return Approval(
        approval_id=str(data["approval_id"]),
        task_id=str(data["task_id"]),
        requester_task_id=str(data.get("requester_task_id", "")),
        decision=str(data.get("decision", "allow_preempt")),
        approvers=[str(a) for a in data.get("approvers", ())],
    )


def event_kind_status_strings() -> dict:
    return {
        "kinds": [k.value for k in EventKind],
        "statuses": [s.value for s in EventStatus],
    }
