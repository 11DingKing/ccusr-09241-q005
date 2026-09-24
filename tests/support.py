"""测试共享构造工具。"""
from __future__ import annotations

from maritime_handover.application.services import HandoverService

FOOTPRINT = {"lat": 10.0, "lon": 100.0, "radius_km": 500}


def window(link_id: str, start: int, end: int, **overrides) -> dict:
    data = {
        "kind": "satellite",
        "band": "ku",
        "start": start,
        "end": end,
        "capacity": 1,
        "footprint": dict(FOOTPRINT),
    }
    data.update(overrides)
    return data


def make_service(
    *,
    links: dict[str, list[dict]] | None = None,
    vessels: list[str] | None = None,
    terminals: list[dict] | None = None,
    exclusions: list[dict] | None = None,
    tracks: dict[str, tuple[float, float]] | None = None,
    now: int = 0,
) -> HandoverService:
    """构造一个带基础资源的服务实例。"""
    service = HandoverService()
    service.advance_time(now)
    vessels = vessels or ["V1"]
    for vessel_id in vessels:
        lat, lon = (tracks or {}).get(vessel_id, (10.0, 100.0))
        service.put_track(
            vessel_id,
            {
                "version": 1,
                "points": [
                    {"tick": 0, "lat": lat, "lon": lon},
                    {"tick": 1000, "lat": lat, "lon": lon},
                ],
            },
        )
    for terminal in terminals or [
        {"terminal_id": "T1", "vessel_id": "V1", "bands": ["ku"], "cooldown": 0}
    ]:
        service.put_terminal(terminal)
    for group in exclusions or []:
        service.put_exclusion(group)
    for link_id, windows in (links or {}).items():
        service.put_link_windows(link_id, {"version": 1, "windows": windows})
    return service


def sessions_of(service: HandoverService, task_id: str) -> list[tuple[str, int, int, str]]:
    plan = service.store.plans[-1]
    return [
        (s.link_id, s.start, s.end, s.end_reason)
        for s in plan.sessions
        if s.task_id == task_id
    ]


def gaps_of(service: HandoverService, task_id: str) -> list[tuple[int, int, str]]:
    plan = service.store.plans[-1]
    return [
        (g.start, g.end, g.reason) for g in plan.gaps if g.task_id == task_id
    ]
