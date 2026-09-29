"""天地链路排程引擎（纯领域服务，不接触持久化与 HTTP）。

逐刻度离散时间仿真。区间一律左闭右开 ``[start, end)``：旧链路在
``t`` 释放、新链路在同一 ``t`` 接入记为一次 HANDOVER，首尾相接，
不计中断。

规则
----

1. **优先级**：紧急任务优先，其次 priority 数值小者；同序按释放时刻、id。
2. **最短保持**：终端在 ``t`` 接入时承诺占用到 ``commit_until``
   （= t + 属主任务 min_hold，并不超过窗末/截止/剩余需求）。承诺期内
   不可被抢占；抢占只能驱逐承诺已到期的低优先级占用。
3. **切换冷却**：终端真正掉线（释放到等待）后，``cooldown`` 个刻度内
   不得重新接入任何链路；无缝交接是预先协调的 make-before-break，豁免冷却。
4. **波束容量**：每刻统计固定占用（未受影响任务）、外部紧急锁预留与动态
   占用；容量不足时高优先级可驱逐可驱逐者，否则冲突等待。
5. **互斥**：一部终端同一时刻只在一条链路上，同终端其他活动任务记
   MUTEX_BLOCKED；互斥组可声明联合容量 group_capacities。
6. **紧急保护**：已确认紧急时隙的容量被预留；普通任务可使用其余容量，
   但不得驱逐紧急属主，只有持双人批准的任务可占用其容量（此时紧急任务
   一并纳入重算）。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..domain.enums import EventType, LinkKind, Reason, SegmentState
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

# 多种阻塞并存时对外报告的关键原因排序。
_WAIT_REASON_RANK = {
    Reason.EMERGENCY_LOCK: 0,
    Reason.MUTEX_BLOCKED: 1,
    Reason.COOLDOWN: 2,
    Reason.CAPACITY_FULL: 3,
    Reason.NO_WINDOW: 4,
}


@dataclass(frozen=True)
class WindowDiff:
    """两版预测之间的窗口差量。"""

    old_version: int
    new_version: int
    added: list[Window] = field(default_factory=list)
    removed: list[Window] = field(default_factory=list)
    changed: list[tuple[Window, Window]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def affected_terminals(self) -> set[str]:
        terms = {w.terminal_id for w in self.added}
        terms |= {w.terminal_id for w in self.removed}
        terms |= {new.terminal_id for _old, new in self.changed}
        return terms

    def to_dict(self) -> dict:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "added": [w.to_dict() for w in self.added],
            "removed": [w.to_dict() for w in self.removed],
            "changed": [
                {"old": old.to_dict(), "new": new.to_dict()}
                for old, new in self.changed
            ],
        }


def diff_windows(old: list[Window], new: list[Window]) -> WindowDiff:
    """按窗口身份键 (uid, 终端, 资源) 计算两版预测差量。"""

    old_map = {w.key: w for w in old}
    new_map = {w.key: w for w in new}
    old_v = old[0].version if old else 0
    new_v = next((w.version for w in new), old_v)
    added: list[Window] = []
    removed: list[Window] = []
    changed: list[tuple[Window, Window]] = []
    for key, w in new_map.items():
        if key not in old_map:
            added.append(w)
        else:
            o = old_map[key]
            if (o.start, o.end) != (w.start, w.end):
                changed.append((o, w))
    for key, w in old_map.items():
        if key not in new_map:
            removed.append(w)
    added.sort(key=lambda w: (w.start, w.window_uid))
    removed.sort(key=lambda w: (w.start, w.window_uid))
    changed.sort(key=lambda pair: (pair[1].start, pair[0].window_uid))
    return WindowDiff(old_v, new_v, added, removed, changed)


@dataclass
class UnschedulableReport:
    """无解（或部分无解）任务说明。"""

    task_id: str
    terminal_id: str
    demanded: int
    served: int
    shortfall: int
    deadline: int
    blocker_ticks: dict[str, int]
    explanation: str

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "terminal_id": self.terminal_id,
            "demanded": self.demanded,
            "served": self.served,
            "shortfall": self.shortfall,
            "deadline": self.deadline,
            "blocker_ticks": dict(self.blocker_ticks),
            "explanation": self.explanation,
        }


@dataclass
class ScheduleResult:
    segments: list[PlanSegment]
    events: list[TimelineEvent]
    locks: list[EmergencyLock]
    unschedulable: list[UnschedulableReport]
    plan_version: int
    cutoff: int

    def stats(self) -> dict:
        return {
            "segments": len(self.segments),
            "events": len(self.events),
            "access": sum(1 for e in self.events if e.event_type is EventType.ACCESS),
            "release": sum(1 for e in self.events if e.event_type is EventType.RELEASE),
            "handover": sum(1 for e in self.events if e.event_type is EventType.HANDOVER),
            "wait": sum(1 for e in self.events if e.event_type is EventType.WAIT),
            "link_ticks": sum(
                s.length for s in self.segments if s.state is SegmentState.LINK
            ),
            "unschedulable_tasks": len(self.unschedulable),
        }


@dataclass
class ScheduleRequest:
    """一次（全量或增量）排程输入。

    - cutoff：确认时刻，只计算 [cutoff, horizon)，cutoff 之前不动；
    - frozen_prefixes：受影响任务 cutoff 之前的段（用于扣减已服务量）；
    - fixed_segments：不受影响任务的全部段，其未来 LINK 段不可驱逐；
    - active_locks：外部已确认、且属主不在本次重算范围内的紧急时隙预留；
    - granted_approvals：已生效双人批准；
    - continuity：跨 cutoff 仍在线的终端 -> (资源, 属主任务)；
    - group_capacities：互斥组联合容量。
    """

    resources: dict[str, Resource]
    terminals: dict[str, Terminal]
    tasks: list[Task]
    windows: list[Window]
    horizon: int
    cutoff: int = 0
    affected_task_ids: set[str] | None = None
    frozen_prefixes: list[PlanSegment] = field(default_factory=list)
    fixed_segments: list[PlanSegment] = field(default_factory=list)
    active_locks: list[EmergencyLock] = field(default_factory=list)
    granted_approvals: list[ApprovalRecord] = field(default_factory=list)
    continuity: dict[str, tuple[str, str]] = field(default_factory=dict)
    group_capacities: dict[str, int] = field(default_factory=dict)
    plan_version: int = 1
    prediction_version: int = 1


@dataclass
class _TaskRT:
    task: Task
    remaining: int
    finished: bool = False
    state: SegmentState | None = None
    seg_start: int = 0
    resource: str | None = None
    reason: Reason | None = None
    blockers: Counter = field(default_factory=Counter)
    segments: list[PlanSegment] = field(default_factory=list)


@dataclass
class _TermRT:
    terminal: Terminal
    resource: str | None = None
    owner: str | None = None            # 当前被服务任务 id
    commit_until: int = 0
    last_drop: int | None = None        # 上次真正掉线时刻（冷却基准）
    wait_reason: Reason | None = None
    wait_key: tuple[str, str] | None = None
    ever_connected: bool = False

    def rank(self, lead: Task) -> tuple:
        return (0 if lead.emergency else 1, lead.priority, lead.release, lead.id)

    def cooldown_ready(self, t: int) -> bool:
        return self.last_drop is None or t >= self.last_drop + self.terminal.cooldown


class SchedulerEngine:
    """逐刻度排程引擎（无状态，可重复调用）。"""

    def schedule(self, req: ScheduleRequest) -> ScheduleResult:
        self._pv = req.prediction_version
        affected = req.affected_task_ids
        if affected is None:
            affected = {t.id for t in req.tasks}

        # -- 冻结前缀：仅用于扣减已服务量（旧段由仓储保留，不再重复输出）----
        served: dict[str, int] = defaultdict(int)
        for seg in req.frozen_prefixes:
            if seg.state is SegmentState.LINK:
                served[seg.task_id] += seg.length

        # -- 受影响任务运行时 ----------------------------------------------
        rts: dict[str, _TaskRT] = {}
        for task in req.tasks:
            if task.id not in affected:
                continue
            remain = max(0, task.demand - served.get(task.id, 0))
            if remain <= 0 or task.deadline <= req.cutoff:
                continue
            rts[task.id] = _TaskRT(task=task, remaining=remain)

        terms: dict[str, _TermRT] = {
            rt.task.terminal_id: _TermRT(terminal=req.terminals[rt.task.terminal_id])
            for rt in rts.values()
        }
        # 跨 cutoff 连续性：视为冷却已走完，commit 不再约束（已确认部分）。
        for tid, (res, owner_id) in req.continuity.items():
            if tid not in terms or res is None:
                continue
            tr = terms[tid]
            tr.resource = res
            tr.owner = owner_id
            tr.ever_connected = True
            tr.commit_until = req.cutoff
            if owner_id in rts:
                rt = rts[owner_id]
                rt.state = SegmentState.LINK
                rt.resource = res
                rt.seg_start = req.cutoff

        tasks_by_terminal: dict[str, list[_TaskRT]] = defaultdict(list)
        for rt in rts.values():
            tasks_by_terminal[rt.task.terminal_id].append(rt)

        # -- 窗口索引 -------------------------------------------------------
        win_index: dict[tuple[str, str], list[Window]] = defaultdict(list)
        for w in req.windows:
            win_index[(w.terminal_id, w.resource_id)].append(w)
        for intervals in win_index.values():
            intervals.sort(key=lambda w: w.start)

        def covering(tid: str, rid: str, t: int) -> Window | None:
            for w in win_index.get((tid, rid), ()):
                if w.start <= t < w.end:
                    return w
                if w.start > t:
                    break
            return None

        # -- 固定占用日历（不受影响任务，不可驱逐）--------------------------
        fixed_occ: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        fixed_term_busy: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for seg in req.fixed_segments:
            if seg.state is not SegmentState.LINK or not seg.resource_id:
                continue
            a, b = max(seg.start, req.cutoff), seg.end
            if a < b:
                fixed_occ[seg.resource_id].append((a, b, seg.terminal_id))
                fixed_term_busy[seg.terminal_id].append((a, b))
        # -- 外部紧急锁日历 -------------------------------------------------
        # 锁是紧急时隙的“预留”。若该终端在同一资源同一刻度已有固定 LINK 段，
        # 占用已计入 fixed_occ，不再重复计数，避免容量虚高。
        lock_occ: dict[str, list[tuple[int, int, str, str]]] = defaultdict(list)
        for lock in req.active_locks:
            a, b = max(lock.start, req.cutoff), lock.end
            for tt in range(a, b):
                already_fixed = any(
                    x <= tt < y and tid == lock.terminal_id
                    for x, y, tid in fixed_occ.get(lock.resource_id, ())
                )
                if not already_fixed:
                    lock_occ[lock.resource_id].append(
                        (tt, tt + 1, lock.terminal_id, lock.task_id))
        # -- 双人批准日历：(task_id, resource) -> [区间]
        # 批准=为受益任务预留一个容量名额：对其他任务是硬占用，
        # 对受益任务自身不占自身名额。
        appr_cal: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        # (rid, t) -> [(beneficiary_task, beneficiary_terminal)]
        approval_slots: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)
        for appr in req.granted_approvals:
            a, b = max(appr.start, req.cutoff), appr.end
            if a < b:
                appr_cal[(appr.task_id, appr.resource_id)].append((a, b))
                for tt in range(a, b):
                    approval_slots[(appr.resource_id, tt)].append(
                        (appr.task_id, appr.terminal_id))

        def fixed_terms(rid: str, t: int) -> set[str]:
            return {tid for a, b, tid in fixed_occ.get(rid, ()) if a <= t < b}

        def lock_owners(rid: str, t: int) -> dict[str, str]:
            return {tid: task for a, b, tid, task in lock_occ.get(rid, ())
                    if a <= t < b}

        def term_busy_fixed(tid: str, t: int) -> bool:
            return any(a <= t < b for a, b in fixed_term_busy.get(tid, ()))

        def is_approved(task_id: str, rid: str, t: int) -> bool:
            return any(a <= t < b for a, b in appr_cal.get((task_id, rid), ()))

        def _approval_terms_on(rid: str, t: int,
                               beneficiary: str | None
                               ) -> list[tuple[str, str]]:
            """该刻资源上因他人的双人批准而被预留的 (任务, 终端) 名额。"""
            return [
                (task_id, term_id)
                for task_id, term_id in approval_slots.get((rid, t), ())
                if task_id != beneficiary
            ]

        def dyn_terms(rid: str, t: int) -> set[str]:
            return {x.terminal.id for x in terms.values()
                    if x.resource == rid and x.owner is not None}

        # -- 事件（event_key 去重，重复生成保持幂等）-----------------------
        event_seen: set[str] = set()
        events: list[TimelineEvent] = []

        def emit(et: EventType, t: int, rt: _TaskRT, rid: str | None,
                 reason: Reason, detail: str = "") -> None:
            key = f"{rt.task.id}|{et.value}|{t}|{rid or '-'}|{reason.value}"
            if key in event_seen:
                return
            event_seen.add(key)
            events.append(TimelineEvent(
                time=t, event_type=et, task_id=rt.task.id,
                terminal_id=rt.task.terminal_id, resource_id=rid, reason=reason,
                detail=detail, event_key=key, plan_version=req.plan_version,
            ))

        # -- 候选链路与可行性 -----------------------------------------------
        def candidates(tr: _TermRT, t: int) -> list[Resource]:
            out = [r for r in req.resources.values()
                   if tr.terminal.supports(r)
                   and covering(tr.terminal.id, r.id, t) is not None]
            out.sort(key=lambda r: (
                0 if r.kind is LinkKind.TERRESTRIAL else 1,
                -(covering(tr.terminal.id, r.id, t).end - t),
                r.id,
            ))
            return out

        def group_blocked(tr: _TermRT, r: Resource, t: int,
                          beneficiary: str | None = None) -> bool:
            g = r.mutex_group
            cap = req.group_capacities.get(g) if g else None
            if cap is None:
                return False
            occ: set[str] = set()
            for gr in req.resources.values():
                if gr.mutex_group != g:
                    continue
                occ |= fixed_terms(gr.id, t)
                occ |= set(lock_owners(gr.id, t))
                occ |= dyn_terms(gr.id, t)
                for _btask, bterm in _approval_terms_on(gr.id, t, beneficiary):
                    occ.add(bterm)
            occ.discard(tr.terminal.id)
            return len(occ) >= cap

        def feasible(tr: _TermRT, r: Resource, t: int, need: int,
                     owner_rt: _TaskRT, allow_evict: bool
                     ) -> tuple[bool, set[str], set[Reason]]:
            """检查接入 r 并连续保持 need 个刻度是否可行。

            占用分三类：

            - hard：固定段、外部紧急锁、他人的双人批准预留名额（若尚未物理
              接入则以幽灵名额计入）、以及承诺期内的动态占用——不可移除；
            - forced：持双人批准时可让予名额的紧急任务动态占用；
            - soft：承诺到期、且优先级严格更低、可在接入当刻驱逐的占用。

            返回 (可行, 需在 t 驱逐的终端, 阻塞原因集合)。
            """
            tid = tr.terminal.id
            beneficiary = owner_rt.task.id
            blockers: set[Reason] = set()
            victims: set[str] = set()
            for u in range(t, t + need):
                w = covering(tid, r.id, u)
                if w is None:
                    blockers.add(Reason.NO_WINDOW)
                    return False, victims, blockers
                if term_busy_fixed(tid, u):
                    blockers.add(Reason.MUTEX_BLOCKED)
                    return False, victims, blockers

                approved_here = is_approved(beneficiary, r.id, u)
                dyn_now = dyn_terms(r.id, u)

                hard: set[str] = set(fixed_terms(r.id, u))
                # 外部紧急锁（日历构建时已去掉与固定段重叠的部分）。
                lock_blocking_terms: set[str] = set()
                for owner_tid in lock_owners(r.id, u):
                    if owner_tid == tid:
                        continue
                    if approved_here:
                        continue  # 批准受益任务可让紧急锁让路
                    hard.add(owner_tid)
                    lock_blocking_terms.add(owner_tid)
                # 他人的批准预留名额；若其终端已物理占用该资源则不重复计数。
                for (btask, bterm) in _approval_terms_on(r.id, u, beneficiary):
                    if bterm not in dyn_now and bterm not in hard:
                        hard.add(bterm)

                soft: set[str] = set()
                forced: set[str] = set()
                for dt in dyn_now:
                    if dt == tid:
                        continue
                    other = terms[dt]
                    ovrt = rts.get(other.owner) if other.owner else None
                    # 持双人批准的在线占用：批准名额受保护，任何任务不可反抢占。
                    if ovrt is not None and is_approved(ovrt.task.id, r.id, u):
                        hard.add(dt)
                        continue
                    # 对方窗口若在 u 前必然结束，u 刻不可能仍占用此资源。
                    if covering(dt, r.id, u) is None:
                        continue
                    if approved_here and ovrt is not None and ovrt.task.emergency:
                        forced.add(dt)  # 紧急属主同批重算，批准名额可让其让路
                        continue
                    if u > t or other.commit_until > u:
                        hard.add(dt)
                    else:
                        soft.add(dt)

                # 普通驱逐只在接入当刻、对优先级严格更低者生效。
                # 受害者按（非紧急、优先级低、释放晚、id）从弱到强排序，
                # 尽量驱逐最弱的一个，减少不必要扰动。
                def victim_strength(dt: str) -> tuple:
                    ovrt = rts.get(terms[dt].owner) if terms[dt].owner else None
                    if ovrt is None:
                        return (1, 1 << 30, 0, dt)
                    lead = ovrt.task
                    return (0 if lead.emergency else 1, lead.priority,
                            -lead.release, lead.id)

                evictable: set[str] = set(forced)
                if u == t and allow_evict:
                    for dt in soft:
                        other = terms[dt]
                        vrt = rts.get(other.owner) if other.owner else None
                        if vrt is not None and tr.rank(owner_rt.task) < other.rank(vrt.task):
                            evictable.add(dt)
                removable_now = evictable if u == t else forced
                total = hard | soft | forced
                must_leave = len(total) - (r.capacity - 1)
                if len(hard) > r.capacity - 1 or must_leave > len(removable_now):
                    if lock_blocking_terms and not approved_here:
                        blockers.add(Reason.EMERGENCY_LOCK)
                    else:
                        blockers.add(Reason.CAPACITY_FULL)
                else:
                    if u == t and must_leave > 0:
                        ranked = sorted(evictable, key=victim_strength,
                                        reverse=True)
                        victims.update(ranked[:must_leave])
                if group_blocked(tr, r, u, beneficiary):
                    blockers.add(Reason.MUTEX_BLOCKED)
            if blockers:
                return False, victims, blockers
            return True, victims, set()

        def connect(tr: _TermRT, r: Resource, t: int, owner_rt: _TaskRT,
                    reason: Reason, victims: set[str]) -> None:
            for vt in victims:
                victim = terms[vt]
                vrt = rts[victim.owner]
                old = victim.resource
                # 优先让被抢占者在同刻无缝交接至其他链路。
                new_rid = self._handover(
                    victim, t, old, Reason.HIGHER_PRIORITY_PREEMPT, vrt, req,
                    terms, rts, candidates, covering, lock_owners,
                    is_approved, feasible, emit,
                )
                if new_rid is None:
                    self._flush(vrt, t)
                    emit(EventType.RELEASE, t, vrt, old,
                         Reason.PREEMPTED, detail="容量让给更高优先级任务")
                    emit(EventType.WAIT, t, vrt, None, Reason.PREEMPTED)
                    victim.resource = None
                    victim.owner = None
                    victim.last_drop = t
                    victim.wait_reason = Reason.PREEMPTED
                    victim.wait_key = (vrt.task.id, Reason.PREEMPTED.value)
            need = max(1, min(owner_rt.task.min_hold, owner_rt.remaining,
                              owner_rt.task.deadline - t))
            tr.resource = r.id
            tr.owner = owner_rt.task.id
            tr.commit_until = t + need
            tr.last_drop = None  # 重新接入成功，冷却基准清除
            tr.ever_connected = True
            self._flush(owner_rt, t, new_state=SegmentState.LINK,
                        new_resource=r.id)
            emit(EventType.ACCESS, t, owner_rt, r.id, reason)

        def active_rts_of(tid: str, t: int) -> list[_TaskRT]:
            return [rt for rt in tasks_by_terminal[tid] if self._active(rt, t)]

        def try_access(tr: _TermRT, t: int) -> tuple[bool, Reason, set[Reason]]:
            if not tr.cooldown_ready(t):
                return False, Reason.COOLDOWN, {Reason.COOLDOWN}
            active = active_rts_of(tr.terminal.id, t)
            if not active:
                return False, Reason.NO_WINDOW, {Reason.NO_WINDOW}
            owner_rt = min(active, key=lambda rt: tr.rank(rt.task))
            need = max(1, min(owner_rt.task.min_hold, owner_rt.remaining,
                              owner_rt.task.deadline - t))
            all_blockers: set[Reason] = set()
            any_candidate = False
            for r in candidates(tr, t):
                any_candidate = True
                w = covering(tr.terminal.id, r.id, t)
                if w.end - t < need:
                    all_blockers.add(Reason.NO_WINDOW)
                    continue
                ok, victims, blockers = feasible(tr, r, t, need, owner_rt, True)
                if ok:
                    reason = (Reason.APPROVAL_OVERRIDE
                              if is_approved(owner_rt.task.id, r.id, t)
                              else self._access_reason(tr))
                    connect(tr, r, t, owner_rt, reason, victims)
                    return True, reason, set()
                all_blockers |= blockers
            if not any_candidate:
                all_blockers.add(Reason.NO_WINDOW)
            if not all_blockers:
                all_blockers.add(Reason.CAPACITY_FULL)
            key = min(all_blockers, key=lambda x: _WAIT_REASON_RANK[x])
            return False, key, all_blockers

        # -- 主循环 ---------------------------------------------------------
        for t in range(req.cutoff, req.horizon):
            # (1) 属主完成/到期：链路交给同终端下一活动任务，或释放。
            for tr in terms.values():
                if tr.resource is None or tr.owner is None:
                    continue
                ort = rts.get(tr.owner)
                if ort is not None and self._active(ort, t):
                    continue
                active = active_rts_of(tr.terminal.id, t)
                if active:
                    nrt = min(active, key=lambda rt: tr.rank(rt.task))
                    if ort is not None:
                        self._flush(ort, t)
                    tr.owner = nrt.task.id
                    tr.commit_until = max(
                        tr.commit_until,
                        t + max(1, min(nrt.task.min_hold, nrt.remaining,
                                       nrt.task.deadline - t)),
                    )
                    self._flush(nrt, t, new_state=SegmentState.LINK,
                                new_resource=tr.resource)
                    emit(EventType.ACCESS, t, nrt, tr.resource,
                         Reason.TASK_RELEASE, detail="终端复用，下一任务接入")
                else:
                    if ort is not None and not ort.finished:
                        self._flush(ort, t)
                        emit(EventType.RELEASE, t, ort, tr.resource,
                             Reason.DEADLINE)
                    # ort.finished 的 RELEASE 已在完成刻度 (4) 发出。
                    tr.resource = None
                    tr.owner = None

            # (2) 在线终端：窗口结束或外部紧急锁到达 → 同刻交接，否则释放。
            for tr in list(terms.values()):
                if tr.resource is None or tr.owner is None:
                    continue
                rid = tr.resource
                ort = rts[tr.owner]
                win_ok = covering(tr.terminal.id, rid, t) is not None
                ext_lock = any(
                    otid != tr.terminal.id
                    and not is_approved(ort.task.id, rid, t)
                    for otid in lock_owners(rid, t)
                )
                if win_ok and not ext_lock:
                    continue
                if not win_ok:
                    why_end, ho_reason = Reason.WINDOW_END, Reason.WINDOW_END_HANDOVER
                else:
                    why_end, ho_reason = Reason.EMERGENCY_LOCK, Reason.HIGHER_PRIORITY_PREEMPT
                new_rid = self._handover(
                    tr, t, rid, ho_reason, ort, req, terms, rts,
                    candidates, covering, lock_owners, is_approved,
                    feasible, emit,
                )
                if new_rid is None:
                    self._flush(ort, t)
                    emit(EventType.RELEASE, t, ort, rid, why_end)
                    tr.resource = None
                    tr.owner = None
                    tr.last_drop = t
                    tr.wait_reason = why_end
                    tr.wait_key = None

            # (3) 空闲终端按优先级申请接入（可驱逐到期的低优先级占用）。
            waiting = [
                tr for tr in terms.values()
                if tr.resource is None
                and not term_busy_fixed(tr.terminal.id, t)
                and active_rts_of(tr.terminal.id, t)
            ]
            waiting.sort(
                key=lambda x: x.rank(min(active_rts_of(x.terminal.id, t),
                                         key=lambda rt: x.rank(rt.task)).task)
            )
            for tr in waiting:
                if tr.resource is not None:
                    continue
                ok, reason, _blockers = try_access(tr, t)
                active = active_rts_of(tr.terminal.id, t)
                if ok:
                    tr.wait_reason = None
                    tr.wait_key = None
                    continue
                tr.wait_reason = reason
                lead = min(active, key=lambda rt: tr.rank(rt.task))
                key = (lead.task.id, reason.value)
                if tr.wait_key != key:
                    emit(EventType.WAIT, t, lead, None, reason)
                    tr.wait_key = key

            # (4) 逐任务记账：推进服务量、记录段与阻塞刻度。
            for tr in terms.values():
                fixed_busy = term_busy_fixed(tr.terminal.id, t)
                active = active_rts_of(tr.terminal.id, t)
                for rt in active:
                    if tr.resource is not None and tr.owner == rt.task.id:
                        self._flush(rt, t, new_state=SegmentState.LINK,
                                    new_resource=tr.resource)
                        rt.remaining -= 1
                        if rt.remaining == 0:
                            rt.finished = True
                            self._flush(rt, t + 1)
                            emit(EventType.RELEASE, t + 1, rt, tr.resource,
                                 Reason.TASK_COMPLETE, detail="通信量完成")
                    elif fixed_busy:
                        rt.blockers[Reason.MUTEX_BLOCKED.value] += 1
                        self._flush(rt, t, new_state=SegmentState.WAIT,
                                    new_reason=Reason.MUTEX_BLOCKED)
                    elif tr.resource is not None:
                        rt.blockers[Reason.MUTEX_BLOCKED.value] += 1
                        self._flush(rt, t, new_state=SegmentState.WAIT,
                                    new_reason=Reason.MUTEX_BLOCKED)
                    else:
                        # 终端空闲：同终端所有活动任务都在排队，
                        # 共同受队首任务的等待原因阻塞。
                        reason = tr.wait_reason or Reason.NO_WINDOW
                        rt.blockers[reason.value] += 1
                        self._flush(rt, t, new_state=SegmentState.WAIT,
                                    new_reason=reason)

        # -- 收尾：未完成任务在 min(deadline, horizon) 闭合 ----------------
        end_t = req.horizon
        for rt in rts.values():
            e = min(rt.task.deadline, end_t)
            if rt.state is SegmentState.LINK:
                rid = rt.resource
                self._flush(rt, e)
                emit(EventType.RELEASE, e, rt, rid,
                     Reason.DEADLINE if e == rt.task.deadline else Reason.HORIZON_END)
            elif rt.state is SegmentState.WAIT:
                self._flush(rt, e)

        # -- 汇总段、事件、锁与无解说明 ------------------------------------
        all_segments = [
            s for rt in rts.values() for s in rt.segments if s.end > s.start
        ]
        all_segments.sort(key=lambda s: (s.start, s.task_id, s.resource_id or ""))
        events.sort(key=lambda e: (
            e.time,
            {EventType.RELEASE: 0, EventType.HANDOVER: 1,
             EventType.ACCESS: 2, EventType.WAIT: 3}[e.event_type],
            e.task_id,
        ))

        locks: list[EmergencyLock] = []
        for rt in rts.values():
            if not rt.task.emergency:
                continue
            for s in rt.segments:
                if (s.state is SegmentState.LINK and s.resource_id
                        and s.start >= req.cutoff):
                    locks.append(EmergencyLock(
                        task_id=rt.task.id, terminal_id=rt.task.terminal_id,
                        resource_id=s.resource_id, start=s.start, end=s.end,
                        mutex_domain=s.resource_id,
                    ))

        unsched: list[UnschedulableReport] = []
        for rt in sorted(rts.values(), key=lambda x: x.task.id):
            if rt.remaining > 0:
                served_n = rt.task.demand - rt.remaining
                unsched.append(UnschedulableReport(
                    task_id=rt.task.id, terminal_id=rt.task.terminal_id,
                    demanded=rt.task.demand, served=served_n,
                    shortfall=rt.remaining, deadline=rt.task.deadline,
                    blocker_ticks=dict(rt.blockers),
                    explanation=self._explain(
                        rt.task, served_n, rt.remaining, dict(rt.blockers)),
                ))

        return ScheduleResult(
            segments=all_segments, events=events, locks=locks,
            unschedulable=unsched, plan_version=req.plan_version,
            cutoff=req.cutoff,
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _active(rt: _TaskRT, t: int) -> bool:
        return not rt.finished and rt.task.release <= t < rt.task.deadline

    def _flush(self, rt: _TaskRT, end: int, *, new_state: SegmentState | None = None,
               new_resource: str | None = None,
               new_reason: Reason | None = None) -> None:
        """把当前开放段闭合到 end，并切换到新状态。

        新状态与当前状态相同（LINK 同资源，或 WAIT 同原因）时只延展不切段；
        new_state=None 表示终结（闭合后不再开放）。LINK 段不携带等待原因。
        """
        if rt.state is not None:
            same = (
                new_state == rt.state
                and (rt.state is SegmentState.LINK and new_resource == rt.resource
                     or rt.state is SegmentState.WAIT and new_reason == rt.reason)
            )
            if not same and end > rt.seg_start:
                rt.segments.append(PlanSegment(
                    task_id=rt.task.id, terminal_id=rt.task.terminal_id,
                    start=rt.seg_start, end=end, state=rt.state,
                    resource_id=rt.resource, reason=rt.reason,
                    predicted_version=self._pv,
                ))
            if not same:
                rt.seg_start = end
        else:
            rt.seg_start = end
        if new_state is None:
            rt.state = None
            rt.resource = None
            return
        rt.state = new_state
        rt.resource = new_resource if new_state is SegmentState.LINK else None
        if new_state is SegmentState.LINK:
            rt.reason = None
        elif new_reason is not None:
            rt.reason = new_reason

    @staticmethod
    def _access_reason(tr: _TermRT) -> Reason:
        if not tr.ever_connected:
            return Reason.TASK_RELEASE
        if tr.wait_reason in (Reason.WINDOW_END, Reason.EMERGENCY_LOCK,
                              Reason.PREEMPTED, Reason.NO_WINDOW):
            return Reason.WINDOW_START if tr.wait_reason is Reason.NO_WINDOW \
                else Reason.CAPACITY_AVAILABLE
        if tr.wait_reason is Reason.COOLDOWN:
            return Reason.COOLDOWN_READY
        return Reason.CAPACITY_AVAILABLE

    def _handover(self, tr: _TermRT, t: int, old_rid: str,
                  ho_reason: Reason, ort: _TaskRT, req: ScheduleRequest,
                  terms, rts, candidates, covering, lock_owners,
                  is_approved, feasible, emit) -> str | None:
        """同刻寻找替代链路完成无缝交接；冷却未结束或无可行替代则 None。"""
        if not tr.cooldown_ready(t):
            return None
        need = max(1, min(ort.task.min_hold, ort.remaining,
                          ort.task.deadline - t))
        for r in candidates(tr, t):
            if r.id == old_rid:
                continue
            w = covering(tr.terminal.id, r.id, t)
            if w is None or w.end - t < need:
                continue
            if any(otid != tr.terminal.id
                   and not is_approved(ort.task.id, r.id, t)
                   for otid in lock_owners(r.id, t)):
                continue
            ok, _victims, _blockers = feasible(tr, r, t, need, ort, False)
            if not ok:
                continue
            self._flush(ort, t, new_state=SegmentState.LINK, new_resource=r.id)
            emit(EventType.HANDOVER, t, ort, r.id, ho_reason,
                 detail=f"由 {old_rid} 无缝交接至 {r.id}")
            tr.resource = r.id
            tr.commit_until = t + need
            tr.ever_connected = True
            return r.id
        return None

    @staticmethod
    def _explain(task: Task, served: int, shortfall: int,
                 blocker_ticks: dict[str, int]) -> str:
        if blocker_ticks:
            detail = "、".join(
                f"{Reason(k).label} {v} 刻度"
                for k, v in sorted(blocker_ticks.items(), key=lambda kv: -kv[1])
            )
        else:
            detail = "活动窗口内可用容量始终不足"
        return (
            f"任务 {task.id} 需求 {task.demand} 刻度，截止前仅获得 {served}，"
            f"缺口 {shortfall}；等待构成：{detail}。"
        )
