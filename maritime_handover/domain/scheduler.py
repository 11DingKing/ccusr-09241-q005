"""贪心离散时间调度器。

输入：待排任务集合、固定会话（已执行 / 已确认 / 未受影响保留）与当前时刻。
输出：全量活动会话（含被截断的固定会话）、未覆盖区段、被抢占任务集合。

调度规则：
- 任务按优先级降序、需求起点升序处理；
- 每个任务逐 tick 寻找可用（终端, 链路）组合，优先最长连续窗口以减少交接；
- 一段会话不得短于任务的最短保持时间 min_hold；
- 容量 / 互斥被占时，高优先级任务可抢占未确认会话；已确认紧急时隙
  只有在获得双人批准（approved 集合）后才可被抢占；
- 终端切换冷却期内不得再次接入，同任务同 tick 的计划内交接除外。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Optional

from .constraints import ConstraintChecker, Occupancy
from .geometry import haversine_km
from .models import ExclusionGroup, Session, SessionEnd, Task, Terminal

#: 抢占导致的级联重排上限，防止异常输入下无限循环。
MAX_QUEUE_ITERATIONS = 64

#: 阻塞原因码 -> 会话结束原因。
BLOCKER_END_REASON = {
    "window_closed": SessionEnd.WINDOW_END.value,
    "band_unsupported": SessionEnd.WINDOW_END.value,
    "track_unknown": SessionEnd.COVERAGE_LOST.value,
    "outside_footprint": SessionEnd.COVERAGE_LOST.value,
    "capacity_full": SessionEnd.CAPACITY_BLOCKED.value,
    "exclusion_blocked": SessionEnd.EXCLUSION_BLOCKED.value,
    "terminal_busy": SessionEnd.TERMINAL_BLOCKED.value,
    "terminal_cooldown": SessionEnd.TERMINAL_BLOCKED.value,
}


@dataclass
class ScheduleOutput:
    sessions: list[Session] = field(default_factory=list)
    gaps: list[tuple[str, int, int, str]] = field(default_factory=list)
    preempted_task_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        *,
        checker: ConstraintChecker,
        terminals: dict[str, Terminal],
        groups: list[ExclusionGroup],
        tasks: dict[str, Task],
        now: int,
        approved: set[tuple[str, str]],
        confirmed: set[str],
        preemption_history: set[tuple[str, str]],
    ):
        self.checker = checker
        self.terminals = terminals
        self.tasks = tasks
        self.now = now
        # 已生效的双人批准：(被抢占任务, 请求方任务) 对
        self.approved = approved
        self.confirmed = confirmed
        self.preemption_history = preemption_history
        self.groups_by_link: dict[str, list[ExclusionGroup]] = {}
        for group in groups:
            for link_id in group.link_ids:
                self.groups_by_link.setdefault(link_id, []).append(group)
        self.terminals_by_vessel: dict[str, list[Terminal]] = {}
        for terminal in sorted(terminals.values(), key=lambda t: t.terminal_id):
            self.terminals_by_vessel.setdefault(terminal.vessel_id, []).append(terminal)

    # ---- 主入口 -------------------------------------------------------

    def schedule(
        self, tasks_to_schedule: list[Task], fixed_sessions: list[Session]
    ) -> ScheduleOutput:
        # 占用账本作为实例状态：_schedule_task 采纳试探性抢占的分叉账本时
        # 会整体替换 self._occ，主流程始终使用最新账本。
        self._occ = Occupancy(self.groups_by_link)
        for session in fixed_sessions:
            self._occ.add(session, origin="fixed")
        out = ScheduleOutput()
        gaps_by_task: dict[str, list[tuple[str, int, int, str]]] = {}

        queue: list[tuple[int, int, str, Task]] = []
        for task in tasks_to_schedule:
            heapq.heappush(queue, (-task.priority, task.start, task.task_id, task))
        schedulable = {t.task_id for t in tasks_to_schedule}
        processed: set[str] = set()
        iterations = 0

        while queue:
            iterations += 1
            if iterations > MAX_QUEUE_ITERATIONS:
                out.notes.append("scheduler_queue_cap_reached")
                break
            _, _, _, task = heapq.heappop(queue)
            if task.task_id in processed:
                self._occ.remove_task_sessions(task.task_id)
                gaps_by_task.pop(task.task_id, None)
            processed.add(task.task_id)
            cuts_before = set(self._occ.cuts)
            gaps = self._schedule_task(task)
            gaps_by_task.setdefault(task.task_id, []).extend(gaps)
            # 本轮新产生的截断：若受害者属于待排集合且已处理过，重新入队
            for index in set(self._occ.cuts) - cuts_before:
                victim_id = self._occ.sessions[index].task_id
                if victim_id in schedulable and victim_id in processed:
                    processed.discard(victim_id)
                    victim = self.tasks[victim_id]
                    heapq.heappush(
                        queue, (-victim.priority, victim.start, victim_id, victim)
                    )

        occ = self._occ
        out.sessions = occ.final_sessions()
        out.gaps = [gap for gaps in gaps_by_task.values() for gap in gaps]
        out.preempted_task_ids = sorted(
            {occ.sessions[i].task_id for i in occ.cuts}
        )
        return out

    # ---- 单任务调度 ----------------------------------------------------

    def _schedule_task(self, task: Task) -> list[tuple[str, int, int, str]]:
        occ = self._occ
        gaps: list[tuple[str, int, int, str]] = []
        covered = occ.task_intervals(task.task_id)
        tick = max(task.start, self.now)
        gap_start: Optional[int] = None
        gap_reason: Optional[str] = None

        while tick < task.end:
            covered_end = self._covered_end(covered, tick)
            if covered_end is not None:
                if gap_start is not None:
                    gaps.append(
                        (task.task_id, gap_start, tick, gap_reason or "unknown")
                    )
                    gap_start, gap_reason = None, None
                tick = covered_end
                continue
            choice = self._choose(task, tick, occ)
            if choice is None:
                reason = self._stuck_reason(task, tick, occ)
                if gap_start is None:
                    gap_start, gap_reason = tick, reason
                tick += 1
                continue
            if gap_start is not None:
                gaps.append((task.task_id, gap_start, tick, gap_reason or "unknown"))
                gap_start, gap_reason = None, None
            terminal, link_id, run_end, end_reason, occ = choice
            session = Session(
                task_id=task.task_id,
                terminal_id=terminal.terminal_id,
                link_id=link_id,
                start=tick,
                end=run_end,
                end_reason=end_reason,
                priority=task.priority,
                locked=task.task_id in self.confirmed,
            )
            occ.add(session, origin=task.task_id)
            tick = run_end
        if gap_start is not None:
            gaps.append((task.task_id, gap_start, task.end, gap_reason or "unknown"))
        self._occ = occ
        return gaps

    @staticmethod
    def _covered_end(
        intervals: list[tuple[int, int]], tick: int
    ) -> Optional[int]:
        for start, end in intervals:
            if start <= tick < end:
                return end
            if start > tick:
                return None
        return None

    # ---- 候选选择 -------------------------------------------------------

    def _choose(
        self, task: Task, tick: int, occ: Occupancy
    ) -> Optional[tuple[Terminal, str, int, str, Occupancy]]:
        prev_hint = self._prev_hint(occ, task.task_id, tick)
        handover_terminal = prev_hint[0] if prev_hint else None
        best: Optional[tuple] = None
        for terminal in self.terminals_by_vessel.get(task.vessel_id, ()):
            ignore_release = terminal.terminal_id == handover_terminal
            if (
                self.checker.terminal_blocker(
                    task, terminal, tick, occ, ignore_release
                )
                is not None
            ):
                continue
            for link_id in sorted(self.checker.windows):
                if (
                    self.checker.link_blocker(task, terminal, link_id, tick, occ)
                    is not None
                ):
                    continue
                run_end, reason = self._extend(
                    task, terminal, link_id, tick, occ, ignore_release
                )
                run = run_end - tick
                if run < task.min_hold:
                    continue
                window = self.checker.window_at(link_id, tick)
                spare = window.capacity - occ.link_load[(link_id, tick)] if window else 0
                continuity = 1 if (terminal.terminal_id, link_id) == prev_hint else 0
                key = (run, continuity, spare, link_id, terminal.terminal_id)
                if best is None or key > best[0]:
                    best = (key, terminal, link_id, run_end, reason, occ)
        if best is not None:
            _, terminal, link_id, run_end, reason, used_occ = best
            return terminal, link_id, run_end, reason, used_occ
        return self._choose_with_preemption(task, tick, occ, prev_hint)

    def _choose_with_preemption(
        self,
        task: Task,
        tick: int,
        occ: Occupancy,
        prev_hint: Optional[tuple[str, str]],
    ) -> Optional[tuple[Terminal, str, int, str, Occupancy]]:
        handover_terminal = prev_hint[0] if prev_hint else None
        best: Optional[tuple] = None
        for terminal in self.terminals_by_vessel.get(task.vessel_id, ()):
            ignore_release = terminal.terminal_id == handover_terminal
            if (
                self.checker.terminal_blocker(
                    task, terminal, tick, occ, ignore_release
                )
                is not None
            ):
                continue
            for link_id in sorted(self.checker.windows):
                blocker = self.checker.link_blocker(task, terminal, link_id, tick, occ)
                if blocker not in (None, "capacity_full", "exclusion_blocked"):
                    continue
                fork = occ.fork()
                run_end, reason, victims = self._extend_with_preemption(
                    task, terminal, link_id, tick, fork, ignore_release
                )
                run = run_end - tick
                if run < task.min_hold:
                    continue
                continuity = 1 if (terminal.terminal_id, link_id) == prev_hint else 0
                key = (run, continuity, -len(victims), link_id, terminal.terminal_id)
                if best is None or key > best[0]:
                    best = (key, terminal, link_id, run_end, reason, fork, victims)
        if best is None:
            return None
        _, terminal, link_id, run_end, reason, fork, victims = best
        for victim_id, was_locked in victims:
            self.preemption_history.add((task.task_id, victim_id))
            if was_locked:
                # 经批准让出的紧急时隙：同一轮重算内阻止原任务立即抢回
                self.preemption_history.add((victim_id, task.task_id))
        return terminal, link_id, run_end, reason, fork

    def _extend(
        self,
        task: Task,
        terminal: Terminal,
        link_id: str,
        start: int,
        occ: Occupancy,
        ignore_release: bool = False,
    ) -> tuple[int, str]:
        tick = start
        while tick < task.end:
            blocker = self._blocker(task, terminal, link_id, tick, occ, ignore_release)
            if blocker is not None:
                return tick, BLOCKER_END_REASON[blocker]
            tick += 1
        return tick, SessionEnd.TASK_COMPLETE.value

    def _extend_with_preemption(
        self,
        task: Task,
        terminal: Terminal,
        link_id: str,
        start: int,
        occ: Occupancy,
        ignore_release: bool = False,
    ) -> tuple[int, str, list[tuple[str, bool]]]:
        victims: list[tuple[str, bool]] = []
        tick = start
        while tick < task.end:
            blocker = self._blocker(task, terminal, link_id, tick, occ, ignore_release)
            if blocker is None:
                tick += 1
                continue
            if blocker in ("capacity_full", "exclusion_blocked"):
                victim = self._find_victim(task, link_id, tick, occ, blocker)
                if victim is not None:
                    index, victim_session = victim
                    occ.cut(
                        index,
                        tick,
                        f"{SessionEnd.PREEMPTED.value}_by:{task.task_id}",
                    )
                    victims.append((victim_session.task_id, victim_session.locked))
                    continue
            return tick, BLOCKER_END_REASON[blocker], victims
        return tick, SessionEnd.TASK_COMPLETE.value, victims

    def _blocker(
        self,
        task: Task,
        terminal: Terminal,
        link_id: str,
        tick: int,
        occ: Occupancy,
        ignore_release: bool = False,
    ) -> Optional[str]:
        blocker = self.checker.terminal_blocker(
            task, terminal, tick, occ, ignore_release
        )
        if blocker is not None:
            return blocker
        return self.checker.link_blocker(task, terminal, link_id, tick, occ)

    def _find_victim(
        self, task: Task, link_id: str, tick: int, occ: Occupancy, blocker: str
    ) -> Optional[tuple[int, Session]]:
        candidates: list[tuple[int, Session]] = []
        if blocker == "capacity_full":
            candidates = occ.sessions_at(link_id, tick)
        else:
            for group in self.groups_by_link.get(link_id, ()):
                if occ.group_load[(group.group_id, tick)] >= group.limit:
                    candidates.extend(occ.group_sessions_at(group, tick))
        best: Optional[tuple[int, Session]] = None
        for index, session in candidates:
            if session.task_id == task.task_id:
                continue
            if (task.task_id, session.task_id) in self.preemption_history:
                continue
            victim_task = self.tasks.get(session.task_id)
            if victim_task is None:
                continue
            if tick - session.start < victim_task.min_hold:
                continue  # 不得破坏受害者的最短保持时间
            if session.locked:
                if (session.task_id, task.task_id) not in self.approved:
                    continue  # 已确认紧急时隙：无双人批准不得抢占
            elif session.priority >= task.priority:
                continue  # 未确认会话只能被更高优先级抢占
            key = (session.priority, session.start, session.task_id)
            if best is None or key < best[0]:
                best = (key, index, session)
        if best is None:
            return None
        return best[1], best[2]

    # ---- 辅助 -----------------------------------------------------------

    def _prev_hint(
        self, occ: Occupancy, task_id: str, tick: int
    ) -> Optional[tuple[str, str]]:
        for index, session in enumerate(occ.sessions):
            if index in occ.removed or session.task_id != task_id:
                continue
            if occ.effective_end(index) == tick:
                return (session.terminal_id, session.link_id)
        return None

    def _stuck_reason(self, task: Task, tick: int, occ: Occupancy) -> str:
        terminals = self.terminals_by_vessel.get(task.vessel_id, ())
        if not terminals:
            return "no_terminal"
        any_window = False
        footprint_ok = False
        terminal_ok = False
        capacity_blocked = False
        exclusion_blocked = False
        for terminal in terminals:
            terminal_blocked = (
                self.checker.terminal_blocker(task, terminal, tick, occ) is not None
            )
            for link_id in sorted(self.checker.windows):
                window = self.checker.window_at(link_id, tick)
                if window is None or window.band not in terminal.bands:
                    continue
                any_window = True
                position = self.checker.position_at(task.vessel_id, tick)
                if position is None:
                    continue
                footprint = window.footprint
                if (
                    haversine_km(
                        position[0], position[1], footprint.lat, footprint.lon
                    )
                    > footprint.radius_km
                ):
                    continue
                footprint_ok = True
                if terminal_blocked:
                    continue
                terminal_ok = True
                blocker = self.checker.link_blocker(
                    task, terminal, link_id, tick, occ
                )
                if blocker == "capacity_full":
                    capacity_blocked = True
                elif blocker == "exclusion_blocked":
                    exclusion_blocked = True
        if not any_window:
            return "no_eligible_link"
        if not footprint_ok:
            return "outside_coverage"
        if not terminal_ok:
            return "terminal_unavailable"
        if capacity_blocked:
            return "capacity_exhausted"
        if exclusion_blocked:
            return "exclusion_blocked"
        return "insufficient_continuous_window"
