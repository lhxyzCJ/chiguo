# Chiguo v2 删除审计（Phase 8 清单）

> 状态：**清单已审计，执行待前置功能移植完成**。本文档记录「旧系统哪些东西
> 已被 v2 完全取代可删 / 哪些仍被 v2 依赖保留 / 哪些功能尚未移植因而阻塞删除」。
> 审计方法：全库 import 扫描（生产代码，排除 tests/.venv）+ bridge/cron 入口点核对。

---

## A. 保留（v2 运行时直接依赖）

| 模块 | v2 消费方 | 后续归宿 |
|---|---|---|
| `chiguo_time` / `chiguo_paths` / `chiguo_version` / `chiguo_auth` / `chiguo_net` / `chiguo_locks` / `chiguo_atomic` | 全域基础 | 保持 |
| `chiguo_math` | `domain/affect`、`sources/*`（纯数学） | 收敛为 domain 内部实现 |
| `chiguo_state_models`（EVENT_DELTA/BASELINE_DEFAULTS/`_memory_dedup_key` 等常量） | `domain/affect/model.py` 只读引用 | 常量迁入 domain 后删除 |
| `ops/bridge_ops.py` | `app/actions/executor.py`（/send） | 迁 `integrations/wechat` |
| `schedule/`（parser/sources/day_plan/holiday/anniversary/…） | `sources/{schedule,holiday}.py` 包装（只读） | 数据面并入 `sources/schedule`（写路径另见 C1） |
| `netease/` | `sources/netease.py` 包装（只读） | 并入 `sources/netease` |
| `memory/` + mem0（`data/mem0/`） | 记忆子系统（v2 `memories` 表为 canonical，mem0 为语义索引） | 保留为 memory subsystem |
| `scripts/agent-run.mjs` | executor 生成路径（Phase 7 完整后由 extension/RPC 收敛） | 视 Phase 7 收敛结果 |
| `wechat-bridge/`（send 传输 + 命令路由） | executor `/send`；对话路由待切（见 C3） | 瘦身为传输+命令 |
| `personality/`、`update_holidays.py`、`solar_terms.py` | 静态人格/节假日数据 | 保持 |

## B. 已被 v2 完全取代、可删（执行前置 = C 全部清空）

**旧决策栈**（唯一入口是 daemon CLI / `scripts/chiguo-tick.sh` / bridge 子进程调用；
v2 无任何 import——扫描证据见本文件底部）：

- `chiguo_daemon.py`、`decision/`（base/core/context/engine/idle）、`runner/`、`ops/`（除 bridge_ops）、
  `cli/`（旧 36 参数 CLI）、`state/`（persistence/emotion/interaction/mood/limits/pending/personality/schedule/ownership）
- `chiguo_state.py`、`chiguo_trigger.py`、`chiguo_topics.py`、`chiguo_composer.py`、
  `chiguo_bayesian.py`、`chiguo_circadian.py`、`chiguo_pending.py`、`chiguo_personality.py`、
  `trigger_types.py`、`decision_schema.py`
- `chiguo_demo.py`（面向旧引擎；重写或删除）
- `chiguo_monitor.py`、`monitor/`（stats/alerts/health → `chiguo status`；告警推送见 C2）
- `chiguo_rotation.py` 与日志轮转（JSONL 消失后无对象；先完成旧日志 import/归档决策）

**旧状态文件**（数据由一次性 import 工具迁移后删除；格式不再支持）：

```
chiguo_state.json(+.bak/.tmp/.lock)  chiguo_decisions.jsonl  chiguo_messages.jsonl
chiguo_state_audit.jsonl  chiguo_alerts.json  agent_health.json  chiguo_events.jsonl
schedule_plan.json  schedule_clarify.json  chiguo_loop.pid  break_state.json
```

**旧脚本**：`scripts/chiguo-tick.sh`（cron 改 `chiguo tick`）、`scripts/alert-cron.sh`（C2 后）、
`scripts/chiguo-daemon.service`（systemd 改 `chiguo serve` + `chiguo tick`）、
`install_agent.sh` 的 crontab/systemd 段（重写注册目标）。

## C. 阻塞 Phase 8 的未移植功能（诚实清单）

1. **微信写命令链**（纪念日/假期/安排 extract→verify→写入/澄清追问）：v2 已有
   `schedule.created`→承诺投影与 `chiguo schedule-change` 的等价事件模型，
   但提取/校验/澄清（bridge `command-detect.mjs` + `schedule.mjs` + daemon `--schedule-change`）未移植。
2. **告警/监控推送**（`--alerts-push`、agent_health transition 告警、monitor 12 类异常检测）：
   v2 无 alerts 子系统；删 monitor 前需补最小告警（`alert.raised` 事件 + 经 bridge 推送）。
3. **bridge 对话路由切换**：`serve /context` + `/turn` 与 Pi extension 均已就绪，
   但 `wechat-bridge/agent.mjs`/`message.mjs` 仍走 daemon CLI + agent-run 拼 prompt；
   切换前 v2 不能独占对话上下文。
4. **会话轮换/备份**（`session-rotate.mjs`）→ Pi 原生机制，未接线。
5. **一次性 import 工具**（`chiguo db import-legacy`：decisions/messages/memories → sqlite）未实现。
6. **executor 生成路径的 Phase 7 收敛**（agent-run.mjs → Pi extension/RPC 直连）未完成；
   当前 v2 发送链复用既有 agent-run.mjs（行为等价，但仍是旧 Node 资产）。

## D. 扫描证据（生产 import 摘要）

```
decision/          ← chiguo_daemon.py, ops/engine_ops.py, runner/loop.py, cli/*（旧栈内部闭环）
runner/            ← cli/handlers/session.py, decision/engine.py
ops/（除 bridge）   ← 旧栈内部；app/actions/executor.py 仅用 ops.bridge_ops
state/             ← chiguo_state.py 内部
chiguo_state.py    ← chiguo_trigger/topics/demo、ops/engine_ops、decision/base
chiguo_trigger.py  ← decision/core、chiguo_demo
chiguo_topics.py   ← decision/base
chiguo_composer.py ← decision/base、chiguo_demo
chiguo_bayesian/circadian/pending/personality ← chiguo_state.py / state/*
chiguo_monitor.py  ← cli/handlers/{monitor,conversation}.py（旧 CLI）
monitor/           ← chiguo_monitor.py
chiguo_rotation.py ← decision/base、cli/handlers/rotation.py（+ tests/conftest 守卫）
```

结论：旧栈是**闭环**——除 daemon CLI / tick.sh / bridge 三个进程入口外无外部消费者；
v2 代码零依赖（`chiguo_state_models`/`chiguo_math` 常量级依赖除外）。
C1–C6 完成后，B 段可一次性删除且不影响 v2。

## E. 语义差异备忘（删除旧栈前必须知晓）

1. **计费时点**：v2 = 送达成功（`message.sent`）才扣 energy/anxiety，失败/不确定不扣、
   无退款路径；旧引擎 = 决策时预扣 + 失败退款（`refund_send`）。双写对账期两边数值
   系统性偏移属预期；Phase 8 删除旧栈后旧退款语义消失（v2 语义更简单且无损）。
2. **silent_hours**：v2 reducer 已对齐旧引擎（扣除睡眠窗重叠）；untrusted 时间戳
   由 /turn ±24h 钳制兜底。
3. **damp（A10 回复饱和阻尼）**：v2 恒 1.0（旧引擎按 30 分钟窗口计数）——双写对账期
   密集连发场景会偏移；Phase 6 前补齐或显式记入验收差异。
4. **tick 的日程情境**（is_holiday/in_class 参与情绪半衰期修正）：v2 tick 恒取默认，
   schedule 感知在 Phase 6 sources 接线时注入。
5. **假期/纪念日机会**：v2 已可达（H3 修复后）；旧栈无对应「机会」概念，属新增能力。
