"""领域层：链路交接排程的核心模型与枚举。"""

from .enums import (
    ApprovalDecision,
    EventType,
    LinkKind,
    Reason,
    SegmentState,
)
from .models import (
    ApprovalRecord,
    EmergencyLock,
    PlanSegment,
    Resource,
    Task,
    Terminal,
    TimelineEvent,
    Window,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalRecord",
    "EmergencyLock",
    "EventType",
    "LinkKind",
    "PlanSegment",
    "Reason",
    "Resource",
    "Task",
    "Terminal",
    "TimelineEvent",
    "Window",
    "SegmentState",
]
