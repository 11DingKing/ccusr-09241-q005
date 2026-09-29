"""驱动侧适配器：可复现的时钟、标识生成器与过程观测器。"""

from __future__ import annotations

import itertools
from typing import Any

from ..application.ports import Clock, IdGenerator, Observer


class FixedClock(Clock):
    """固定/可手动推进的时钟，用于测试与离散模拟。"""

    def __init__(self, start: int = 0) -> None:
        self._t = start

    def now(self) -> int:
        return self._t

    def advance(self, delta: int = 1) -> int:
        self._t += delta
        return self._t

    def set(self, value: int) -> None:
        self._t = value


class SequentialIdGenerator(IdGenerator):
    """``prefix-1``、``prefix-2``…… 的确定性标识生成器（按前缀独立计数）。"""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count] = {}

    def next_id(self, prefix: str) -> str:
        if prefix not in self._counters:
            self._counters[prefix] = itertools.count(1)
        return f"{prefix}-{next(self._counters[prefix])}"


class ListObserver(Observer):
    """把观测事件收集到内存列表，供断言与模拟器追踪使用。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, topic: str, payload: dict[str, Any]) -> None:
        self.records.append({"topic": topic, "payload": payload})

    def topics(self) -> list[str]:
        return [r["topic"] for r in self.records]

    def by_topic(self, topic: str) -> list[dict[str, Any]]:
        return [r["payload"] for r in self.records if r["topic"] == topic]
