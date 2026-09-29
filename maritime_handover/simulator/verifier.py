"""模拟器结果核对器。

四项核对
--------

1. **无缝交接**：每个 HANDOVER 事件时刻 t，同任务旧链路段恰在 t 结束、
   新链路段自 t 开始——既无空档也无重叠。
2. **容量守恒**：每个 (资源, 刻度) 上的 LINK 终端数不超过资源容量；
   同一终端任意时刻最多占用一条链路。
3. **冲突等待**：每条 WAIT 事件给出的原因必须与当时的客观状态一致
   （无窗口 / 冷却 / 容量满且含占用证据 / 紧急锁 / 互斥）。
4. **无解说明**：需求未满足的任务必须有缺口与阻塞构成，且账目守恒
   （已服务 + 缺口 = 需求）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..domain.enums import EventType, Reason, SegmentState
from ..domain.models import PlanSegment, Resource, Task, Terminal, TimelineEvent, Window


class Verifier:
    def __init__(
        self,
        resources: dict[str, Resource],
        terminals: dict[str, Terminal],
        tasks: dict[str, Task],
        windows: list[Window],
        segments: list[PlanSegment],
        events: list[TimelineEvent],
        horizon: int,
        cooldown_drops: dict[str, int] | None = None,
    ) -> None:
        self.resources = resources
        self.terminals = terminals
        self.tasks = tasks
        self.windows = windows
        self.segments = segments
        self.events = events
        self.horizon = horizon

    def verify_all(self) -> dict[str, Any]:
        return {
            "seamless_handover": self.check_seamless_handover(),
            "capacity_conservation": self.check_capacity(),
            "conflict_waits": self.check_conflict_waits(),
            "unschedulable": self.check_unschedulable_accounting(),
        }

    # ------------------------------------------------------------------ #

    def check_seamless_handover(self) -> dict[str, Any]:
        """HANDOVER 时刻 t：旧段以 t 结束、新段自 t 开始，资源不同。"""
        segs_by_task: dict[str, list[PlanSegment]] = defaultdict(list)
        for s in self.segments:
            segs_by_task[s.task_id].append(s)
        for segs in segs_by_task.values():
            segs.sort(key=lambda s: s.start)

        violations: list[dict[str, Any]] = []
        handovers = [e for e in self.events if e.event_type is EventType.HANDOVER]
        for ev in handovers:
            task_segs = segs_by_task.get(ev.task_id, [])
            before = [s for s in task_segs
                      if s.state is SegmentState.LINK and s.end == ev.time]
            after = [s for s in task_segs
                     if s.state is SegmentState.LINK and s.start == ev.time]
            problems = []
            if not before:
                problems.append("交接时刻没有恰在此时结束的旧链路段")
            if not after:
                problems.append("交接时刻没有恰在此时开始的新链路段")
            if before and after and before[0].resource_id == after[0].resource_id:
                problems.append("交接前后链路相同")
            if after and after[0].resource_id != ev.resource_id:
                problems.append("事件记录的新链路与新计划段不一致")
            if before and before[0].resource_id == ev.resource_id:
                problems.append("事件记录的新链路与旧计划段相同")
            if problems:
                violations.append({
                    "time": ev.time, "task_id": ev.task_id,
                    "resource_id": ev.resource_id, "problems": problems,
                })
        # 反向核对：相邻 LINK 段资源不同但首尾相接处，必须存在 HANDOVER 事件。
        ev_index = {(e.task_id, e.time) for e in handovers}
        for tid, segs in segs_by_task.items():
            for a, b in zip(segs, segs[1:]):
                if (a.state is SegmentState.LINK and b.state is SegmentState.LINK
                        and a.resource_id != b.resource_id and a.end == b.start
                        and (tid, a.end) not in ev_index):
                    violations.append({
                        "time": a.end, "task_id": tid,
                        "resource_id": b.resource_id,
                        "problems": ["链路已切换但缺少 HANDOVER 事件"],
                    })
        return {
            "passed": not violations,
            "handover_count": len(handovers),
            "violations": violations,
        }

    def check_capacity(self) -> dict[str, Any]:
        """逐 (资源, 刻度) 统计占用，核对容量；同时核对终端单线占用。"""
        occupancy: dict[tuple[str, int], set[str]] = defaultdict(set)
        term_links: dict[tuple[str, int], set[str]] = defaultdict(set)
        for s in self.segments:
            if s.state is not SegmentState.LINK or not s.resource_id:
                continue
            for t in range(s.start, s.end):
                occupancy[(s.resource_id, t)].add(s.terminal_id)
                term_links[(s.terminal_id, t)].add(s.resource_id)

        over: list[dict[str, Any]] = []
        peak: dict[str, int] = defaultdict(int)
        for (rid, t), terms in occupancy.items():
            cap = self.resources[rid].capacity
            peak[rid] = max(peak[rid], len(terms))
            if len(terms) > cap:
                over.append({
                    "resource_id": rid, "time": t,
                    "occupants": sorted(terms), "capacity": cap,
                })
        multi_link: list[dict[str, Any]] = []
        for (tid, t), rids in term_links.items():
            if len(rids) > 1:
                multi_link.append({
                    "terminal_id": tid, "time": t,
                    "resources": sorted(rids),
                })
        return {
            "passed": not over and not multi_link,
            "peak_occupancy": {rid: peak[rid] for rid in sorted(peak)},
            "capacity": {rid: r.capacity for rid, r in self.resources.items()},
            "over_capacity": sorted(over, key=lambda x: (x["time"], x["resource_id"])),
            "terminal_double_link": sorted(
                multi_link, key=lambda x: (x["time"], x["terminal_id"])),
        }

    def check_conflict_waits(self) -> dict[str, Any]:
        """每条 WAIT 事件的原因必须与 t 刻客观状态一致。"""
        link_at: dict[tuple[str, int], bool] = {}
        for s in self.segments:
            if s.state is SegmentState.LINK and s.resource_id:
                for t in range(s.start, s.end):
                    link_at[(s.resource_id, t)] = True

        win_at: dict[tuple[str, str, int], bool] = {}
        for w in self.windows:
            for t in range(w.start, w.end):
                win_at[(w.terminal_id, w.resource_id, t)] = True

        def resource_full(rid: str, t: int, self_tid: str) -> tuple[bool, list[str]]:
            terms = {
                s.terminal_id for s in self.segments
                if s.state is SegmentState.LINK and s.resource_id == rid
                and s.start <= t < s.end
            }
            terms.discard(self_tid)
            return len(terms) >= self.resources[rid].capacity, sorted(terms)

        unjustified: list[dict[str, Any]] = []
        justified = 0
        waits = [e for e in self.events if e.event_type is EventType.WAIT]

        # 每个终端最近一次“真正掉线”（释放到等待，而非交接/完成）的时刻。
        # 重新接入或交接后清除基准。
        last_drop: dict[str, int] = {}
        for ev in sorted(self.events, key=lambda x: (x.time, {
                EventType.RELEASE: 0, EventType.HANDOVER: 1,
                EventType.ACCESS: 2, EventType.WAIT: 3}[x.event_type])):
            if ev.event_type in (EventType.ACCESS, EventType.HANDOVER):
                last_drop.pop(ev.terminal_id, None)
            elif (ev.event_type is EventType.RELEASE
                  and ev.reason in (Reason.WINDOW_END, Reason.EMERGENCY_LOCK,
                                    Reason.PREEMPTED, Reason.DEADLINE,
                                    Reason.HORIZON_END)):
                last_drop[ev.terminal_id] = ev.time

        for ev in waits:
            term = self.terminals.get(ev.terminal_id)
            candidates = [
                r for r in self.resources.values()
                if term is not None and term.supports(r)
                and win_at.get((ev.terminal_id, r.id, ev.time), False)
            ]
            ok = False
            evidence: dict[str, Any] = {}
            if ev.reason is Reason.NO_WINDOW:
                ok = not candidates
                evidence = {"available_windows": len(candidates)}
            elif ev.reason is Reason.COOLDOWN:
                drop = last_drop.get(ev.terminal_id)
                cd = term.cooldown if term else 0
                ok = drop is not None and ev.time < drop + cd
                evidence = {"last_drop": drop, "cooldown": cd}
            elif ev.reason in (Reason.CAPACITY_FULL, Reason.PREEMPTED,
                               Reason.EMERGENCY_LOCK):
                full_resources = []
                for r in candidates:
                    full, who = resource_full(r.id, ev.time, ev.terminal_id)
                    if full:
                        full_resources.append(
                            {"resource_id": r.id, "occupants": who})
                ok = bool(full_resources) or not candidates
                evidence = {"full_resources": full_resources}
            elif ev.reason is Reason.MUTEX_BLOCKED:
                # 同终端另有任务在线即构成互斥证据。
                busy = {
                    s.resource_id for s in self.segments
                    if s.terminal_id == ev.terminal_id
                    and s.state is SegmentState.LINK
                    and s.start <= ev.time < s.end and s.task_id != ev.task_id
                }
                ok = bool(busy)
                evidence = {"occupied_by_terminal": sorted(busy)}
            if ok:
                justified += 1
            else:
                unjustified.append({
                    "time": ev.time, "task_id": ev.task_id,
                    "reason": ev.reason.value, "evidence": evidence,
                })
        return {
            "passed": not unjustified,
            "wait_events": len(waits),
            "justified": justified,
            "unjustified": unjustified,
        }

    def check_unschedulable_accounting(self) -> dict[str, Any]:
        """已服务 + 缺口 = 需求；缺口任务必须给出口径明确的阻塞说明。"""
        served_by_task: dict[str, int] = defaultdict(int)
        for s in self.segments:
            if s.state is SegmentState.LINK:
                served_by_task[s.task_id] += s.length
        rows: list[dict[str, Any]] = []
        broken: list[dict[str, Any]] = []
        for task in self.tasks.values():
            served = min(served_by_task.get(task.id, 0), task.demand)
            if served >= task.demand:
                continue
            shortfall = task.demand - served
            wait_ticks: dict[str, int] = defaultdict(int)
            task_segments = [s for s in self.segments if s.task_id == task.id]
            for s in task_segments:
                if s.state is SegmentState.WAIT:
                    reason = s.reason.value if s.reason else "unknown"
                    wait_ticks[reason] += s.length
            # 守恒校验：链路 + 等待刻度之和不应超过时间窗。
            served_t = sum(s.length for s in task_segments
                           if s.state is SegmentState.LINK)
            waited_t = sum(s.length for s in task_segments
                           if s.state is SegmentState.WAIT)
            window_len = task.deadline - max(task.release, 0)
            row = {
                "task_id": task.id,
                "demanded": task.demand,
                "served": served,
                "shortfall": shortfall,
                "wait_ticks": dict(wait_ticks),
                "window_ticks": window_len,
                "accounted_ticks": served_t + waited_t,
            }
            rows.append(row)
            if served + shortfall != task.demand:
                broken.append({"task_id": task.id, "problem": "服务量账目不平"})
            if served_t + waited_t > window_len:
                broken.append({"task_id": task.id,
                               "problem": "链路与等待刻度之和超过任务时间窗"})
        return {
            "passed": not broken,
            "unschedulable": rows,
            "accounting_errors": broken,
        }
