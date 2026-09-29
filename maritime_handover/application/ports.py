"""可替换端口：时钟、标识生成、外部观测与仓储抽象。

领域与应用服务只依赖这些接口；具体实现（系统时钟、内存仓储、
JSON 文件仓储、收集型观测器）放在 adapters 层，便于在测试与
模拟器中稳定复现业务过程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable

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


class Clock(ABC):
    """时间端口：返回当前确认时刻（离散刻度或墙钟毫秒由实现决定）。"""

    @abstractmethod
    def now(self) -> int:
        raise NotImplementedError


class IdGenerator(ABC):
    """标识生成端口：审批请求、计划批次等需要稳定唯一标识。"""

    @abstractmethod
    def next_id(self, prefix: str) -> str:
        raise NotImplementedError


class Observer(ABC):
    """外部观测端口：调度与模拟过程中的关键动作都经由此端口发出。"""

    @abstractmethod
    def emit(self, topic: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError


class ScenarioRepository(ABC):
    """场景数据仓储：资源、终端、任务、分版本窗口。"""

    @abstractmethod
    def save_scenario(
        self,
        resources: Iterable[Resource],
        terminals: Iterable[Terminal],
        tasks: Iterable[Task],
        horizon: int,
        group_capacities: dict[str, int] | None = None,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def load_scenario(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def put_windows(self, windows: Iterable[Window]) -> dict[str, Any]:
        """写入一版窗口预测；返回差量摘要 {version, added, removed, changed}。"""
        raise NotImplementedError

    @abstractmethod
    def list_windows(self, version: int | None = None) -> list[Window]:
        raise NotImplementedError

    @abstractmethod
    def latest_version(self) -> int:
        raise NotImplementedError


class PlanRepository(ABC):
    """计划仓储：计划段、事件、紧急锁、审批与幂等键。"""

    @abstractmethod
    def save_segments(self, segments: Iterable[PlanSegment], plan_version: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_segments(self, task_id: str | None = None) -> list[PlanSegment]:
        raise NotImplementedError

    @abstractmethod
    def replace_future(
        self,
        segments: Iterable[PlanSegment],
        cutoff: int,
        affected_task_ids: Iterable[str],
        plan_version: int,
    ) -> None:
        """增量重算：删除受影响任务 cutoff 之后的旧段，替换为新段。

        cutoff 之前（已确认）的段与无关节点的段原样保留。
        """
        raise NotImplementedError

    @abstractmethod
    def save_events(self, events: Iterable[TimelineEvent]) -> int:
        """按 event_key 幂等写入，返回新增事件数。"""
        raise NotImplementedError

    @abstractmethod
    def drop_future_events(
        self, cutoff: int, affected_task_ids: Iterable[str]
    ) -> int:
        """丢弃受影响任务尚未执行（time>=cutoff）的旧事件。"""
        raise NotImplementedError

    @abstractmethod
    def list_events(self) -> list[TimelineEvent]:
        raise NotImplementedError

    @abstractmethod
    def save_locks(self, locks: Iterable[EmergencyLock]) -> None:
        raise NotImplementedError

    @abstractmethod
    def replace_locks(
        self,
        locks: Iterable[EmergencyLock],
        affected_task_ids: Iterable[str],
        cutoff: int,
    ) -> None:
        """用新锁整体替换受影响任务 cutoff 之后的锁，其余保留。"""
        raise NotImplementedError

    @abstractmethod
    def list_locks(self) -> list[EmergencyLock]:
        raise NotImplementedError

    @abstractmethod
    def put_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        """幂等写入/更新审批记录（同 request_id 不重复创建）。"""
        raise NotImplementedError

    @abstractmethod
    def get_approval(self, request_id: str) -> ApprovalRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_approvals(self) -> list[ApprovalRecord]:
        raise NotImplementedError

    @abstractmethod
    def seen_idempotency_key(self, key: str, response: Any) -> bool:
        """首次返回 False 并记录；同键再次出现返回 True（重复请求幂等）。"""
        raise NotImplementedError

    @abstractmethod
    def stored_response(self, key: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    def set_plan_meta(self, meta: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_plan_meta(self) -> dict[str, Any]:
        raise NotImplementedError
