# 海上任务天地链路交接计划器

为海上搜救船队安排卫星波束与近岸地面链路的**连续接入**：依据任务优先级、
最短保持时间、终端切换冷却、波束容量与同终端互斥关系生成计划，给出每次
**接入 / 释放 / 交接 / 等待**的时刻与原因；新版预测晚到时只重算尚未执行且
真正受影响的区段；已确认紧急时隙受双人批准保护；重复审批与重复事件幂等。

## 架构

纯 Python 标准库实现，按端口-适配器分层：

```
maritime_handover/
├── domain/          # 领域模型与枚举（资源/终端/任务/窗口/段/事件/锁/批准）
├── application/
│   ├── scheduler.py         # 逐刻度排程引擎（纯领域服务，无 IO）
│   ├── planning_service.py  # 用事编排：场景/预测/增量重算/双人批准
│   ├── ports.py             # 时钟、标识、观测、仓储端口
│   └── serialization.py     # JSON ↔ 领域对象
├── adapters/
│   ├── drivers.py           # 固定时钟、顺序 ID、收集型观测器
│   ├── memory_repo.py       # 内存仓储
│   └── json_repo.py         # JSON 文件快照仓储
├── interfaces/
│   └── http_api.py          # 本地 JSON API（http.server）
└── simulator/
    ├── simulator.py         # 文件驱动的离散时间模拟器
    └── verifier.py          # 四项结果核对
```

时间使用整数刻度，所有区间左闭右开 `[start, end)`。旧链路在 `t` 释放、新链路
在同一 `t` 接入记为一次 **HANDOVER**，首尾相接、不计中断。

## 排程规则

1. **优先级**：紧急任务优先，其次 `priority` 数值小者，同序按释放时刻、id。
2. **最短保持**：接入时承诺占用 `min_hold` 个刻度（不超过窗末/截止/剩余需求），
   承诺期内不可被抢占。
3. **切换冷却**：终端真正掉线后 `cooldown` 个刻度内不得重新接入；无缝交接是
   预先协调的 make-before-break，豁免冷却。
4. **波束容量**：每刻统计固定占用、外部紧急锁预留与动态占用；高优先级可驱逐
   承诺已到期的低优先级占用，否则冲突等待。
5. **互斥**：一部终端同一时刻只在一条链路上，同终端其他活动任务记
   `mutex_blocked`；支持互斥组联合容量。
6. **紧急保护**：紧急任务的已排时隙入锁并预留容量，普通任务无法夺走；只有持
   **两名不同批准人**的批准生效后，受益任务才能占用，且紧急任务同批重算。
7. **增量重算**：新预测按窗口身份键 `(window_uid, 终端, 资源)` 做差量，只重算
   覆盖变化终端上、截止时刻之后仍活动的任务；cutoff 之前的计划冻结，未受影响
   任务的未来段作为不可驱逐占用参与容量核算。

## 本地 JSON API

```bash
python -m maritime_handover.cli serve --host 127.0.0.1 --port 8080 \
    --data var/plan_state.json   # --data 可省，省则纯内存
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/health` | 健康检查与当前时钟/预测版本 |
| POST | `/api/scenario` | 建立资源、终端、任务、视界 |
| POST | `/api/predictions` | 摄入一版窗口预测（版本严格递增，自动增量重算） |
| POST | `/api/plans/generate` | 生成初始计划 |
| POST | `/api/plans/recompute` | 以当前时钟为确认时刻手动重算 |
| GET  | `/api/plan` | 段、事件、紧急锁、审批 |
| GET  | `/api/plans/tasks/<id>` | 单任务计划 |
| GET  | `/api/windows?version=n` | 查询窗口版本 |
| POST | `/api/approvals` | 双人批准（同 `request_id` 幂等） |
| POST | `/api/clock/advance` `/api/clock/set` | 推进/设置确认时钟 |
| GET  | `/api/observations` | 过程观测记录 |

写操作可带 `Idempotency-Key` 头：同键重复提交直接回放首次结果，不会重复排程
或重复记账。

### 双人批准示例

```bash
# 第一位批准人（不生效）
curl -X POST localhost:8080/api/approvals -d '{
  "request_id":"R1","approver":"值班长","task_id":"T_N",
  "resource_id":"SAT1","start":10,"end":14}'
# 第二位不同批准人（生效并触发仅涉及受益任务与被挤让紧急任务的重算）
curl -X POST localhost:8080/api/approvals -d '{
  "request_id":"R1","approver":"搜救指挥","task_id":"T_N",
  "resource_id":"SAT1","start":10,"end":14}'
```

## 离散时间模拟器

从一个 JSON 文件运行（场景 + 分时刻到达的预测版本 + 分时刻提交的批准）：

```bash
python -m maritime_handover.cli simulate examples/rescue_scenario.json \
    --output var/sim_report.json
```

报告的 `verification` 给出四项可机器核对的结论：

- **seamless_handover**：每次交接无空档、无重叠，事件与计划段一致；
- **capacity_conservation**：逐 `(资源, 刻度)` 占用不超容量，终端无双重链路；
- **conflict_waits**：每条等待事件的原因都有客观证据（无窗口/冷却/容量满/
  紧急锁/互斥）；
- **unschedulable**：无解任务的需求、已服务、缺口账目守恒（已服务+缺口=需求）
  并附按原因分类的等待构成。

`summary.verification_passed` 为总结论；进程退出码 `0` 通过、`2` 存在核对失败。
示例场景演示了窗口突然缩短后的无缝交接、容量竞争、紧急时隙双人批准与无解说明。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q maritime_handover tests
```

运行数据写入 `var/`（已由 `.gitignore` 排除），不污染源码目录。
