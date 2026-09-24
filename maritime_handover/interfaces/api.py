"""面向计划员的本地 JSON API（仅依赖标准库）。

运行方式：
    python3 -m maritime_handover.interfaces.api --host 127.0.0.1 --port 8080 \
        [--data-file state.json]

所有变更类请求都接受可选的 "request_id" 字段用于幂等重放。
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from ..application.services import DomainError, HandoverService
from ..infrastructure.json_store import JsonFileStore

RouteHandler = Callable[[dict, dict], tuple[int, dict]]


class Router:
    def __init__(self, service: HandoverService):
        self.service = service
        self.routes: list[tuple[str, re.Pattern, RouteHandler]] = []
        self._register()

    def _register(self) -> None:
        svc = self.service

        def body_request_id(body: dict) -> Optional[str]:
            rid = body.get("request_id")
            return None if rid is None else str(rid)

        self.add("GET", r"/v1/health", lambda p, b: (200, {"status": "ok"}))
        self.add(
            "GET",
            r"/v1/state/summary",
            lambda p, b: (200, svc.state_summary()),
        )
        self.add(
            "POST",
            r"/v1/links/(?P<link_id>[^/]+)/windows",
            lambda p, b: (
                200,
                svc.put_link_windows(
                    p["link_id"], b, request_id=body_request_id(b)
                ),
            ),
        )
        self.add(
            "POST",
            r"/v1/vessels/(?P<vessel_id>[^/]+)/tracks",
            lambda p, b: (
                200,
                svc.put_track(p["vessel_id"], b, request_id=body_request_id(b)),
            ),
        )
        self.add(
            "PUT",
            r"/v1/terminals/(?P<terminal_id>[^/]+)",
            lambda p, b: (
                200,
                svc.put_terminal(
                    {**b, "terminal_id": p["terminal_id"]},
                    request_id=body_request_id(b),
                ),
            ),
        )
        self.add(
            "PUT",
            r"/v1/exclusions/(?P<group_id>[^/]+)",
            lambda p, b: (
                200,
                svc.put_exclusion(
                    {**b, "group_id": p["group_id"]},
                    request_id=body_request_id(b),
                ),
            ),
        )
        self.add(
            "POST",
            r"/v1/tasks",
            lambda p, b: (200, svc.submit_task(b, request_id=body_request_id(b))),
        )
        self.add(
            "POST",
            r"/v1/tasks/(?P<task_id>[^/]+)/confirm",
            lambda p, b: (
                200,
                svc.confirm_task(p["task_id"], request_id=body_request_id(b)),
            ),
        )
        self.add(
            "POST",
            r"/v1/tasks/(?P<task_id>[^/]+)/cancel",
            lambda p, b: (
                200,
                svc.cancel_task(p["task_id"], request_id=body_request_id(b)),
            ),
        )
        self.add(
            "GET",
            r"/v1/tasks/(?P<task_id>[^/]+)/schedule",
            lambda p, b: (200, svc.task_schedule(p["task_id"])),
        )
        self.add(
            "POST",
            r"/v1/approvals",
            lambda p, b: (200, svc.approve(b, request_id=body_request_id(b))),
        )
        self.add(
            "POST",
            r"/v1/time",
            lambda p, b: (200, svc.advance_time(int(b["now"]))),
        )
        self.add("POST", r"/v1/replan", lambda p, b: (200, svc.replan()))
        self.add(
            "GET",
            r"/v1/plans/current",
            lambda p, b: (200, svc.current_plan_view()),
        )
        self.add(
            "GET",
            r"/v1/plans/(?P<version>\d+)",
            lambda p, b: (200, svc.plan_view(int(p["version"]))),
        )

    def add(self, method: str, pattern: str, handler: RouteHandler) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), handler))

    def dispatch(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                return handler(match.groupdict(), body)
        raise DomainError("not_found", f"路由不存在: {method} {path}", status=404)


def make_handler(router: Router) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MaritimeHandover/1.0"
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        raise DomainError(
                            "bad_json", f"请求体不是合法 JSON: {exc}", status=400
                        )
                    if not isinstance(body, dict):
                        raise DomainError(
                            "bad_json", "请求体必须是 JSON 对象", status=400
                        )
                else:
                    body = {}
                status, payload = router.dispatch(
                    self.command, self.path.split("?", 1)[0], body
                )
            except DomainError as exc:
                status, payload = exc.status, exc.to_dict()
            except (KeyError, ValueError, TypeError) as exc:
                status, payload = 400, {
                    "error": {"code": "bad_request", "message": str(exc)}
                }
            except Exception as exc:  # noqa: BLE001 - 接口层兜底
                status, payload = 500, {
                    "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}
                }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle

        def log_message(self, format: str, *args) -> None:  # 静默访问日志
            return

    return Handler


def build_service(data_file: Optional[str] = None) -> HandoverService:
    if data_file:
        return HandoverService(JsonFileStore(data_file))
    return HandoverService()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="海上任务天地链路交接计划器 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-file", default=None, help="状态快照 JSON 文件路径")
    args = parser.parse_args(argv)

    service = build_service(args.data_file)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(Router(service))
    )
    print(f"listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
