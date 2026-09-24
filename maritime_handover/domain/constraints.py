"""占用账本与约束评估。

Occupancy 按 tick 记录链路容量、互斥组与终端占用，支持：
- add：放入一段会话；
- cut：在某 tick 截断会话（抢占），容量随之释放；
- remove_task_sessions：撤销某任务本次调度放入的会话（级联重排用）；
- fork：复制账本用于试探性抢占评估。

ConstraintChecker 回答“某任务在某 tick 能否使用某链路/终端”，
不能时返回机器可读的原因码。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Optional

from .geometry import haversine_km, interpolate_track
from .models import ExclusionGroup, LinkWindow, Session, Task, Terminal


class Occupancy:
    """按 tick 的占用账本。"""

    def __init__(self, groups_by_link: dict[str, list[ExclusionGroup]]):
        self.groups_by_link = groups_by_link
        self.link_load: dict[tuple[str, int], int] = defaultdict(int)
        self.group_load: dict[tuple[str, int], int] = defaultdict(int)
        self.terminal_busy: dict[tuple[str, int], str] = {}
        # 终端最近一次“非交接式”释放 -> (释放 tick, 任务 id)，用于冷却判断
        self.terminal_release: dict[str, tuple[int, str]] = {}
        self.sessions: list[Session] = []
        self.origins: list[str] = []  # "fixed" 或本次调度的任务 id
        self.cuts: dict[int, tuple[int, str]] = {}  # 会话下标 -> (截断 tick, 原因)
        self.removed: set[int] = set()

    # ---- 账本维护 -----------------------------------------------------

    def fork(self) -> "Occupancy":
        other = Occupancy(self.groups_by_link)
        other.link_load = defaultdict(int, self.link_load)
        other.group_load = defaultdict(int, self.group_load)
        other.terminal_busy = dict(self.terminal_busy)
        other.terminal_release = dict(self.terminal_release)
        other.sessions = list(self.sessions)
        other.origins = list(self.origins)
        other.cuts = dict(self.cuts)
        other.removed = set(self.removed)
        return other

    def add(self, session: Session, origin: str) -> int:
        index = len(self.sessions)
        self.sessions.append(session)
        self.origins.append(origin)
        for tick in range(session.start, session.end):
            self.link_load[(session.link_id, tick)] += 1
            for group in self.groups_by_link.get(session.link_id, ()):
                self.group_load[(group.group_id, tick)] += 1
            self.terminal_busy[(session.terminal_id, tick)] = session.task_id
        self._recompute_release(session.terminal_id)
        return index

    def cut(self, index: int, at: int, reason: str) -> Session:
        """把会话截断到 at（at 之后释放容量），返回截断后的会话。"""
        session = self.sessions[index]
        effective_end = self.cuts.get(index, (session.end, ""))[0]
        if not session.start < at <= effective_end:
            raise ValueError(f"非法截断点: session={session} at={at}")
        self.cuts[index] = (at, reason)
        for tick in range(at, effective_end):
            self.link_load[(session.link_id, tick)] -= 1
            for group in self.groups_by_link.get(session.link_id, ()):
                self.group_load[(group.group_id, tick)] -= 1
            self.terminal_busy.pop((session.terminal_id, tick), None)
        self._recompute_release(session.terminal_id)
        return replace(session, end=at, end_reason=reason)

    def remove_task_sessions(self, task_id: str) -> None:
        """撤销某任务本次调度放入且未锁定的会话（重排前清理）。"""
        for index, session in enumerate(self.sessions):
            if index in self.removed or index in self.cuts:
                continue
            if self.origins[index] != task_id or session.locked:
                continue
            self.removed.add(index)
            for tick in range(session.start, session.end):
                self.link_load[(session.link_id, tick)] -= 1
                for group in self.groups_by_link.get(session.link_id, ()):
                    self.group_load[(group.group_id, tick)] -= 1
                self.terminal_busy.pop((session.terminal_id, tick), None)
            self._recompute_release(session.terminal_id)

    def _recompute_release(self, terminal_id: str) -> None:
        best: Optional[tuple[int, str]] = None
        for index, session in enumerate(self.sessions):
            if index in self.removed or session.terminal_id != terminal_id:
                continue
            end = self.cuts.get(index, (session.end, ""))[0]
            if best is None or end >= best[0]:
                best = (end, session.task_id)
        if best is None:
            self.terminal_release.pop(terminal_id, None)
        else:
            self.terminal_release[terminal_id] = best

    # ---- 查询 ---------------------------------------------------------

    def effective_end(self, index: int) -> int:
        return self.cuts.get(index, (self.sessions[index].end, ""))[0]

    def sessions_at(self, link_id: str, tick: int) -> list[tuple[int, Session]]:
        return [
            (i, s)
            for i, s in enumerate(self.sessions)
            if i not in self.removed
            and s.link_id == link_id
            and s.start <= tick < self.effective_end(i)
        ]

    def group_sessions_at(
        self, group: ExclusionGroup, tick: int
    ) -> list[tuple[int, Session]]:
        members = set(group.link_ids)
        return [
            (i, s)
            for i, s in enumerate(self.sessions)
            if i not in self.removed
            and s.link_id in members
            and s.start <= tick < self.effective_end(i)
        ]

    def task_intervals(self, task_id: str) -> list[tuple[int, int]]:
        """某任务当前占用（含固定会话、按截断后口径）的合并区间。"""
        spans = sorted(
            (s.start, self.effective_end(i))
            for i, s in enumerate(self.sessions)
            if i not in self.removed and s.task_id == task_id
        )
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(s, e) for s, e in merged]

    def final_sessions(self) -> list[Session]:
        """应用截断、剔除被撤销会话后的最终会话列表。"""
        result = []
        for index, session in enumerate(self.sessions):
            if index in self.removed:
                continue
            if index in self.cuts:
                at, reason = self.cuts[index]
                result.append(replace(session, end=at, end_reason=reason))
            else:
                result.append(session)
        return result


class ConstraintChecker:
    """窗口、覆盖、容量、互斥与终端冷却的逐 tick 判定。"""

    def __init__(
        self,
        windows: dict[str, list[LinkWindow]],
        tracks: dict[str, list[tuple[int, float, float]]],
    ):
        self.windows = windows
        self.tracks = tracks

    def window_at(self, link_id: str, tick: int) -> Optional[LinkWindow]:
        for window in self.windows.get(link_id, ()):
            if window.covers(tick):
                return window
        return None

    def position_at(self, vessel_id: str, tick: int) -> Optional[tuple[float, float]]:
        return interpolate_track(self.tracks.get(vessel_id, ()), tick)

    def link_blocker(
        self,
        task: Task,
        terminal: Terminal,
        link_id: str,
        tick: int,
        occ: Occupancy,
    ) -> Optional[str]:
        """返回 None 表示链路在该 tick 可用，否则返回原因码。"""
        window = self.window_at(link_id, tick)
        if window is None:
            return "window_closed"
        if window.band not in terminal.bands:
            return "band_unsupported"
        position = self.position_at(task.vessel_id, tick)
        if position is None:
            return "track_unknown"
        footprint = window.footprint
        if (
            haversine_km(position[0], position[1], footprint.lat, footprint.lon)
            > footprint.radius_km
        ):
            return "outside_footprint"
        if occ.link_load[(link_id, tick)] >= window.capacity:
            return "capacity_full"
        for group in occ.groups_by_link.get(link_id, ()):
            if occ.group_load[(group.group_id, tick)] >= group.limit:
                return "exclusion_blocked"
        return None

    def terminal_blocker(
        self,
        task: Task,
        terminal: Terminal,
        tick: int,
        occ: Occupancy,
        ignore_same_task_release: bool = False,
    ) -> Optional[str]:
        """返回 None 表示终端在该 tick 可用，否则返回原因码。

        切换冷却：终端上一次非交接释放后 cooldown 个 tick 内不得再次接入；
        同一任务在释放的同一 tick 立即接入（计划内交接）不受冷却限制，
        且该交接会话的整段延续也不再受该释放标记影响
        （ignore_same_task_release 由调度器在交接点传入）。
        """
        if (terminal.terminal_id, tick) in occ.terminal_busy:
            return "terminal_busy"
        release = occ.terminal_release.get(terminal.terminal_id)
        if release is not None:
            released_at, released_task = release
            if released_at <= tick < released_at + terminal.cooldown:
                same_task = released_task == task.task_id
                if same_task and (ignore_same_task_release or released_at == tick):
                    return None
                return "terminal_cooldown"
        return None
