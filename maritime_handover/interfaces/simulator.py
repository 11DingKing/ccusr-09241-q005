"""离散时间模拟器：从 JSON 场景文件驱动服务逐 tick 运行并输出核对报告。

运行方式：
    python3 -m maritime_handover.interfaces.simulator examples/scenario_basic.json \
        [--out report.json]

场景文件格式见 examples/ 目录。报告包含：
- plans：每次重算产生的计划版本；
- events：最终计划的接入 / 释放 / 交接事件（含时间与原因）；
- actions：脚本动作的执行回执（可核对幂等重放）；
- checks：无缝交接、容量守恒、冲突等待、无解任务说明四类核对。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from ..application.services import HandoverService
from ..domain import checks
from ..infrastructure.serialization import event_to_dict
from ..domain.planner import derive_events


def _apply_action(service: HandoverService, action: dict, request_id: str) -> dict:
    kind = action["action"]
    if kind == "link_windows":
        return service.put_link_windows(
            action["link_id"], action, request_id=request_id
        )
    if kind == "track":
        return service.put_track(action["vessel_id"], action, request_id=request_id)
    if kind == "terminal":
        return service.put_terminal(action["terminal"], request_id=request_id)
    if kind == "exclusion":
        return service.put_exclusion(action["exclusion"], request_id=request_id)
    if kind == "task":
        return service.submit_task(action["task"], request_id=request_id)
    if kind == "confirm":
        return service.confirm_task(action["task_id"], request_id=request_id)
    if kind == "cancel":
        return service.cancel_task(action["task_id"], request_id=request_id)
    if kind == "approve":
        return service.approve(action, request_id=request_id)
    raise ValueError(f"未知脚本动作: {kind}")


def run_scenario(scenario: dict) -> dict:
    name = scenario.get("name", "scenario")
    tick_start = int(scenario["ticks"]["start"])
    tick_end = int(scenario["ticks"]["end"])
    service = HandoverService()
    log: list[dict] = []

    def apply(action: dict, index: int) -> None:
        response = _apply_action(service, action, f"sim:{name}:{index}")
        log.append(
            {
                "at": action.get("at", tick_start),
                "action": action["action"],
                "status": response.get("status", "ok"),
                "idempotent_replay": response.get("idempotent_replay", False),
            }
        )

    service.advance_time(tick_start)
    index = 0
    for terminal in scenario.get("terminals", []):
        apply({"action": "terminal", "terminal": terminal}, index)
        index += 1
    for exclusion in scenario.get("exclusions", []):
        apply({"action": "exclusion", "exclusion": exclusion}, index)
        index += 1
    for link in scenario.get("links", []):
        apply({"action": "link_windows", **link}, index)
        index += 1
    for track in scenario.get("tracks", []):
        apply({"action": "track", **track}, index)
        index += 1
    for task in scenario.get("tasks", []):
        apply({"action": "task", "task": task}, index)
        index += 1

    script = sorted(scenario.get("script", []), key=lambda a: int(a["at"]))
    for tick in range(tick_start, tick_end + 1):
        for action in [a for a in script if int(a["at"]) == tick]:
            apply(action, index)
            index += 1
        if tick < tick_end:
            service.advance_time(tick + 1)

    store = service.store
    plan = store.plans[-1]
    sessions = list(plan.sessions)
    tasks = store.tasks
    events = derive_events(sessions, tasks, store.confirmed, store.now)
    tick_range = range(tick_start, tick_end + 1)

    seamless = checks.check_seamless(sessions, tasks)
    capacity = checks.check_capacity(
        sessions, store.windows, list(store.exclusions.values()), tick_range
    )
    waiting = checks.check_waiting(tasks, sessions)
    infeasible = checks.check_infeasible(list(plan.gaps))

    return {
        "scenario": name,
        "ticks": {"start": tick_start, "end": tick_end},
        "actions": log,
        "plans": [
            {
                "version": p.version,
                "generated_at": p.generated_at,
                "notes": list(p.notes),
            }
            for p in store.plans
        ],
        "events": [event_to_dict(e) for e in events],
        "checks": {
            "seamless_handover": seamless,
            "capacity_conservation": capacity,
            "conflict_waiting": waiting,
            "infeasible_tasks": infeasible,
        },
        "ok": seamless["ok"] and capacity["ok"],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="天地链路交接离散时间模拟器")
    parser.add_argument("scenario", help="场景 JSON 文件路径")
    parser.add_argument("--out", default=None, help="报告输出路径（默认标准输出）")
    args = parser.parse_args(argv)

    with open(args.scenario, "r", encoding="utf-8") as handle:
        scenario = json.load(handle)
    report = run_scenario(scenario)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"report written to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
