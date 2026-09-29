"""命令行入口。

用法
----

启动本地 JSON API::

    python -m maritime_handover.cli serve --host 127.0.0.1 --port 8080 \
        --data var/plan_state.json

从文件运行离散时间模拟器::

    python -m maritime_handover.cli simulate examples/rescue_scenario.json \
        --output var/sim_report.json
"""

from __future__ import annotations

import argparse
import json
import sys

from .interfaces.http_api import build_default_context, build_server
from .simulator.simulator import run_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="maritime_handover",
        description="海上任务天地链路交接计划器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="启动本地 JSON API")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8080)
    serve_p.add_argument("--data", default=None,
                         help="JSON 状态文件路径；缺省使用纯内存仓储")

    sim_p = sub.add_parser("simulate", help="从文件运行离散时间模拟器")
    sim_p.add_argument("input", help="场景文件（JSON）")
    sim_p.add_argument("--output", default=None, help="报告输出路径")
    sim_p.add_argument("--quiet", action="store_true",
                       help="不在标准输出打印完整报告，仅打印核对结论")

    args = parser.parse_args(argv)

    if args.command == "serve":
        ctx = build_default_context(args.data)
        if args.data:
            ctx.after_write = _make_flush(ctx, args.data)
        server = build_server(args.host, args.port, ctx)
        print(f"天地链路交接计划 API 已启动: http://{args.host}:{args.port}",
              file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n收到中断，退出。", file=sys.stderr)
        finally:
            server.server_close()
        return 0

    if args.command == "simulate":
        report = run_file(args.input, args.output)
        if args.quiet:
            summary = report["summary"]
            print(json.dumps({
                "verification_passed": summary["verification_passed"],
                "final_plan_version": summary["final_plan_version"],
                "link_ticks": summary["link_ticks"],
                "events": summary["events"],
            }, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.output:
            print(f"报告已写入 {args.output}", file=sys.stderr)
        return 0 if report["summary"]["verification_passed"] else 2

    parser.error("未知命令")
    return 1


def _make_flush(ctx, path: str):
    def flush() -> None:
        ctx.service.plans.flush()  # type: ignore[attr-defined]
    return flush


if __name__ == "__main__":
    raise SystemExit(main())
