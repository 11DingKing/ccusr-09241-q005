"""领域模型：链路资源、终端、任务、覆盖窗口、计划段与双人批准。

时间全部使用整数刻度（discrete ticks，从计划原点 0 起算）。
区间统一采用左闭右开 ``[start, end)``：占用 ``[a, b)`` 与 ``[b, c)``
在时刻 ``b`` 首尾相接，视为无缝交接，不计中断。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .enums import EventType, LinkKind, Reason, SegmentState


@dataclass(frozen=True)
class Resource:
    """天地链路资源：卫星波束或近岸地面覆盖扇区。

    capacity 为该资源同一时刻可容纳的终端数量；
    mutex_group 非空时，同组资源对同一终端互斥（不可同时占用）。
    """

    id: str
    kind: LinkKind
    name: str
    capacity: int = 1
    mutex_group: str | None = None

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"资源 {self.id} 容量必须 >= 1")
        if not self.id:
            raise ValueError("资源 id 不能为空")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "capacity": self.capacity,
            "mutex_group": self.mutex_group,
        }


@dataclass(frozen=True)
class Terminal:
    """船载终端。

    cooldown 为切换冷却刻度数：终端离开某链路后，必须等待
    该时长才允许接入另一条链路（回到原链路同样受限）。
    """

    id: str
    name: str
    cooldown: int = 0
    supported_kinds: frozenset[LinkKind] = frozenset(
        {LinkKind.SATELLITE, LinkKind.TERRESTRIAL}
    )

    def __post_init__(self) -> None:
        if self.cooldown < 0:
            raise ValueError(f"终端 {self.id} 冷却不能为负")

    def supports(self, resource: Resource) -> bool:
        return resource.kind in self.supported_kinds

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "cooldown": self.cooldown,
            "supported_kinds": sorted(k.value for k in self.supported_kinds),
        }


@dataclass(frozen=True)
class Task:
    """通信任务。

    - priority 数值越小优先级越高（1 最高）；
    - emergency=True 的任务在确认时刻之后的时隙受紧急保护；
    - demand 为需要的通信量（刻度数），在 [release, deadline) 内累计完成；
    - min_hold 为最短保持时间：任何一次接入若不能持续至少 min_hold
      个刻度，调度器宁可不接入而继续等待。
    """

    id: str
    terminal_id: str
    release: int
    deadline: int
    demand: int
    priority: int
    min_hold: int = 1
    emergency: bool = False
    name: str = ""

    def __post_init__(self) -> None:
        if self.release < 0 or self.deadline <= self.release:
            raise ValueError(f"任务 {self.id} 时间窗非法: release={self.release} deadline={self.deadline}")
        if self.demand <= 0:
            raise ValueError(f"任务 {self.id} 通信量必须为正")
        if self.priority < 1:
            raise ValueError(f"任务 {self.id} 优先级必须 >= 1")
        if self.min_hold < 1:
            raise ValueError(f"任务 {self.id} 最短保持时间必须 >= 1")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "terminal_id": self.terminal_id,
            "name": self.name or self.id,
            "release": self.release,
            "deadline": self.deadline,
            "demand": self.demand,
            "priority": self.priority,
            "min_hold": self.min_hold,
            "emergency": self.emergency,
        }


@dataclass(frozen=True)
class Window:
    """预测覆盖窗口：终端在 [start, end) 可接入资源 resource_id。

    version 标识该窗口来自哪一版预测；修订版预测中保留的窗口
    会以相同 window_uid 出现，用于增量差量。
    """

    window_uid: str
    terminal_id: str
    resource_id: str
    start: int
    end: int
    version: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                f"窗口 {self.window_uid} 区间非法: [{self.start}, {self.end})"
            )

    @property
    def key(self) -> tuple[str, str, str]:
        """差量身份键：窗口主体 + 终端 + 资源。"""
        return (self.window_uid, self.terminal_id, self.resource_id)

    def to_dict(self) -> dict:
        return {
            "window_uid": self.window_uid,
            "terminal_id": self.terminal_id,
            "resource_id": self.resource_id,
            "start": self.start,
            "end": self.end,
            "version": self.version,
        }


@dataclass
class PlanSegment:
    """计划段：任务在 [start, end) 内的状态。

    state=LINK 时 resource_id 给出占用链路；state=WAIT 时为等待，
    reason 说明等待原因。首尾相接的 LINK 段若资源不同则构成一次交接。
    """

    task_id: str
    terminal_id: str
    start: int
    end: int
    state: SegmentState
    resource_id: str | None = None
    reason: Reason | None = None
    predicted_version: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "terminal_id": self.terminal_id,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "state": self.state.value,
            "resource_id": self.resource_id,
            "reason": self.reason.value if self.reason else None,
            "reason_label": self.reason.label if self.reason else None,
            "predicted_version": self.predicted_version,
        }


@dataclass
class TimelineEvent:
    """计划事件：在时刻 time 发生的一次接入/释放/交接/等待。

    幂等键 event_key 由 (任务, 类型, 时刻, 资源/原因) 派生，
    重复生成的相同事件不会重复入库。
    """

    time: int
    event_type: EventType
    task_id: str
    terminal_id: str
    resource_id: str | None
    reason: Reason
    detail: str = ""
    event_key: str = ""
    plan_version: int = 1

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "event_type": self.event_type.value,
            "task_id": self.task_id,
            "terminal_id": self.terminal_id,
            "resource_id": self.resource_id,
            "reason": self.reason.value,
            "reason_label": self.reason.label,
            "detail": self.detail,
            "event_key": self.event_key,
            "plan_version": self.plan_version,
        }


@dataclass(frozen=True)
class EmergencyLock:
    """紧急时隙保护：紧急任务在 [start, end) 锁定某终端的链路占用。

    普通任务不得占用被锁的 (终端, 互斥域)；要夺走必须取得双人批准
    （见 ApprovalRecord）。
    """

    task_id: str
    terminal_id: str
    resource_id: str
    start: int
    end: int
    mutex_domain: str  # 资源 id 或互斥组名

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "terminal_id": self.terminal_id,
            "resource_id": self.resource_id,
            "start": self.start,
            "end": self.end,
            "mutex_domain": self.mutex_domain,
        }


@dataclass(frozen=True)
class ApprovalRecord:
    """双人批准记录（四眼原则）。

    request_id 相同的重复审批请求幂等：两名不同批准人齐备才生效，
    同一批准人重复提交不增加计数。
    """

    request_id: str
    task_id: str
    terminal_id: str
    resource_id: str
    start: int
    end: int
    reason: str
    approvers: frozenset[str] = field(default_factory=frozenset)
    granted: bool = False

    def with_approver(self, approver: str) -> "ApprovalRecord":
        return replace(self, approvers=self.approvers | {approver})

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "terminal_id": self.terminal_id,
            "resource_id": self.resource_id,
            "start": self.start,
            "end": self.end,
            "reason": self.reason,
            "approvers": sorted(self.approvers),
            "granted": self.granted,
        }
