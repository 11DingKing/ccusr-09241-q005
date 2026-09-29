"""领域枚举：链路类型、计划事件类型与原因码。"""

from __future__ import annotations

from enum import Enum


class LinkKind(str, Enum):
    """链路资源类型。"""

    SATELLITE = "satellite"
    TERRESTRIAL = "terrestrial"


class EventType(str, Enum):
    """计划时间线上的事件类型。"""

    ACCESS = "access"        # 接入
    RELEASE = "release"      # 释放
    HANDOVER = "handover"    # 交接（同一时刻释放旧链路并接入新链路）
    WAIT = "wait"            # 进入等待（冲突等待）


class Reason(str, Enum):
    """事件与等待原因码，``label`` 为面向计划员的中文说明。"""

    TASK_RELEASE = ("task_release", "任务到达开始时刻，首次接入链路")
    WINDOW_START = ("window_start", "覆盖窗口开始，链路变为可用")
    WINDOW_END = ("window_end", "覆盖窗口结束，释放当前链路")
    WINDOW_END_HANDOVER = ("window_end_handover", "当前覆盖窗口结束，无缝交接至另一链路")
    TASK_COMPLETE = ("task_complete", "任务通信量已完成，释放链路")
    DEADLINE = ("deadline", "到达任务截止时间")
    HORIZON_END = ("horizon_end", "到达计划视界末端")
    CAPACITY_AVAILABLE = ("capacity_available", "波束容量出现空闲，重新接入")
    HIGHER_PRIORITY_PREEMPT = ("higher_priority_preempt", "更高优先级任务需要容量，交接至其他链路")
    PREEMPTED = ("preempted", "链路被更高优先级任务抢占，被迫释放")
    COOLDOWN = ("cooldown", "终端切换冷却尚未结束，无法接入")
    COOLDOWN_READY = ("cooldown_ready", "终端切换冷却结束，重新接入")
    CAPACITY_FULL = ("capacity_full", "波束容量已满，冲突等待")
    NO_WINDOW = ("no_window", "当前没有任何覆盖窗口，等待覆盖")
    MUTEX_BLOCKED = ("mutex_blocked", "互斥链路正在占用，冲突等待")
    EMERGENCY_LOCK = ("emergency_lock", "紧急时隙已确认锁定，普通任务禁止占用")
    APPROVAL_OVERRIDE = ("approval_override", "获得双人批准，按特许占用原紧急时隙")
    PREDICTION_UPDATE = ("prediction_update", "新版预测到达，区段重算后调整链路")

    def __new__(cls, code: str, label: str) -> "Reason":
        obj = str.__new__(cls, code)
        obj._value_ = code
        obj.label = label  # type: ignore[attr-defined]
        return obj


class SegmentState(str, Enum):
    """计划段状态：占用链路或等待。"""

    LINK = "link"
    WAIT = "wait"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
