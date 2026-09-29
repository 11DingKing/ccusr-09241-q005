"""计划应用服务：场景建立、预测摄入与增量重算、双人批准。

这是领域引擎与端口之间的用事编排层：

- 新版预测晚到时，先按窗口身份键做差量，只把覆盖真正变化的终端上、
  截止时刻之后仍活动的任务纳入重算；
- cutoff 之前的段保持确认，跨刻度段在 cutoff 处切分；
- 未受影响任务的未来段作为固定占用（不可驱逐）参与容量核算；
- 紧急任务的已确认时隙以锁的形式预留，普通任务无法夺走；
- 双人批准一经集齐两名不同批准人立即生效，并触发一次仅涉及
  受益任务与被挤让紧急任务的重算。
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import ApprovalDecision, SegmentState
from ..domain.models import (
    ApprovalRecord,
    PlanSegment,
    Task,
)
from .ports import Clock, IdGenerator, Observer, PlanRepository, ScenarioRepository
from .scheduler import SchedulerEngine, ScheduleRequest
from .serialization import (
    resource_from,
    task_from,
    terminal_from,
    window_from,
)


class PlanningService:
    def __init__(
        self,
        scenarios: ScenarioRepository,
        plans: PlanRepository,
        clock: Clock,
        id_gen: IdGenerator,
        observer: Observer,
        engine: SchedulerEngine | None = None,
    ) -> None:
        self.scenarios = scenarios
        self.plans = plans
        self.clock = clock
        self.id_gen = id_gen
        self.observer = observer
        self.engine = engine or SchedulerEngine()

    # ------------------------------------------------------------------ #
    # 场景
    # ------------------------------------------------------------------ #

    def setup_scenario(self, payload: dict[str, Any]) -> dict[str, Any]:
        resources = [resource_from(d) for d in payload.get("resources", [])]
        terminals = [terminal_from(d) for d in payload.get("terminals", [])]
        tasks = [task_from(d) for d in payload.get("tasks", [])]
        horizon = int(payload.get("horizon", 0))
        group_capacities = {
            str(k): int(v) for k, v in payload.get("group_capacities", {}).items()
        }
        rids = {r.id for r in resources}
        tids = {t.id for t in terminals}
        groups = {r.mutex_group for r in resources if r.mutex_group}
        for g in group_capacities:
            if g not in groups:
                raise ValueError(f"互斥组 {g} 未被任何资源引用")
        for t in tasks:
            if t.terminal_id not in tids:
                raise ValueError(f"任务 {t.id} 引用了未知终端 {t.terminal_id}")
            if t.deadline > horizon:
                raise ValueError(f"任务 {t.id} 截止 {t.deadline} 超过视界 {horizon}")
        self.scenarios.save_scenario(resources, terminals, tasks, horizon,
                                     group_capacities)
        self.observer.emit("scenario.setup", {
            "resources": len(resources), "terminals": len(terminals),
            "tasks": len(tasks), "horizon": horizon,
        })
        return {
            "resources": [r.to_dict() for r in resources],
            "terminals": [t.to_dict() for t in terminals],
            "tasks": [t.to_dict() for t in tasks],
            "group_capacities": group_capacities,
            "horizon": horizon,
        }

    # ------------------------------------------------------------------ #
    # 预测摄入
    # ------------------------------------------------------------------ #

    def ingest_prediction(self, payload: dict[str, Any]) -> dict[str, Any]:
        declared = payload.get("version")
        version = int(declared) if declared is not None \
            else self.scenarios.latest_version() + 1
        windows = [window_from(d, version) for d in payload.get("windows", [])]
        scenario = self.scenarios.load_scenario()
        rids = {r.id for r in scenario["resources"]}
        tids = {t.id for t in scenario["terminals"]}
        for w in windows:
            if w.version != version:
                raise ValueError(
                    f"窗口 {w.window_uid} 版本 {w.version} 与批次版本 {version} 不一致"
                )
            if w.resource_id not in rids:
                raise ValueError(f"窗口 {w.window_uid} 引用未知资源 {w.resource_id}")
            if w.terminal_id not in tids:
                raise ValueError(f"窗口 {w.window_uid} 引用未知终端 {w.terminal_id}")
        diff = self.scenarios.put_windows(windows)
        self.observer.emit("prediction.ingested", {
            "version": version,
            "added": len(diff["added"]), "removed": len(diff["removed"]),
            "changed": len(diff["changed"]),
        })
        if not (diff["added"] or diff["removed"] or diff["changed"]):
            return {"version": version, "changed": False, "diff": diff,
                    "message": "预测与当前版本完全相同，未触发重算"}
        changed_terminals = {w["terminal_id"] for w in diff["added"]}
        changed_terminals |= {w["terminal_id"] for w in diff["removed"]}
        changed_terminals |= {w["new"]["terminal_id"] for w in diff["changed"]}
        meta = self.plans.get_plan_meta()
        if not meta.get("plan_version"):
            # 计划尚未生成：只落库预测，等待显式 generate。
            return {"version": version, "changed": True, "diff": diff,
                    "message": "预测已落库，等待生成初始计划"}
        recomputed = self.recompute(
            cutoff=self.clock.now(),
            reason_terminals=changed_terminals,
            prediction_version=version,
            trigger="prediction_update",
        )
        recomputed["diff"] = diff
        return recomputed

    # ------------------------------------------------------------------ #
    # 初次排程 / 增量重算
    # ------------------------------------------------------------------ #

    def generate_initial_plan(self) -> dict[str, Any]:
        meta = self.plans.get_plan_meta()
        if meta.get("plan_version"):
            # 初始计划已随首版预测生成；重复调用幂等返回当前计划摘要。
            return {
                "changed": False,
                "plan_version": meta["plan_version"],
                "prediction_version": meta.get("prediction_version"),
                "cutoff": meta.get("confirmed_at", 0),
                "trigger": "initial",
                "message": "初始计划已存在，未重复排程",
            }
        return self.recompute(cutoff=0, reason_terminals=None,
                              prediction_version=self.scenarios.latest_version(),
                              trigger="initial")

    def recompute(
        self,
        cutoff: int,
        reason_terminals: set[str] | None,
        prediction_version: int | None,
        trigger: str,
        extra_affected: set[str] | None = None,
    ) -> dict[str, Any]:
        scenario = self.scenarios.load_scenario()
        resources = {r.id: r for r in scenario["resources"]}
        terminals = {t.id: t for t in scenario["terminals"]}
        tasks: list[Task] = scenario["tasks"]
        horizon = scenario["horizon"]
        group_capacities = scenario.get("group_capacities", {})
        windows = self.scenarios.list_windows()

        meta = self.plans.get_plan_meta()
        plan_version = meta["plan_version"] + 1
        if prediction_version is None:
            prediction_version = self.scenarios.latest_version()

        affected = self._affected_tasks(tasks, cutoff, reason_terminals,
                                        extra_affected or set())

        frozen_prefixes, fixed_segments, continuity = self._classify_segments(
            cutoff, affected,
        )

        active_locks = [
            l for l in self.plans.list_locks()
            if l.task_id not in affected and l.end > cutoff
        ]
        granted = [
            a for a in self.plans.list_approvals()
            if a.granted and a.end > cutoff
        ]

        req = ScheduleRequest(
            resources=resources,
            terminals=terminals,
            tasks=tasks,
            windows=windows,
            horizon=horizon,
            cutoff=cutoff,
            affected_task_ids=affected,
            frozen_prefixes=frozen_prefixes,
            fixed_segments=fixed_segments,
            active_locks=active_locks,
            granted_approvals=granted,
            continuity=continuity,
            group_capacities=group_capacities,
            plan_version=plan_version,
            prediction_version=prediction_version,
        )
        result = self.engine.schedule(req)

        # 持久化：先切分跨 cutoff 段，再替换受影响任务的未来段/事件/锁。
        self._truncate_crossing_segments(cutoff, affected)
        self.plans.drop_future_events(cutoff, affected)
        added = self.plans.save_events(result.events)
        self.plans.replace_future(result.segments, cutoff, affected, plan_version)
        self.plans.replace_locks(result.locks, affected, cutoff)
        self.plans.set_plan_meta({
            "plan_version": plan_version,
            "prediction_version": prediction_version,
            "confirmed_at": cutoff,
            "last_trigger": trigger,
            "affected_task_ids": sorted(affected),
        })
        self.observer.emit("plan.recomputed", {
            "plan_version": plan_version, "cutoff": cutoff,
            "affected": sorted(affected), "events_added": added,
            "unschedulable": [u.task_id for u in result.unschedulable],
            "trigger": trigger,
        })
        return {
            "changed": True,
            "plan_version": plan_version,
            "prediction_version": prediction_version,
            "cutoff": cutoff,
            "trigger": trigger,
            "affected_task_ids": sorted(affected),
            "segments": [s.to_dict() for s in result.segments],
            "events_added": added,
            "stats": result.stats(),
            "unschedulable": [u.to_dict() for u in result.unschedulable],
        }

    # ------------------------------------------------------------------ #
    # 双人批准
    # ------------------------------------------------------------------ #

    def submit_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id")
                         or self.id_gen.next_id("appr"))
        approver = str(payload["approver"])
        decision = ApprovalDecision(payload.get("decision", "approve"))
        task_id = str(payload["task_id"])
        scenario = self.scenarios.load_scenario()
        task = next((t for t in scenario["tasks"] if t.id == task_id), None)
        if task is None:
            raise ValueError(f"未知任务 {task_id}")
        resource_id = str(payload["resource_id"])
        if resource_id not in {r.id for r in scenario["resources"]}:
            raise ValueError(f"未知资源 {resource_id}")
        start = int(payload.get("start", self.clock.now()))
        end = int(payload["end"])
        if end <= start:
            raise ValueError("批准区间非法：end 必须大于 start")
        if start >= task.deadline:
            raise ValueError(
                f"批准开始 {start} 不在任务 {task_id} 截止 {task.deadline} 之前")
        reason = str(payload.get("reason", "人工特许占用紧急时隙"))

        existing = self.plans.get_approval(request_id)
        if existing is not None:
            # 同 request_id 的重复审批必须指向同一占用请求，防止借幂等键改内容。
            if (existing.resource_id != resource_id
                    or existing.start != start or existing.end != end
                    or existing.task_id != task_id):
                raise ValueError(
                    f"审批请求 {request_id} 已存在但任务/资源/时间不一致")
        if existing is None:
            record = ApprovalRecord(
                request_id=request_id, task_id=task_id,
                terminal_id=task.terminal_id, resource_id=resource_id,
                start=start, end=end, reason=reason,
            )
        else:
            record = existing
        approvers = set(record.approvers)
        if decision is ApprovalDecision.APPROVE:
            approvers.add(approver)
        granted = len(approvers) >= 2 and decision is ApprovalDecision.APPROVE
        updated = ApprovalRecord(
            request_id=request_id, task_id=task_id,
            terminal_id=task.terminal_id, resource_id=resource_id,
            start=start, end=end, reason=reason,
            approvers=frozenset(approvers), granted=granted,
        )
        saved = self.plans.put_approval(updated)
        self.observer.emit("approval.submitted", {
            "request_id": request_id, "approver": approver,
            "decision": decision.value, "granted": saved.granted,
        })

        was_granted = existing.granted if existing else False
        if saved.granted and not was_granted:
            # 生效：受益任务 + 该资源批准区间内的紧急属主任务一起重算。
            displaced = self._emergency_tasks_on(resource_id, start, end)
            recomputed = self.recompute(
                cutoff=self.clock.now(),
                reason_terminals=set(),
                prediction_version=self.scenarios.latest_version(),
                trigger="approval_override",
                extra_affected={task_id} | displaced,
            )
            response = saved.to_dict()
            response["recompute"] = recomputed
            return response
        return saved.to_dict()

    def _emergency_tasks_on(self, resource_id: str, start: int, end: int) -> set[str]:
        out: set[str] = set()
        for lock in self.plans.list_locks():
            if lock.resource_id != resource_id:
                continue
            if lock.end > start and lock.start < end:
                out.add(lock.task_id)
        return out

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def get_plan(self, task_id: str | None = None) -> dict[str, Any]:
        segments = self.plans.list_segments(task_id)
        events = self.plans.list_events()
        if task_id is not None:
            events = [e for e in events if e.task_id == task_id]
        meta = self.plans.get_plan_meta()
        return {
            "meta": meta,
            "segments": [s.to_dict() for s in segments],
            "events": [e.to_dict() for e in events],
            "locks": [l.to_dict() for l in self.plans.list_locks()
                      if task_id is None or l.task_id == task_id],
            "approvals": [a.to_dict() for a in self.plans.list_approvals()],
        }

    def get_windows(self, version: int | None = None) -> dict[str, Any]:
        ws = self.scenarios.list_windows(version)
        return {
            "version": version if version is not None
            else self.scenarios.latest_version(),
            "windows": [w.to_dict() for w in ws],
        }

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _affected_tasks(tasks, cutoff, reason_terminals, extra):
        if reason_terminals is None and not extra:
            return {t.id for t in tasks if t.deadline > cutoff}
        affected = set(extra)
        if reason_terminals is not None:
            for t in tasks:
                if t.deadline > cutoff and t.terminal_id in reason_terminals:
                    affected.add(t.id)
        return affected

    def _classify_segments(self, cutoff, affected):
        """把现有计划段分成冻结前缀、固定占用、跨 cutoff 连续性三类。"""
        frozen: list[PlanSegment] = []
        fixed: list[PlanSegment] = []
        continuity: dict[str, tuple[str, str]] = {}
        for seg in self.plans.list_segments():
            if seg.task_id in affected:
                if seg.end <= cutoff:
                    frozen.append(seg)
                elif seg.start < cutoff:
                    # 跨 cutoff 段：前半截冻结，连续性由其状态决定。
                    head = PlanSegment(
                        task_id=seg.task_id, terminal_id=seg.terminal_id,
                        start=seg.start, end=cutoff, state=seg.state,
                        resource_id=seg.resource_id, reason=seg.reason,
                        predicted_version=seg.predicted_version,
                    )
                    frozen.append(head)
                    if seg.state is SegmentState.LINK and seg.resource_id:
                        continuity[seg.terminal_id] = (
                            seg.resource_id, seg.task_id
                        )
            else:
                fixed.append(seg)
        return frozen, fixed, continuity

    def _truncate_crossing_segments(self, cutoff, affected) -> None:
        """把受影响任务跨 cutoff 的段在仓储中截到 [.., cutoff)。"""
        current = self.plans.list_segments()
        replacement: list[PlanSegment] = []
        changed = False
        for seg in current:
            if (seg.task_id in affected and seg.start < cutoff < seg.end):
                changed = True
                replacement.append(PlanSegment(
                    task_id=seg.task_id, terminal_id=seg.terminal_id,
                    start=seg.start, end=cutoff, state=seg.state,
                    resource_id=seg.resource_id, reason=seg.reason,
                    predicted_version=seg.predicted_version,
                ))
            else:
                replacement.append(seg)
        if changed:
            self.plans.save_segments(replacement,
                                     self.plans.get_plan_meta()["plan_version"])
