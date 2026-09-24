"""计划核对：无缝交接、容量守恒、冲突等待与无解任务说明。

这些函数同时被模拟器报告与单元测试使用，输入为最终计划的会话集合。
"""
from __future__ import annotations

from .models import ExclusionGroup, Gap, LinkWindow, Session, Task


def merge_intervals(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def subtract_intervals(
    start: int, end: int, covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从 [start, end) 中扣除已覆盖区间，返回未覆盖区段。"""
    gaps: list[tuple[int, int]] = []
    cursor = start
    for span_start, span_end in covered:
        if span_end <= cursor:
            continue
        if span_start > cursor:
            gaps.append((cursor, min(span_start, end)))
        cursor = max(cursor, span_end)
        if cursor >= end:
            return gaps
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def check_seamless(sessions: list[Session], tasks: dict[str, Task]) -> dict:
    """无缝交接核对：每个任务的需求区间是否被会话连续覆盖。"""
    by_task: dict[str, list[Session]] = {}
    for session in sessions:
        by_task.setdefault(session.task_id, []).append(session)
    report_tasks = {}
    all_ok = True
    for task_id, task in sorted(tasks.items()):
        task_sessions = sorted(by_task.get(task_id, []), key=lambda s: s.start)
        covered = merge_intervals([(s.start, s.end) for s in task_sessions])
        gaps = subtract_intervals(task.start, task.end, covered)
        handovers = 0
        for prev, nxt in zip(task_sessions, task_sessions[1:]):
            if prev.end == nxt.start and prev.link_id != nxt.link_id:
                handovers += 1
        ok = not gaps
        all_ok = all_ok and ok
        report_tasks[task_id] = {
            "ok": ok,
            "required": [task.start, task.end],
            "covered": covered,
            "gaps": gaps,
            "handovers": handovers,
        }
    return {"ok": all_ok, "tasks": report_tasks}


def check_capacity(
    sessions: list[Session],
    windows: dict[str, list[LinkWindow]],
    groups: list[ExclusionGroup],
    tick_range: range,
) -> dict:
    """容量守恒核对：任一 tick 链路占用不超过窗口容量、互斥组不超限。"""
    violations = []
    groups_by_link: dict[str, list[ExclusionGroup]] = {}
    for group in groups:
        for link_id in group.link_ids:
            groups_by_link.setdefault(link_id, []).append(group)
    for tick in tick_range:
        link_load: dict[str, int] = {}
        for session in sessions:
            if session.start <= tick < session.end:
                link_load[session.link_id] = link_load.get(session.link_id, 0) + 1
        for link_id, load in sorted(link_load.items()):
            window = next(
                (w for w in windows.get(link_id, ()) if w.covers(tick)), None
            )
            if window is None:
                violations.append(
                    {
                        "tick": tick,
                        "link_id": link_id,
                        "kind": "session_outside_window",
                        "load": load,
                    }
                )
                continue
            if load > window.capacity:
                violations.append(
                    {
                        "tick": tick,
                        "link_id": link_id,
                        "kind": "capacity_exceeded",
                        "load": load,
                        "capacity": window.capacity,
                    }
                )
        for group in groups:
            load = sum(link_load.get(link_id, 0) for link_id in group.link_ids)
            if load > group.limit:
                violations.append(
                    {
                        "tick": tick,
                        "group_id": group.group_id,
                        "kind": "exclusion_exceeded",
                        "load": load,
                        "limit": group.limit,
                    }
                )
    return {"ok": not violations, "violations": violations}


def check_waiting(tasks: dict[str, Task], sessions: list[Session]) -> dict:
    """冲突等待核对：任务从需求起点到首次接入之间的等待时长。"""
    first_access: dict[str, int] = {}
    for session in sessions:
        current = first_access.get(session.task_id)
        if current is None or session.start < current:
            first_access[session.task_id] = session.start
    entries = []
    for task_id, task in sorted(tasks.items()):
        start = first_access.get(task_id)
        waited = None if start is None else max(0, start - task.start)
        entries.append(
            {
                "task_id": task_id,
                "submitted_at": task.submitted_at,
                "required_start": task.start,
                "first_access": start,
                "waited_ticks": waited,
            }
        )
    waited_any = [e for e in entries if e["waited_ticks"]]
    return {"ok": True, "tasks": entries, "waiting_tasks": len(waited_any)}


def check_infeasible(gaps: list[Gap]) -> dict:
    """无解任务说明：汇总每个任务未覆盖区段及原因。"""
    by_task: dict[str, list[Gap]] = {}
    for gap in gaps:
        by_task.setdefault(gap.task_id, []).append(gap)
    entries = [
        {
            "task_id": task_id,
            "uncovered": [[g.start, g.end] for g in sorted(
                task_gaps, key=lambda g: g.start
            )],
            "reasons": sorted({g.reason for g in task_gaps}),
        }
        for task_id, task_gaps in sorted(by_task.items())
    ]
    return {"count": len(entries), "tasks": entries}
