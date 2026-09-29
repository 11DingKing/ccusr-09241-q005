"""可从文件运行的离散时间模拟器。

输入文件格式::

    {
      "scenario": {"resources": [], "terminals": [], "tasks": [], "horizon": 12},
      "predictions": [
        {"at": 0, "version": 1, "windows": [ ... ]},
        {"at": 5, "version": 2, "windows": [ ... ]}
      ],
      "approvals": [
        {"at": 6, "request_id": "R1", "approver": "甲", "task_id": "N",
         "resource_id": "SAT1", "start": 6, "end": 9}
      ]
    }

模拟器从 0 逐拍推进到 horizon：在每拍先应用该时刻到达的预测版本
（自动触发只影响相关区段的增量重算）与双人批准提交，再记录快照。
最终输出时间线、段、锁、无解说明与四项核对结果。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..adapters.drivers import FixedClock, ListObserver, SequentialIdGenerator
from ..adapters.memory_repo import InMemoryRepository
from ..application.planning_service import PlanningService
from ..domain.enums import SegmentState
from ..domain.models import Window
from .verifier import Verifier


@dataclass
class SimulationResult:
    report: dict[str, Any]


def run_simulation(spec: dict[str, Any]) -> dict[str, Any]:
    scenario = spec["scenario"]
    horizon = int(scenario.get("horizon", 0))
    clock = FixedClock(0)
    observer = ListObserver()
    repo = InMemoryRepository()
    service = PlanningService(
        scenarios=repo, plans=repo, clock=clock,
        id_gen=SequentialIdGenerator(), observer=observer,
    )
    service.setup_scenario(scenario)

    predictions = sorted(spec.get("predictions", []),
                         key=lambda p: (int(p["at"]), int(p["version"])))
    approvals = sorted(spec.get("approvals", []), key=lambda a: int(a["at"]))
    pred_at = {int(p["at"]): p for p in predictions}
    appr_at: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for a in approvals:
        appr_at[int(a["at"])].append(a)

    snapshots: list[dict[str, Any]] = []
    applied_versions: list[tuple[int, int]] = []  # (at, version)
    first_generated = False
    # 任务 id -> 最新一版无解说明（引擎给出的中文解释）。
    unschedulable_notes: dict[str, dict[str, Any]] = {}

    def _collect_notes(result: dict[str, Any]) -> None:
        for tid in result.get("affected_task_ids", ()):
            unschedulable_notes.pop(tid, None)
        for row in result.get("unschedulable", ()):
            unschedulable_notes[row["task_id"]] = row

    for t in range(0, horizon + 1):
        clock.set(t)
        if t in pred_at:
            p = pred_at[t]
            ingest = service.ingest_prediction(p)
            applied_versions.append((t, int(p["version"])))
            if not first_generated:
                _collect_notes(service.generate_initial_plan())
                first_generated = True
            else:
                _collect_notes(ingest)
        for a in appr_at.get(t, ()):
            out = service.submit_approval(a)
            if "recompute" in out:
                _collect_notes(out["recompute"])
        if t < horizon:
            snapshots.append(_snapshot(service, t, repo))

    if not first_generated and predictions:
        _collect_notes(service.generate_initial_plan())

    loaded = service.scenarios.load_scenario()
    resources = {r.id: r for r in loaded["resources"]}
    terminals = {x.id: x for x in loaded["terminals"]}
    tasks = {x.id: x for x in loaded["tasks"]}

    segments = repo.list_segments()
    events = repo.list_events()

    # 依据各预测版本的生效区间构造“实际生效窗口”集合，供核对使用。
    effective_windows = _effective_played_windows(
        repo, applied_versions, horizon,
    )

    verifier = Verifier(
        resources=resources, terminals=terminals, tasks=tasks,
        windows=effective_windows, segments=segments, events=events,
        horizon=horizon,
    )
    verification = verifier.verify_all()

    plan = service.get_plan()
    report = {
        "summary": {
            "horizon": horizon,
            "prediction_versions": [
                {"at": at, "version": v} for at, v in applied_versions
            ],
            "final_plan_version": repo.get_plan_meta().get("plan_version"),
            "tasks": len(tasks),
            "resources": len(resources),
            "terminals": len(terminals),
            "link_ticks": sum(
                s.length for s in segments if s.state is SegmentState.LINK
            ),
            "events": len(events),
            "locks": len(plan["locks"]),
            "granted_approvals": [
                a["request_id"] for a in plan["approvals"] if a["granted"]
            ],
            "verification_passed": all(
                v["passed"] for v in verification.values()
            ),
        },
        "timeline": [e.to_dict() for e in events],
        "segments": [s.to_dict() for s in segments],
        "locks": plan["locks"],
        "approvals": plan["approvals"],
        "snapshots": snapshots,
        "unschedulable_notes": sorted(
            unschedulable_notes.values(), key=lambda x: x["task_id"]),
        "verification": verification,
        "observations": observer.records,
    }
    return report


def _snapshot(service: PlanningService, t: int, repo: InMemoryRepository) -> dict[str, Any]:
    plan = service.get_plan()
    active_links = [
        {"task_id": s["task_id"], "terminal_id": s["terminal_id"],
         "resource_id": s["resource_id"]}
        for s in plan["segments"]
        if s["state"] == "link" and s["start"] <= t < s["end"]
    ]
    waiting = [
        {"task_id": s["task_id"], "reason": s["reason"]}
        for s in plan["segments"]
        if s["state"] == "wait" and s["start"] <= t < s["end"]
    ]
    return {"time": t, "links": active_links, "waiting": waiting}


def _effective_played_windows(
    repo: InMemoryRepository,
    applied_versions: list[tuple[int, int]],
    horizon: int,
) -> list[Window]:
    """每个版本在 [at, next_at) 内生效；将其窗口裁到生效区间后合并。"""
    if not applied_versions:
        return repo.list_windows()
    out: list[Window] = []
    for idx, (at, version) in enumerate(applied_versions):
        next_at = (applied_versions[idx + 1][0]
                   if idx + 1 < len(applied_versions) else horizon)
        for w in repo.list_windows(version):
            start = max(w.start, at)
            end = min(w.end, next_at)
            if start < end:
                out.append(Window(
                    window_uid=f"{w.window_uid}@v{version}",
                    terminal_id=w.terminal_id, resource_id=w.resource_id,
                    start=start, end=end, version=version,
                ))
    return out


def run_file(input_path: str, output_path: str | None = None) -> dict[str, Any]:
    with open(input_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    report = run_simulation(spec)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return report
