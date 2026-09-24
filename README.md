# 海上任务天地链路交接计划器

为海上任务安排卫星与地面链路的连续接入。系统接收按版本更新的卫星波束窗口、
近岸地面覆盖、船位预测与终端能力，依据任务优先级、最短保持时间、终端切换
冷却、波束容量与不可同时占用关系生成链路计划，明确每次接入、释放和交接的
时间与原因；晚到的预测只触发尚未执行且真正受影响区段的重算。

## 概念模型

- **链路窗口 LinkWindow**：某条链路（卫星波束 `satellite` / 地面站 `ground`）
  在一个版本下的可用区段，含频段、容量与覆盖 footprint（圆心 + 半径）。
  同一链路的新版本窗口整体替换旧版本。
- **船位轨迹 Track**：按版本更新的 (tick, 经纬度) 序列，区间内线性插值。
  船位落在 footprint 内才能使用对应链路。
- **终端 Terminal**：船载终端，含可用频段与切换冷却（tick）。冷却期内不得
  再次接入；同一任务在释放的同一 tick 的计划内交接不受冷却限制。
- **任务 Task**：在 `[start, end)` 内需要连续链路保持，含优先级与最短保持
  时间 `min_hold`（一段会话不得短于它，抢占也不得破坏受害者的它）。
- **互斥组 ExclusionGroup**：组内链路在任一 tick 的活动会话数不得超过
  `limit`（两两互斥即 `limit=1`）。
- **计划 Plan**：版本化的会话集合。每次接入 / 释放 / 交接都是带时间与原因
  的事件；事件 id 由内容决定，未受重算影响的会话在多次重算间保持同一 id。

时间统一为整数 tick，区间为左闭右开。

## 运行规则要点

- 高优先级任务可抢占未确认会话；**已确认的紧急时隙**（优先级 ≥ 100 并经
  `confirm`）不得被普通任务夺走，除非针对（紧急任务, 请求方任务）的
  **双人批准**（两名不同审批人）已经生效。
- 晚到的窗口 / 轨迹版本只重算：会话或覆盖缺口与变化区段相交、且尚未执行
  的任务；其余任务的计划事件保持不变。被抢占截断的任务会级联重排。
- 重复提交幂等：观测按 (id, version) 去重，变更类请求按 `request_id` 去重，
  重复审批不会重复计数。

## 本地 JSON API

```bash
python3 -m maritime_handover.interfaces.api --host 127.0.0.1 --port 8080 \
    [--data-file state.json]
```

主要端点（变更类均接受可选 `"request_id"` 用于幂等重放）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/links/{link_id}/windows` | 提交链路窗口新版本 |
| POST | `/v1/vessels/{vessel_id}/tracks` | 提交船位轨迹新版本 |
| PUT  | `/v1/terminals/{terminal_id}` | 登记终端（频段、冷却） |
| PUT  | `/v1/exclusions/{group_id}` | 登记互斥组 |
| POST | `/v1/tasks` | 提交任务 |
| POST | `/v1/tasks/{task_id}/confirm` | 确认紧急时隙 |
| POST | `/v1/tasks/{task_id}/cancel` | 取消任务 |
| POST | `/v1/approvals` | 登记双人批准（`approval_id`/`task_id`/`requester_task_id`/`approver`） |
| POST | `/v1/time` | 推进本地时钟 `now` |
| POST | `/v1/replan` | 显式触发重算 |
| GET  | `/v1/plans/current`、`/v1/plans/{version}` | 计划视图（含事件与版本差异） |
| GET  | `/v1/tasks/{task_id}/schedule` | 单任务会话 / 事件 / 缺口 |
| GET  | `/v1/state/summary`、`/v1/health` | 状态汇总 / 健康检查 |

指定 `--data-file` 后，每次变更把全量状态原子快照到 JSON 文件，重启后
自动恢复（含版本号、确认状态与幂等记录）。

## 离散时间模拟器

```bash
python3 -m maritime_handover.interfaces.simulator examples/scenario_basic.json \
    [--out report.json]
```

场景文件给出初始资源、任务与按 tick 编排的脚本动作（新窗口版本、新轨迹、
新任务、确认、批准等），示例见 `examples/`：

- `scenario_basic.json`：地面站 → 卫星 → 卫星的无缝交接链；
- `scenario_conflict.json`：容量争用、优先级抢占、紧急时隙确认与双人批准；
- `scenario_replan.json`：窗口突然缩短后的增量重算（未受影响任务事件不变）。

报告 `checks` 部分提供四类核对：

- `seamless_handover`：每个任务的需求区间是否被会话连续覆盖、交接次数；
- `capacity_conservation`：任一 tick 链路占用不超过窗口容量、互斥组不超限；
- `conflict_waiting`：任务从需求起点到首次接入的等待 tick 数；
- `infeasible_tasks`：未覆盖区段及原因（无可用链路 / 不在覆盖内 /
  容量耗尽 / 终端不可用 / 互斥阻塞 / 连续窗口不足）。

## 工程约定

项目采用 Python 包目录组织服务端代码。领域模型、应用服务、持久化适配和
接口层应保持边界清晰；时间、标识生成及外部观测均通过可替换端口接入，便于
稳定复现业务过程。运行数据不得写入源码目录，临时文件和本地配置由
`.gitignore` 排除。

目录结构：

```
maritime_handover/
  domain/         # 模型、约束、调度器、事件推导、计划核对
  application/    # 用例服务：版本接入、增量重算、确认与批准、幂等
  infrastructure/ # 内存仓储、JSON 序列化、文件快照
  interfaces/     # 本地 JSON API、离散时间模拟器
examples/         # 模拟器场景
tests/            # 单元与端到端测试
```

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q maritime_handover tests
```
