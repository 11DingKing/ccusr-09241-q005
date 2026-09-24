"""领域模型：链路窗口、船位、终端、互斥、任务、会话与计划。

时间统一使用整数 tick，区间均为左闭右开 [start, end)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

#: 优先级达到该值的任务视为紧急任务。
EMERGENCY_PRIORITY = 100

#: 表示“无穷远”的 tick，用于受影响区间标记。
INF = 10**12


class LinkKind(str, Enum):
    SATELLITE = "satellite"
    GROUND = "ground"


@dataclass(frozen=True)
class Footprint:
    """链路窗口的覆盖 footprint（圆心 + 半径）。"""

    lat: float
    lon: float
    radius_km: float


@dataclass(frozen=True)
class LinkWindow:
    """某条链路（卫星波束或地面站）在一个版本下的一段可用窗口。"""

    window_id: str
    link_id: str
    kind: LinkKind
    band: str
    start: int
    end: int
    capacity: int
    footprint: Footprint
    version: int

    def covers(self, tick: int) -> bool:
        return self.start <= tick < self.end


@dataclass(frozen=True)
class Terminal:
    """船载终端：支持的频段集合与切换冷却（tick 数）。"""

    terminal_id: str
    vessel_id: str
    bands: tuple[str, ...]
    cooldown: int


@dataclass(frozen=True)
class ExclusionGroup:
    """不可同时占用关系：组内链路在任一 tick 的活动会话数不得超过 limit。"""

    group_id: str
    link_ids: tuple[str, ...]
    limit: int = 1


@dataclass(frozen=True)
class Task:
    """通信任务：在 [start, end) 内需要连续链路保持。"""

    task_id: str
    vessel_id: str
    priority: int
    start: int
    end: int
    min_hold: int
    submitted_at: int = 0

    @property
    def emergency(self) -> bool:
        return self.priority >= EMERGENCY_PRIORITY


class SessionEnd(str, Enum):
    """会话结束原因（即释放/交接事件的原因码）。"""

    TASK_COMPLETE = "task_complete"
    WINDOW_END = "window_end"
    COVERAGE_LOST = "coverage_lost"
    CAPACITY_BLOCKED = "capacity_blocked"
    EXCLUSION_BLOCKED = "exclusion_blocked"
    TERMINAL_BLOCKED = "terminal_blocked"
    PREEMPTED = "preempted"
    LINK_LOST = "link_lost"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Session:
    """一段链路占用：某任务的某终端在 [start, end) 内占用某链路一个容量单位。"""

    task_id: str
    terminal_id: str
    link_id: str
    start: int
    end: int
    end_reason: str
    priority: int = 0
    locked: bool = False  # 已确认的紧急时隙：无双人批准不得被抢占

    def active_at(self, tick: int) -> bool:
        return self.start <= tick < self.end


class EventKind(str, Enum):
    ACCESS = "access"
    RELEASE = "release"
    HANDOVER = "handover"


class EventStatus(str, Enum):
    PLANNED = "planned"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"


@dataclass(frozen=True)
class PlanEvent:
    """计划事件：一次接入、释放或交接，带时间与原因。"""

    event_id: str
    tick: int
    kind: EventKind
    task_id: str
    terminal_id: str
    link_id: str
    to_link_id: Optional[str]
    reason: str
    status: EventStatus


@dataclass(frozen=True)
class Gap:
    """任务需求区间内未能覆盖的区段及原因。"""

    task_id: str
    start: int
    end: int
    reason: str


@dataclass(frozen=True)
class Plan:
    """一个计划版本：会话集合 + 未覆盖区段 + 备注。事件由会话推导（见 planner）。"""

    version: int
    generated_at: int
    sessions: tuple[Session, ...]
    gaps: tuple[Gap, ...]
    notes: tuple[str, ...]


@dataclass
class Approval:
    """双人批准：允许 requester_task_id 指定的任务占用 task_id 的已确认时隙。

    两名不同审批人提交同一 approval_id 后视为批准生效；重复提交幂等。
    """

    approval_id: str
    task_id: str
    requester_task_id: str
    decision: str = "allow_preempt"
    approvers: list[str] = field(default_factory=list)

    @property
    def granted(self) -> bool:
        return len(set(self.approvers)) >= 2
