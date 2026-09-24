"""交接计划应用服务。

职责：
- 接收版本化观测（链路窗口、船位轨迹）与静态资源（终端、互斥组）；
- 接收任务、确认紧急时隙、登记双人批准；
- 触发增量重算：仅重排尚未执行且真正受影响的区段；
- 维护请求级幂等（request_id）与观测版本幂等（同版本重放为无操作）。

所有时间均为整数 tick；服务内部时钟通过 advance_time 推进（本地部署
由 API/模拟器驱动），保证业务过程可稳定复现。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from ..domain.constraints import ConstraintChecker, Occupancy
from ..domain.geometry import interpolate_track
from ..domain.models import (
    INF,
    Approval,
    Gap,
    LinkWindow,
    Plan,
    Session,
    SessionEnd,
    Task,
)
from ..domain.planner import derive_events, diff_events
from ..domain.scheduler import BLOCKER_END_REASON, Scheduler
from ..infrastructure.memory import MemoryStore
from ..infrastructure.serialization import (
    approval_to_dict,
    exclusion_from_dict,
    exclusion_to_dict,
    plan_to_dict,
    task_from_dict,
    task_to_dict,
    terminal_from_dict,
    terminal_to_dict,
    window_from_dict,
)

#: 抢占级联的最大迭代轮数。
MAX_CASCADE = 8


class DomainError(Exception):
    """业务错误：携带 HTTP 友好的状态码与机器可读 code。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _merge_gaps(gaps: list[Gap]) -> list[Gap]:
    """合并同一任务、同一原因且首尾相接的缺口区段。"""
    merged: list[Gap] = []
    for gap in sorted(gaps, key=lambda g: (g.task_id, g.reason, g.start)):
        if (
            merged
            and merged[-1].task_id == gap.task_id
            and merged[-1].reason == gap.reason
            and gap.start <= merged[-1].end
        ):
            last = merged[-1]
            if gap.end > last.end:
                merged[-1] = Gap(last.task_id, last.start, gap.end, last.reason)
            continue
        merged.append(gap)
    return merged


def diff_window_ranges(
    old: list[LinkWindow], new: list[LinkWindow]
) -> list[tuple[int, int]]:
    """逐基本区段比较两个版本的窗口集合，返回发生变化的 tick 区间。"""
    points = sorted({w.start for w in old + new} | {w.end for w in old + new})
    changed: list[tuple[int, int]] = []

    def at(windows: list[LinkWindow], tick: int) -> Optional[LinkWindow]:
        for window in windows:
            if window.covers(tick):
                return window
        return None

    def key(window: Optional[LinkWindow]):
        if window is None:
            return None
        return (window.capacity, window.band, window.footprint, window.kind)

    for start, end in zip(points, points[1:]):
        if start == end:
            continue
        if key(at(old, start)) != key(at(new, start)):
            changed.append((start, end))
    return _merge_ranges(changed)


def diff_track_ranges(
    old: list[tuple[int, float, float]], new: list[tuple[int, float, float]]
) -> list[tuple[int, int]]:
    """比较两个版本的轨迹，返回插值位置发生变化的 tick 区间。"""
    points = sorted({p[0] for p in old} | {p[0] for p in new})
    if not points:
        return []
    changed: list[tuple[int, int]] = []
    for start, end in zip(points, points[1:]):
        if start == end:
            continue
        if interpolate_track(old, start) != interpolate_track(
            new, start
        ) or interpolate_track(old, end) != interpolate_track(new, end):
            changed.append((start, end))
    return _merge_ranges(changed)


def normalize_windows(windows: list[LinkWindow]) -> list[LinkWindow]:
    """排序并合并相邻且属性一致的窗口；属性不同的重叠窗口视为非法。"""
    ordered = sorted(windows, key=lambda w: (w.start, w.end, w.window_id))
    merged: list[LinkWindow] = []
    for window in ordered:
        if window.end <= window.start:
            raise DomainError("invalid_window", f"窗口 {window.window_id} 区间为空")
        if window.capacity < 1:
            raise DomainError("invalid_window", f"窗口 {window.window_id} 容量非法")
        if merged:
            last = merged[-1]
            if window.start < last.end:
                raise DomainError(
                    "windows_overlap",
                    f"窗口 {last.window_id} 与 {window.window_id} 重叠",
                    status=409,
                )
            same_attrs = (
                last.band == window.band
                and last.capacity == window.capacity
                and last.footprint == window.footprint
                and last.kind == window.kind
            )
            if same_attrs and last.end == window.start:
                merged[-1] = replace(window, start=last.start)
                continue
        merged.append(window)
    return merged


class HandoverService:
    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        clock: Optional[Callable[[], int]] = None,
    ):
        self.store = store or MemoryStore()
        self._clock = clock

    # ---- 幂等包装 -------------------------------------------------------

    def _idempotent(
        self, request_id: Optional[str], fn: Callable[[], dict]
    ) -> dict:
        if request_id is not None and request_id in self.store.idempotency:
            cached = self.store.idempotency[request_id]
            return {**cached, "idempotent_replay": True}
        response = fn()
        if request_id is not None:
            self.store.idempotency[request_id] = response
        self.store.save()
        return response

    # ---- 观测接入 -------------------------------------------------------

    def put_link_windows(
        self,
        link_id: str,
        payload: dict,
        request_id: Optional[str] = None,
    ) -> dict:
        def apply() -> dict:
            version = int(payload["version"])
            current = self.store.window_versions.get(link_id)
            if current is not None and version < current:
                return {"status": "stale_ignored", "current_version": current}
            if current == version:
                return {"status": "duplicate_ignored", "current_version": current}
            windows = [
                window_from_dict(w, link_id, version)
                for w in payload.get("windows", [])
            ]
            windows = normalize_windows(windows)
            old = self.store.windows.get(link_id, [])
            changed = diff_window_ranges(old, windows)
            self.store.windows[link_id] = windows
            self.store.window_versions[link_id] = version
            if changed:
                self.store.dirty_link_ranges.setdefault(link_id, []).extend(changed)
            plan = self._replan(f"link_windows:{link_id}@{version}")
            return {
                "status": "accepted",
                "link_id": link_id,
                "version": version,
                "changed_ranges": [list(r) for r in changed],
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    def put_track(
        self,
        vessel_id: str,
        payload: dict,
        request_id: Optional[str] = None,
    ) -> dict:
        def apply() -> dict:
            version = int(payload["version"])
            current = self.store.track_versions.get(vessel_id)
            if current is not None and version < current:
                return {"status": "stale_ignored", "current_version": current}
            if current == version:
                return {"status": "duplicate_ignored", "current_version": current}
            points = sorted(
                (
                    (int(p["tick"]), float(p["lat"]), float(p["lon"]))
                    for p in payload.get("points", [])
                ),
                key=lambda p: p[0],
            )
            old = self.store.tracks.get(vessel_id, [])
            changed = diff_track_ranges(old, points)
            self.store.tracks[vessel_id] = points
            self.store.track_versions[vessel_id] = version
            if changed:
                self.store.dirty_vessel_ranges.setdefault(vessel_id, []).extend(
                    changed
                )
            plan = self._replan(f"track:{vessel_id}@{version}")
            return {
                "status": "accepted",
                "vessel_id": vessel_id,
                "version": version,
                "changed_ranges": [list(r) for r in changed],
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    # ---- 资源维护 -------------------------------------------------------

    def put_terminal(self, payload: dict, request_id: Optional[str] = None) -> dict:
        def apply() -> dict:
            terminal = terminal_from_dict(payload)
            existing = self.store.terminals.get(terminal.terminal_id)
            if existing == terminal:
                return {"status": "duplicate_ignored"}
            self.store.terminals[terminal.terminal_id] = terminal
            self.store.dirty_vessel_ranges.setdefault(
                terminal.vessel_id, []
            ).append((self.store.now, INF))
            plan = self._replan(f"terminal:{terminal.terminal_id}")
            return {
                "status": "accepted",
                "terminal": terminal_to_dict(terminal),
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    def put_exclusion(self, payload: dict, request_id: Optional[str] = None) -> dict:
        def apply() -> dict:
            group = exclusion_from_dict(payload)
            existing = self.store.exclusions.get(group.group_id)
            if existing == group:
                return {"status": "duplicate_ignored"}
            affected_links = set(group.link_ids)
            if existing is not None:
                affected_links |= set(existing.link_ids)
            self.store.exclusions[group.group_id] = group
            for link_id in affected_links:
                self.store.dirty_link_ranges.setdefault(link_id, []).append(
                    (self.store.now, INF)
                )
            plan = self._replan(f"exclusion:{group.group_id}")
            return {
                "status": "accepted",
                "exclusion": exclusion_to_dict(group),
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    # ---- 任务生命周期 ----------------------------------------------------

    def submit_task(self, payload: dict, request_id: Optional[str] = None) -> dict:
        def apply() -> dict:
            task = task_from_dict(payload, submitted_at=self.store.now)
            if task.end <= task.start:
                raise DomainError("invalid_task", "任务区间为空")
            if task.min_hold < 1:
                raise DomainError("invalid_task", "最短保持时间至少为 1")
            existing = self.store.tasks.get(task.task_id)
            if existing is not None:
                if replace(existing, submitted_at=task.submitted_at) == task:
                    return {"status": "duplicate_ignored", "task_id": task.task_id}
                raise DomainError(
                    "task_exists", f"任务 {task.task_id} 已存在且内容不同", status=409
                )
            self.store.tasks[task.task_id] = task
            self.store.dirty_tasks.add(task.task_id)
            plan = self._replan(f"task:{task.task_id}")
            return {
                "status": "accepted",
                "task": task_to_dict(task),
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    def confirm_task(self, task_id: str, request_id: Optional[str] = None) -> dict:
        def apply() -> dict:
            self._require_task(task_id)
            if task_id in self.store.cancelled:
                raise DomainError("task_cancelled", "任务已取消", status=409)
            if task_id in self.store.confirmed:
                return {"status": "duplicate_ignored", "task_id": task_id}
            self.store.confirmed.add(task_id)
            plan = self._replan(f"confirm:{task_id}")
            return {
                "status": "confirmed",
                "task_id": task_id,
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    def cancel_task(self, task_id: str, request_id: Optional[str] = None) -> dict:
        def apply() -> dict:
            self._require_task(task_id)
            if task_id in self.store.cancelled:
                return {"status": "duplicate_ignored", "task_id": task_id}
            self.store.cancelled.add(task_id)
            self.store.dirty_tasks.add(task_id)
            plan = self._replan(f"cancel:{task_id}")
            return {
                "status": "cancelled",
                "task_id": task_id,
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    # ---- 双人批准 --------------------------------------------------------

    def approve(self, payload: dict, request_id: Optional[str] = None) -> dict:
        def apply() -> dict:
            approval_id = str(payload["approval_id"])
            task_id = str(payload["task_id"])
            requester = str(payload["requester_task_id"])
            approver = str(payload["approver"])
            decision = str(payload.get("decision", "allow_preempt"))
            self._require_task(task_id)
            self._require_task(requester)
            approval = self.store.approvals.get(approval_id)
            if approval is None:
                approval = Approval(
                    approval_id=approval_id,
                    task_id=task_id,
                    requester_task_id=requester,
                    decision=decision,
                )
                self.store.approvals[approval_id] = approval
            elif (
                approval.task_id != task_id
                or approval.requester_task_id != requester
                or approval.decision != decision
            ):
                raise DomainError(
                    "approval_conflict",
                    f"批准 {approval_id} 已用于其他任务或决定",
                    status=409,
                )
            if approver in approval.approvers:
                return {
                    "status": "duplicate_ignored",
                    "approval": approval_to_dict(approval),
                }
            was_granted = approval.granted
            approval.approvers.append(approver)
            if approval.granted and not was_granted:
                # 批准生效：把被批准任务的已确认时隙标记为受影响，
                # 使等待同一容量的任务纳入重算。
                if self.store.plans:
                    now = self.store.now
                    for session in self.store.plans[-1].sessions:
                        if session.task_id == task_id and session.end > now:
                            self.store.dirty_link_ranges.setdefault(
                                session.link_id, []
                            ).append((session.start, session.end))
            plan = self._replan(f"approval:{approval_id}")
            return {
                "status": "granted" if approval.granted else "recorded",
                "approval": approval_to_dict(approval),
                "plan_version": plan.version,
            }

        return self._idempotent(request_id, apply)

    # ---- 时钟与重算 -------------------------------------------------------

    def advance_time(self, now: int) -> dict:
        now = int(now)
        if now < self.store.now:
            raise DomainError("time_regression", "时间不可回退", status=409)
        self.store.now = now
        plan = self._replan("time_advance")
        self.store.save()
        return {"now": now, "plan_version": plan.version}

    def replan(self) -> dict:
        plan = self._replan("explicit")
        self.store.save()
        return {"plan_version": plan.version}

    def _require_task(self, task_id: str) -> Task:
        task = self.store.tasks.get(task_id)
        if task is None:
            raise DomainError("task_not_found", f"任务 {task_id} 不存在", status=404)
        return task

    # ---- 增量重算核心 -----------------------------------------------------

    def _replan(self, reason: str) -> Plan:
        store = self.store
        now = store.now
        old = store.plans[-1] if store.plans else None

        affected = set(store.dirty_tasks)
        if old is None:
            affected |= set(store.tasks)
        else:
            for session in old.sessions:
                if session.end <= now or session.task_id in affected:
                    continue
                task = store.tasks.get(session.task_id)
                for start, end in store.dirty_link_ranges.get(session.link_id, ()):
                    if _overlap(session.start, session.end, start, end):
                        affected.add(session.task_id)
                        break
                else:
                    if task is not None:
                        for start, end in store.dirty_vessel_ranges.get(
                            task.vessel_id, ()
                        ):
                            if _overlap(session.start, session.end, start, end):
                                affected.add(session.task_id)
                                break
            # 覆盖缺口与新窗口/新轨迹相交的任务也可能受益，纳入重算
            for gap in old.gaps:
                if gap.task_id in affected or gap.end <= now:
                    continue
                task = store.tasks.get(gap.task_id)
                hit = any(
                    _overlap(gap.start, gap.end, s, e)
                    for ranges in store.dirty_link_ranges.values()
                    for s, e in ranges
                )
                if not hit and task is not None:
                    hit = any(
                        _overlap(gap.start, gap.end, s, e)
                        for s, e in store.dirty_vessel_ranges.get(task.vessel_id, ())
                    )
                if hit:
                    affected.add(gap.task_id)

        checker = ConstraintChecker(store.windows, store.tracks)
        history: list[Session] = []
        fixed: list[Session] = []
        notes: list[str] = []
        for session in old.sessions if old else ():
            if session.end <= now:
                history.append(session)
                continue
            locked = session.locked or session.task_id in store.confirmed
            if session.task_id in store.cancelled:
                if session.start < now:
                    history.append(
                        replace(
                            session,
                            end=now,
                            end_reason=SessionEnd.CANCELLED.value,
                        )
                    )
                continue
            if session.start < now:
                # 在执行中的会话：已确认的一律保留；未确认的若仍有效则保留，
                # 若自某 tick 起失效（窗口缩短 / 覆盖丢失）则在该处截断，
                # 失效点之后由调度器为受影响任务重排。
                if locked:
                    fixed.append(replace(session, locked=True))
                    self._note_if_invalid(checker, session, now, notes)
                    continue
                task = store.tasks.get(session.task_id)
                terminal = store.terminals.get(session.terminal_id)
                invalid_at, invalid_reason = self._first_invalid(
                    checker, task, terminal, session, now
                )
                if session.task_id not in affected and invalid_at is None:
                    fixed.append(session)
                    continue
                cut_at = invalid_at if invalid_at is not None else session.end
                trimmed = replace(
                    session,
                    end=cut_at,
                    end_reason=invalid_reason or session.end_reason,
                )
                if trimmed.end <= now:
                    history.append(trimmed)
                else:
                    fixed.append(trimmed)
                if session.task_id not in affected:
                    affected.add(session.task_id)
                    notes.append(
                        f"in_flight_adjusted:{session.task_id}:{session.link_id}"
                    )
                continue
            if locked:
                fixed.append(replace(session, locked=True))
                self._note_if_invalid(checker, session, now, notes)
                continue
            if session.task_id in affected:
                if session.end_reason.startswith(SessionEnd.PREEMPTED.value):
                    # 抢占残段是已承诺的事实：保留为固定占用。
                    # 若抢占方日后移开，重排出的同链路会话会与之重新合并。
                    fixed.append(session)
                continue  # 其余未来会话丢弃，等待重排
            fixed.append(session)

        active_tasks = [
            task
            for task in store.tasks.values()
            if task.task_id in affected
            and task.task_id not in store.cancelled
            and task.end > now
        ]
        approved = {
            (a.task_id, a.requester_task_id)
            for a in store.approvals.values()
            if a.granted
        }
        preemption_history: set[tuple[str, str]] = set()
        gaps_by_task: dict[str, list[tuple[str, int, int, str]]] = {}
        to_do = active_tasks
        active: list[Session] = fixed
        for _ in range(MAX_CASCADE):
            scheduler = Scheduler(
                checker=checker,
                terminals=store.terminals,
                groups=list(store.exclusions.values()),
                tasks=store.tasks,
                now=now,
                approved=approved,
                confirmed=store.confirmed,
                preemption_history=preemption_history,
            )
            output = scheduler.schedule(to_do, fixed)
            for task in to_do:
                gaps_by_task[task.task_id] = []
            for gap in output.gaps:
                gaps_by_task.setdefault(gap[0], []).append(gap)
            victims = {
                task_id
                for task_id in output.preempted_task_ids
                if task_id in store.tasks
                and task_id not in store.cancelled
                and store.tasks[task_id].end > now
            }
            active = output.sessions
            if not victims:
                break
            affected |= victims
            fixed = [
                s
                for s in output.sessions
                if s.task_id not in victims
                or s.locked
                or s.start < now
                or s.end_reason.startswith(SessionEnd.PREEMPTED.value)
            ]
            to_do = [store.tasks[tid] for tid in sorted(victims)]
        else:
            notes.append("cascade_cap_reached")

        gaps: list[Gap] = []
        for gap in old.gaps if old else ():
            if gap.end <= now or gap.task_id not in affected:
                gaps.append(gap)
            elif gap.start < now:
                # 受影响任务的历史缺口部分（now 之前）是已发生的事实，保留
                gaps.append(Gap(gap.task_id, gap.start, now, gap.reason))
        for task_gaps in gaps_by_task.values():
            for task_id, start, end, gap_reason in task_gaps:
                gaps.append(Gap(task_id, start, end, gap_reason))
        gaps = _merge_gaps(gaps)

        sessions = tuple(
            sorted(
                history + active,
                key=lambda s: (s.start, s.task_id, s.link_id),
            )
        )
        gaps_tuple = tuple(sorted(gaps, key=lambda g: (g.start, g.task_id)))
        store.dirty_tasks.clear()
        store.dirty_link_ranges.clear()
        store.dirty_vessel_ranges.clear()
        if (
            old is not None
            and sessions == old.sessions
            and gaps_tuple == old.gaps
            and tuple(notes) == old.notes
        ):
            return old
        plan = Plan(
            version=(old.version + 1) if old else 1,
            generated_at=now,
            sessions=sessions,
            gaps=gaps_tuple,
            notes=tuple(notes),
        )
        store.plans.append(plan)
        return plan

    @staticmethod
    def _first_invalid(
        checker: ConstraintChecker,
        task: Optional[Task],
        terminal,
        session: Session,
        now: int,
    ) -> tuple[Optional[int], Optional[str]]:
        """在执行中会话自 now 起的第一个失效 tick 及原因；全程有效返回 (None, None)。"""
        if task is None or terminal is None:
            return now, SessionEnd.LINK_LOST.value
        empty = Occupancy({})
        for tick in range(now, session.end):
            blocker = checker.link_blocker(
                task, terminal, session.link_id, tick, empty
            )
            if blocker is not None:
                return tick, BLOCKER_END_REASON[blocker]
        return None, None

    @staticmethod
    def _note_if_invalid(
        checker: ConstraintChecker,
        session: Session,
        now: int,
        notes: list[str],
    ) -> None:
        for tick in range(max(session.start, now), session.end):
            if checker.window_at(session.link_id, tick) is None:
                notes.append(
                    f"locked_session_window_invalid:{session.task_id}:{session.link_id}"
                )
                return

    # ---- 查询视图 ---------------------------------------------------------

    def current_plan_view(self) -> dict:
        if not self.store.plans:
            self._replan("initial")
        return self._plan_view(self.store.plans[-1])

    def plan_view(self, version: int) -> dict:
        for plan in self.store.plans:
            if plan.version == version:
                return self._plan_view(plan)
        raise DomainError("plan_not_found", f"计划版本 {version} 不存在", status=404)

    def _plan_view(self, plan: Plan) -> dict:
        events = derive_events(
            list(plan.sessions), self.store.tasks, self.store.confirmed, self.store.now
        )
        view = plan_to_dict(plan, events, self.store.now)
        if len(self.store.plans) >= 2 and plan is self.store.plans[-1]:
            previous = self.store.plans[-2]
            previous_events = derive_events(
                list(previous.sessions),
                self.store.tasks,
                self.store.confirmed,
                previous.generated_at,
            )
            view["diff"] = diff_events(previous_events, events)
        return view

    def task_schedule(self, task_id: str) -> dict:
        task = self._require_task(task_id)
        view = self.current_plan_view()
        sessions = [
            s for s in view["sessions"] if s["task_id"] == task_id
        ]
        events = [e for e in view["events"] if e["task_id"] == task_id]
        gaps = [g for g in view["gaps"] if g["task_id"] == task_id]
        return {
            "task": task_to_dict(task),
            "confirmed": task_id in self.store.confirmed,
            "cancelled": task_id in self.store.cancelled,
            "sessions": sessions,
            "events": events,
            "gaps": gaps,
        }

    def state_summary(self) -> dict:
        return {
            "now": self.store.now,
            "plan_version": self.store.plans[-1].version
            if self.store.plans
            else None,
            "links": {
                link_id: version
                for link_id, version in sorted(self.store.window_versions.items())
            },
            "tracks": {
                vessel_id: version
                for vessel_id, version in sorted(self.store.track_versions.items())
            },
            "terminals": sorted(self.store.terminals),
            "exclusions": sorted(self.store.exclusions),
            "tasks": {
                task_id: {
                    "confirmed": task_id in self.store.confirmed,
                    "cancelled": task_id in self.store.cancelled,
                }
                for task_id in sorted(self.store.tasks)
            },
            "approvals": {
                approval_id: approval_to_dict(approval)
                for approval_id, approval in sorted(self.store.approvals.items())
            },
        }
