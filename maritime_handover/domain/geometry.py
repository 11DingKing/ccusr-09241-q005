"""地理位置工具：大圆距离与船位轨迹插值。"""
from __future__ import annotations

import math
from typing import Optional, Sequence

#: 轨迹点统一为 (tick, lat, lon) 三元组，按 tick 升序。
TrackPointTuple = tuple[int, float, float]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点大圆距离（公里）。"""
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def interpolate_track(
    points: Sequence[TrackPointTuple], tick: int
) -> Optional[tuple[float, float]]:
    """按 tick 线性插值船位；轨迹范围之外返回 None（视为位置未知）。"""
    if not points or tick < points[0][0] or tick > points[-1][0]:
        return None
    prev = points[0]
    if tick == prev[0]:
        return (prev[1], prev[2])
    for point in points[1:]:
        if point[0] == tick:
            return (point[1], point[2])
        if point[0] > tick:
            t0, lat0, lon0 = prev
            t1, lat1, lon1 = point
            frac = (tick - t0) / (t1 - t0)
            return (lat0 + frac * (lat1 - lat0), lon0 + frac * (lon1 - lon0))
        prev = point
    last = points[-1]
    return (last[1], last[2])
