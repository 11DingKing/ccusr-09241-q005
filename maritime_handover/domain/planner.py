"""由会话推导计划事件（接入 / 释放 / 交接），并计算计划版本间差异。

事件 id 由内容决定（不含计划版本号），因此未受重算影响的会话在多次
重算之间保持相同的事件 id —— 这是“只重算受影响区段”的可核对依据。
"""
from __future__ import annotations

from .models import EventKind, EventStatus, PlanEvent, Session, Task


def merge_contiguous(sessions: list[Session]) -> list[Session]:
    """合并同一任务、同一终端、同一链路且首尾相接的会话。"""
    merged: list[Session] = []
    for session in sorted(sessions, key=lambda s: (s.task_id, s.start, s.link_id)):
        if (
            merged
            and merged[-1].task_id == session.task_id
            and merged[-1].terminal_id == session.terminal_id
            and merged[-1].link_id == session.link_id
            and merged[-1].end >= session.start
        ):
            last = merged[-1]
            if session.end > last.end:
                merged[-1] = Session(
                    task_id=last.task_id,
                    terminal_id=last.terminal_id,
                    link_id=last.link_id,
                    start=last.start,
                    end=session.end,
                    end_reason=session.end_reason,
                    priority=last.priority,
                    locked=last.locked or session.locked,
                )
            continue
        merged.append(session)
    return merged


def derive_events(
    sessions: list[Session],
    tasks: dict[str, Task],
    confirmed: set[str],
    now: int,
) -> list[PlanEvent]:
    """把会话列表翻译成带时间与原因的接入 / 释放 / 交接事件。"""
    events: list[PlanEvent] = []
    merged = merge_contiguous(sessions)
    by_task: dict[str, list[Session]] = {}
    for session in merged:
        by_task.setdefault(session.task_id, []).append(session)

    def status_of(task_id: str, tick: int) -> EventStatus:
        if tick < now:
            return EventStatus.EXECUTED
        if task_id in confirmed:
            return EventStatus.CONFIRMED
        return EventStatus.PLANNED

    for task_id, task_sessions in by_task.items():
        task_sessions.sort(key=lambda s: (s.start, s.link_id))
        for index, session in enumerate(task_sessions):
            following = (
                task_sessions[index + 1] if index + 1 < len(task_sessions) else None
            )
            if index == 0:
                events.append(
                    PlanEvent(
                        event_id=f"A:{task_id}:{session.link_id}:{session.start}",
                        tick=session.start,
                        kind=EventKind.ACCESS,
                        task_id=task_id,
                        terminal_id=session.terminal_id,
                        link_id=session.link_id,
                        to_link_id=None,
                        reason="schedule",
                        status=status_of(task_id, session.start),
                    )
                )
            seamless = (
                following is not None
                and following.start == session.end
                and (
                    following.link_id != session.link_id
                    or following.terminal_id != session.terminal_id
                )
            )
            if seamless:
                events.append(
                    PlanEvent(
                        event_id=(
                            f"H:{task_id}:{session.link_id}"
                            f"->{following.link_id}:{session.end}"
                        ),
                        tick=session.end,
                        kind=EventKind.HANDOVER,
                        task_id=task_id,
                        terminal_id=session.terminal_id,
                        link_id=session.link_id,
                        to_link_id=following.link_id,
                        reason=session.end_reason,
                        status=status_of(task_id, session.end),
                    )
                )
                continue
            events.append(
                PlanEvent(
                    event_id=f"R:{task_id}:{session.link_id}:{session.end}",
                    tick=session.end,
                    kind=EventKind.RELEASE,
                    task_id=task_id,
                    terminal_id=session.terminal_id,
                    link_id=session.link_id,
                    to_link_id=None,
                    reason=session.end_reason,
                    status=status_of(task_id, session.end),
                )
            )
            if following is not None:
                events.append(
                    PlanEvent(
                        event_id=(
                            f"A:{task_id}:{following.link_id}:{following.start}"
                        ),
                        tick=following.start,
                        kind=EventKind.ACCESS,
                        task_id=task_id,
                        terminal_id=following.terminal_id,
                        link_id=following.link_id,
                        to_link_id=None,
                        reason="schedule",
                        status=status_of(task_id, following.start),
                    )
                )
    kind_order = {EventKind.HANDOVER: 0, EventKind.RELEASE: 0, EventKind.ACCESS: 1}
    events.sort(key=lambda e: (e.tick, kind_order[e.kind], e.event_id))
    return events


def diff_events(
    old_events: list[PlanEvent], new_events: list[PlanEvent]
) -> dict[str, object]:
    """比较两个计划版本的事件集合：新增、取消（消失）与保留数量。"""
    old_by_id = {event.event_id: event for event in old_events}
    new_by_id = {event.event_id: event for event in new_events}
    added = sorted(set(new_by_id) - set(old_by_id))
    removed = sorted(set(old_by_id) - set(new_by_id))
    return {
        "added": added,
        "removed": removed,
        "kept": len(set(old_by_id) & set(new_by_id)),
    }
