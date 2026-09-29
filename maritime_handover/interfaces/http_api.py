"""面向计划员的本地 JSON API（仅标准库 http.server）。

路由
----

- ``GET  /api/health``                健康检查
- ``POST /api/scenario``              建立场景（资源/终端/任务/视界）
- ``POST /api/predictions``           摄入一版窗口预测（版本须严格递增）
- ``POST /api/plans/generate``        生成初始计划
- ``POST /api/plans/recompute``       以当前时钟为确认时刻触发重算
- ``GET  /api/plan``                  查询完整计划（段/事件/锁/审批）
- ``GET  /api/plans/tasks/<id>``      查询单任务计划
- ``GET  /api/windows?version=n``     查询窗口版本
- ``POST /api/approvals``             提交双人批准（同 request_id 幂等）
- ``POST /api/clock/advance``         推进确认时钟 ``{"delta": 1}``
- ``POST /api/clock/set``             直接设置时钟 ``{"time": n}``
- ``GET  /api/observations``          读取过程观测记录

写操作支持 ``Idempotency-Key`` 头：同键重复提交直接返回首次结果，
不会重复排程或重复记账。
"""

from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..adapters.drivers import (
    FixedClock,
    ListObserver,
    SequentialIdGenerator,
)
from ..application.planning_service import PlanningService
from ..application.serialization import ValidationError


class ApiContext:
    def __init__(self, service: PlanningService, clock: FixedClock,
                 observer: ListObserver,
                 after_write: Callable[[], None] | None = None) -> None:
        self.service = service
        self.clock = clock
        self.observer = observer
        self.after_write = after_write


def _make_handler(ctx: ApiContext) -> type[BaseHTTPRequestHandler]:
    service = ctx.service
    clock = ctx.clock
    observer = ctx.observer
    idem_store = service.plans  # 复用仓储的幂等键能力
    after_write = ctx.after_write
    write_lock = threading.RLock()  # 串行化写操作（内存仓储非线程安全）

    class Handler(BaseHTTPRequestHandler):
        server_version = "MaritimeHandover/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        # -------------------------------------------------------------- #
        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise _HttpError(HTTPStatus.BAD_REQUEST, f"JSON 解析失败: {exc}")
            if not isinstance(data, dict):
                raise _HttpError(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")
            return data

        def _with_idempotency(self, fn: Callable[[], Any]) -> Any:
            key = self.headers.get("Idempotency-Key")
            if key:
                stored = idem_store.stored_response(key)
                if stored is not None:
                    stored = dict(stored)
                    stored["idempotent_replay"] = True
                    return stored
            result = fn()
            if key:
                idem_store.seen_idempotency_key(key, result)
            return result

        # -------------------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                qs = parse_qs(parsed.query)
                if path == "/api/health":
                    return self._send(HTTPStatus.OK, {
                        "status": "ok", "now": clock.now(),
                        "prediction_version": service.scenarios.latest_version()
                        if self._has_scenario() else 0,
                    })
                if path == "/api/plan":
                    return self._send(HTTPStatus.OK, service.get_plan())
                m = re.fullmatch(r"/api/plans/tasks/([^/]+)", path)
                if m:
                    return self._send(HTTPStatus.OK, service.get_plan(m.group(1)))
                if path == "/api/windows":
                    version = int(qs["version"][0]) if "version" in qs else None
                    return self._send(HTTPStatus.OK, service.get_windows(version))
                if path == "/api/observations":
                    return self._send(HTTPStatus.OK, {
                        "records": observer.records,
                    })
                raise _HttpError(HTTPStatus.NOT_FOUND, f"未知路径 {path}")
            except _HttpError as exc:
                self._send(exc.status, {"error": exc.message})
            except KeyError as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc).strip("'")})
            except Exception as exc:  # noqa: BLE001
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            try:
                path = urlparse(self.path).path.rstrip("/") or "/"
                data = self._read_json()
                if not _is_write_path(path):
                    raise _HttpError(HTTPStatus.NOT_FOUND, f"未知路径 {path}")
                # 所有写操作（含幂等键的检查-存储）在同一把锁内串行。
                with write_lock:
                    status, result = self._dispatch_write(path, data)
                if after_write is not None:
                    after_write()
                self._send(status, result)
            except _HttpError as exc:
                self._send(exc.status, {"error": exc.message})
            except ValidationError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (ValueError, KeyError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc).strip("'")})
            except Exception as exc:  # noqa: BLE001
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def _dispatch_write(self, path: str, data: dict[str, Any]):
            if path == "/api/scenario":
                return HTTPStatus.CREATED, self._with_idempotency(
                    lambda: service.setup_scenario(data))
            if path == "/api/predictions":
                return HTTPStatus.OK, self._with_idempotency(
                    lambda: service.ingest_prediction(data))
            if path == "/api/plans/generate":
                return HTTPStatus.OK, self._with_idempotency(
                    service.generate_initial_plan)
            if path == "/api/plans/recompute":
                return HTTPStatus.OK, self._with_idempotency(lambda: service.recompute(
                    cutoff=int(data.get("cutoff", clock.now())),
                    reason_terminals=None,
                    prediction_version=service.scenarios.latest_version(),
                    trigger=data.get("trigger", "manual"),
                ))
            if path == "/api/approvals":
                return HTTPStatus.OK, self._with_idempotency(
                    lambda: service.submit_approval(data))
            if path == "/api/clock/advance":
                now = clock.advance(int(data.get("delta", 1)))
                return HTTPStatus.OK, {"now": now}
            if path == "/api/clock/set":
                clock.set(int(data["time"]))
                return HTTPStatus.OK, {"now": clock.now()}
            raise _HttpError(HTTPStatus.NOT_FOUND, f"未知路径 {path}")

        def _has_scenario(self) -> bool:
            try:
                service.scenarios.load_scenario()
                return True
            except KeyError:
                return False

    def _is_write_path(path: str) -> bool:
        return path in {
            "/api/scenario", "/api/predictions", "/api/plans/generate",
            "/api/plans/recompute", "/api/approvals",
            "/api/clock/advance", "/api/clock/set",
        }

    return Handler


class _HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def build_server(host: str, port: int, ctx: ApiContext) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _make_handler(ctx))


def build_default_context(data_path: str | None = None) -> ApiContext:
    """组装默认装配：内存或 JSON 文件仓储 + 固定时钟 + 收集型观测。"""
    if data_path:
        from ..adapters.json_repo import JsonFileRepository

        repo = JsonFileRepository(data_path)
    else:
        from ..adapters.memory_repo import InMemoryRepository

        repo = InMemoryRepository()
    clock = FixedClock(0)
    observer = ListObserver()
    service = PlanningService(
        scenarios=repo, plans=repo, clock=clock,
        id_gen=SequentialIdGenerator(), observer=observer,
    )
    clock.set(int(repo.get_plan_meta().get("confirmed_at", 0)))
    return ApiContext(service, clock, observer)
