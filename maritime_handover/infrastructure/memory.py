"""内存仓储：服务状态的唯一持有者。

所有运行数据集中在 MemoryStore，便于：
- 应用服务通过同一对象读写；
- JsonFileStore 以快照方式整体持久化；
- 测试直接构造干净状态。
"""
from __future__ import annotations

from ..domain.models import (
    Approval,
    ExclusionGroup,
    LinkWindow,
    Plan,
    Task,
    Terminal,
)


class MemoryStore:
    def __init__(self) -> None:
        # 链路窗口：link_id -> 当前生效窗口列表（按 start 升序、互不重叠）
        self.windows: dict[str, list[LinkWindow]] = {}
        self.window_versions: dict[str, int] = {}
        # 船位轨迹：vessel_id -> (tick, lat, lon) 升序列表
        self.tracks: dict[str, list[tuple[int, float, float]]] = {}
        self.track_versions: dict[str, int] = {}
        self.terminals: dict[str, Terminal] = {}
        self.exclusions: dict[str, ExclusionGroup] = {}
        self.tasks: dict[str, Task] = {}
        self.cancelled: set[str] = set()
        self.confirmed: set[str] = set()
        self.approvals: dict[str, Approval] = {}
        # 请求级幂等：request_id -> 已返回的响应
        self.idempotency: dict[str, dict] = {}
        # 计划历史：按版本递增
        self.plans: list[Plan] = []
        self.now: int = 0
        # 增量重算的脏标记
        self.dirty_tasks: set[str] = set()
        self.dirty_link_ranges: dict[str, list[tuple[int, int]]] = {}
        self.dirty_vessel_ranges: dict[str, list[tuple[int, int]]] = {}

    def save(self) -> None:
        """持久化钩子：内存仓储无操作，文件仓储覆盖实现。"""
