# 迟菓 v2 迁移计划（Phase 1 交付物）

> 配套文档：`doc/ARCHITECTURE_V2.md`（现状审计 / 目标架构 / 模块边界 / **§5 实施状态**）、
> `doc/DATABASE.md`（SQLite schema / 迁移 / 备份恢复 / CLI）、`doc/DELETION_AUDIT.md`（Phase 8 删除清单）。
> 本文档定义 **Phase 2–10 的可执行步骤、SQLite schema 草案、数据迁移映射、删除清单与测试计划**。
> 总原则：**deliberate breaking change**——旧接口/旧状态文件/旧兼容层在 Phase 8 直接删除，
> 不保留 adapter；允许提供一个一次性 import 工具，但新 runtime 不永久支持旧格式。
>
> 执行进度（2026-10-02）：Phase 2–7/9 核心均已实现并测试（scenario 1–6 全绿，见
> architecture-v2 §5.1）；Phase 8 删除清单已审计、**执行被 6 项未移植功能阻塞**
> （见 deletion-audit §C）；Phase 10 文档见 doc/ 四件套。旧生产链路（cron+bridge+daemon）
> 未切换——切换路径见 architecture-v2 §5.3。

---

## 0. 落位与共存策略（Phase 2–7 期间新旧并存）

- v2 代码落在新顶层包，与旧代码物理隔离，避免名字冲突：
  - `app/`（application services：`runtime/`、`autonomous/`、`conversation/`、`actions/`）
  - `domain/`（`affect/`、`relationship/`、`memory/`、`agenda/`、`commitments/`、`threads/`、`planning/`）
  - `storage/`（`sqlite/{schema,migrations,repositories}` + `events/`）
  - `sources/`（`weather/`、`schedule/`、`holiday/`、`netease/`、`wechat/`）
  - `integrations/`（`pi/`、`wechat/`）
  - 新 CLI 落在 `cli/`（新模块；旧 `cli/` 同目录旧文件在 Phase 8 删除）
- 旧运行链路（daemon/bridge/tick/agent-run）在 Phase 2–6 期间**保持生产可用**；
  v2 以「事件双写 + shadow 运行」方式接入（Phase 3–5），Phase 6 切换主动系统，
  Phase 8 删除旧实现。
- v2 测试文件命名：`tests/test_v2_*.py`（扁平命名，与既有 `tests/test_*.py` 同级——
  `test_docs_sync.py` 的「磁盘集合 == pytest 收集集合」双向断言只扫描扁平文件，
  新测试必须落在该集合内；Phase 10 统一重排）。

## 1. SQLite schema 草案（Phase 2 定稿）

单库 `~/.chiguo/chiguo.sqlite`；PRAGMA：`journal_mode=WAL / synchronous=NORMAL /
foreign_keys=ON / busy_timeout=5000`。迁移由 `storage/sqlite/migrations/` 下的
顺序迁移 + `schema_migrations` 表管理；`user_version` 仅作快速校验。

```sql
-- meta / 迁移
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

-- 事件日志（append-only；因果链载体）
CREATE TABLE events (
  event_id       TEXT PRIMARY KEY,          -- ULID
  type           TEXT NOT NULL,
  occurred_at    TEXT NOT NULL,             -- ISO8601 CST
  observed_at    TEXT NOT NULL,
  source         TEXT NOT NULL,
  correlation_id TEXT,
  causation_id   TEXT,
  payload        TEXT NOT NULL DEFAULT '{}' -- JSON
);
CREATE INDEX idx_events_type_time ON events(type, occurred_at);
CREATE INDEX idx_events_correlation ON events(correlation_id);
CREATE INDEX idx_events_causation ON events(causation_id);

-- 会话 / 消息（Pi transcript 的语义镜像；pi_session_id 关联 Pi 会话文件）
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,   -- conversation / schedule / system
  pi_session_id TEXT, started_at TEXT NOT NULL, ended_at TEXT
);
CREATE TABLE messages (
  id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id),
  direction TEXT NOT NULL,                    -- in / out
  text TEXT NOT NULL, at TEXT NOT NULL,
  event_id TEXT REFERENCES events(event_id),
  analysis TEXT,                              -- JSON（情绪分析）
  delivery_id TEXT
);
CREATE INDEX idx_messages_time ON messages(at);

-- 物化状态（每域单行 current + 变更走事件）
CREATE TABLE affect_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  valence REAL, arousal REAL, energy REAL, tension REAL,   -- 内部维度（可映射旧 5 维）
  updated_at TEXT NOT NULL, event_id TEXT REFERENCES events(event_id)
);
CREATE TABLE relationship_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  closeness REAL, trust REAL, familiarity REAL, recent_warmth REAL,
  recent_tension REAL, interaction_rhythm TEXT, initiative_balance REAL,
  shared_history_depth REAL, updated_at TEXT NOT NULL
);
CREATE TABLE self_state (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE user_state (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, updated_at TEXT NOT NULL);

-- 承诺 / 线索（一等公民）
CREATE TABLE commitments (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, details TEXT,
  due_at TEXT, status TEXT NOT NULL DEFAULT 'open',    -- open/done/cancelled/expired
  created_from_event TEXT REFERENCES events(event_id),
  created_at TEXT NOT NULL, resolved_at TEXT, resolution_event TEXT REFERENCES events(event_id)
);
CREATE INDEX idx_commitments_status_due ON commitments(status, due_at);
CREATE TABLE threads (
  id TEXT PRIMARY KEY, subject TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
  opened_at TEXT NOT NULL, last_interaction_at TEXT, closed_at TEXT,
  source TEXT, payload TEXT
);
CREATE INDEX idx_threads_state ON threads(state, last_interaction_at);

-- 机会 / 驱动力 / 意图 / 行动
CREATE TABLE opportunities (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  novelty REAL, relevance REAL, urgency REAL, emotional_affordance REAL,
  expires_at TEXT, observation_event_id TEXT REFERENCES events(event_id),
  status TEXT NOT NULL DEFAULT 'open',        -- open/consumed/expired/dismissed
  created_at TEXT NOT NULL, payload TEXT
);
CREATE INDEX idx_opportunities_status ON opportunities(status, expires_at);
CREATE TABLE drives (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, intensity REAL NOT NULL,
  evaluated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  inputs TEXT,                                 -- JSON（affect/relationship/threads 快照引用）
  autonomous_turn_id TEXT
);
CREATE TABLE intents (
  id TEXT PRIMARY KEY, type TEXT NOT NULL,     -- check_in/follow_up/comfort/remind/...
  why TEXT NOT NULL,                           -- JSON：opportunity_ids/drive_ids/explanation
  plan TEXT, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  autonomous_turn_id TEXT
);
CREATE TABLE actions (
  id TEXT PRIMARY KEY, type TEXT NOT NULL,
  status TEXT NOT NULL,                        -- pending/started/completed/failed/cancelled/deferred
  intent_id TEXT REFERENCES intents(id),
  cause_event_id TEXT REFERENCES events(event_id),
  correlation_id TEXT,
  created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
  input TEXT, output TEXT, error TEXT
);
CREATE INDEX idx_actions_status ON actions(status, created_at);
CREATE TABLE deliveries (
  id TEXT PRIMARY KEY, action_id TEXT REFERENCES actions(id),
  message_id TEXT REFERENCES messages(id), channel TEXT NOT NULL,
  status TEXT NOT NULL,                        -- sent/failed/uncertain
  attempts INTEGER NOT NULL DEFAULT 1, sent_at TEXT, error TEXT, provider_message_id TEXT
);

-- 观测（source 输出；与 events 的 *.observed 对应）
CREATE TABLE world_observations (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, type TEXT NOT NULL,
  observed_at TEXT NOT NULL, expires_at TEXT,
  event_id TEXT REFERENCES events(event_id), payload TEXT NOT NULL
);
CREATE INDEX idx_world_obs_source ON world_observations(source, observed_at);

-- 记忆（canonical）+ 链接 + 语义索引关联
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                          -- semantic/episodic/relationship/user_fact/assistant_self
  text TEXT NOT NULL,
  source TEXT, origin TEXT, confidence REAL,
  observed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  valid_from TEXT, valid_to TEXT,
  status TEXT NOT NULL DEFAULT 'active',       -- active/stale/superseded/forgotten/conflicted
  importance REAL, emotion_tag TEXT,
  mem0_id TEXT,                                -- 语义索引关联（embedding 永不作为事实来源）
  meta TEXT
);
CREATE INDEX idx_memories_kind_status ON memories(kind, status);
CREATE TABLE memory_links (
  from_memory_id TEXT NOT NULL REFERENCES memories(id),
  to_memory_id TEXT NOT NULL REFERENCES memories(id),
  relation TEXT NOT NULL,                      -- supersedes/derived_from/related
  created_at TEXT NOT NULL,
  PRIMARY KEY (from_memory_id, to_memory_id, relation)
);

-- 调度（wake 与 deferred work；不含「要不要发」的决策）
CREATE TABLE schedules (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,     -- cron/heartbeat/once/deferred
  due_at TEXT, recurrence TEXT, payload TEXT,
  status TEXT NOT NULL DEFAULT 'pending', last_fired_at TEXT, next_fire_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_schedules_next ON schedules(status, next_fire_at);

-- 自主回合审计（每次 autonomous_turn 的完整输入输出指针，便于 replay/shadow 与「为什么」）
CREATE TABLE autonomous_turns (
  id TEXT PRIMARY KEY, reason TEXT NOT NULL,   -- cron/heartbeat/event/manual
  started_at TEXT NOT NULL, finished_at TEXT,
  event_window_from TEXT, event_window_to TEXT,
  state_snapshot TEXT, opportunity_ids TEXT, drive_ids TEXT, intent_id TEXT,
  action_id TEXT, outcome TEXT                 -- sent/waited/deferred/...
);
```

查询性能：对高频查询（events 按类型+时间、commitments open+due、threads open、
actions 按 status）以 `EXPLAIN QUERY PLAN` 验证走索引；必要时补部分索引。

## 2. 事件类型初始集合

```
message.received / message.sent / message.delivery_failed / message.uncertain
conversation.turn_completed
schedule.created / schedule.updated / schedule.cancelled / schedule.completed / schedule.missed
commitment.created / commitment.resolved / commitment.expired
thread.opened / thread.updated / thread.closed
weather.changed / music.observed / holiday.upcoming / anniversary.upcoming
affect.updated / relationship.changed / user.state_inferred / self.reflected
opportunity.discovered / opportunity.expired
drive.evaluated / intent.created / intent.abandoned
action.started / action.completed / action.failed / action.deferred
wake（cron/heartbeat/manual 唤醒）/ db.migrated / alert.raised
memory.created / memory.superseded / memory.confirmed
```

## 3. 数据迁移映射（一次性 import 工具 `chiguo db import-legacy`）

| 旧来源 | 新去处 | 规则 |
|---|---|---|
| `chiguo_state.json` emotion/cooldown/circadian | `affect_state` + `world_observations`/events（历史）| 当前值进 affect_state；可重建的历史（情绪演化）按需生成 `affect.updated` 事件（可选） |
| `chiguo_state.json` personality / personality_history | `self_state.payload` +（history 保留在 payload） | 人格静态配置仍读 TOML；演变历史作为 self_state 快照 |
| `chiguo_state.json` pending_topics | `threads` | topic→subject；attempted→last_interaction 标记 |
| `chiguo_state.json` memory_dedup / recv_dedup / refunded / reply_stats 等 | 丢弃 | 纯运行时乐观锁/去重状态，不再迁移（行为由事件+唯一约束重表达） |
| `chiguo_decisions.jsonl` | `events`（`message.sent`/`wake`/`message.received` 等）+ `messages` | 逐行转换：send→message.sent（correlation_id=msg_id）；recv→message.received；idle→低价值，可丢弃或压缩为统计 |
| `chiguo_messages.jsonl` | `messages` | 直接逐行导入（含时间序）|
| `data/chiguo_memories.json` | `commitments`（reminder→due_at）/ `schedules`（habit→recurrence） | 内容仍为事实源；last_triggered_at→status/触发记录 |
| `break_state.json` / `schedule_overrides.json` / `anniversaries.json` / `holidays.json` / `schedule_cache.json` | `schedules`/`commitments` + 静态配置（holidays 仍可留 JSON 作静态数据） | schedule 域在 Phase 6 重写为 source；导入仅在需要保留历史时 |
| `schedule_plan.json` | 丢弃 | replan 机制由 planner 取代 |
| `chiguo_alerts.json` | `events`（alert.raised） | 告警生命周期由 CLI/监控重建 |
| `agent_health.json` | `events`（alert.raised/恢复） | 健康状态机并入 deliveries/events |
| `data/mem0/` | `memories`（mem0_id 关联） | 回填 memories 行（kind=semantic，source=mem0_import，confidence 默认 0.5，status=active），保留 mem0 作为索引；不做事实级合并 |
| `~/.chiguo/session-backups`、`~/.pi/agent/sessions` | 不迁移 | Pi 会话是执行层转录，保留原状 |
| `schedule_clarify.json` | `threads`/actions（若在途） | 6h 短时状态，通常直接丢弃 |

## 4. Phase 2–10 执行计划

### Phase 2 — SQLite 基础设施（storage 层）
交付：
- `storage/sqlite/`：`db.py`（连接/PRAGMA/事务/`connect()`）、`schema/`（§1 DDL）、
  `migrations/`（v1 初始 + 迁移运行器 + `schema_migrations`）、
  `repositories/`（`events.py`、`messages.py`、`state.py`、`commitments.py`、`threads.py`、
  `opportunities.py`、`drives.py`、`intents.py`、`actions.py`、`memories.py`、`schedules.py`）；
- `storage/events/`：`EventStore.append(type, ..., causation_id=...)` + 读取/链查询原语；
- `chiguo db status|migrate|integrity|backup` CLI（新 `cli/` 模块）。
出口条件：新增单测全绿（并发写、迁移幂等、外键/约束、损坏数据库 fail-fast、integrity check）；
`EXPLAIN QUERY PLAN` 抽查高频查询。
不做：任何旧代码接入。

### Phase 3 — 事件双写（旧逻辑继续跑）
交付：
- 旧链关键路径接入 `EventStore`（旁路写，失败不阻断旧链）：
  - `ops.engine_ops.record_user_message` → `message.received`；
  - `record_send_text` / `record_send_result` → `message.sent` / `message.delivery_failed` / `message.uncertain`；
  - `decision.core._log` → `wake`（带 idle reason / send 摘要）；
  - schedule 域（`--schedule-change`、`anniversary`、`break`）→ `schedule.*` / `commitment.*`；
  - netease service 拉取成功/故障 → `music.observed` / 故障事件（source observation）；
  - bridge 收发的原始事实（可选：由 Python 侧双写已足够）。
- `sources/` 插件接口 + `schedule`/`holiday`/`netease` 三个只读 source（包装现有数据面，仅 observe）。
出口条件：对账测试——同一天旧 decisions/messages 与 events 行数/时间戳一致；
旧全量测试绿（双写失败静默不影响旧链）。

### Phase 4 — 物化状态（state reducer）
交付：
- `domain/affect/`（内部保留现情绪数学，公共 API 收敛为 `affect.current()`/`apply(event)`）；
- `domain/relationship/`（initiative_balance 等，含历史）；
- `domain/commitments/`、`domain/threads/`（从事件重建）；
- `app/runtime/reducer.py`：events → state 的确定性重放（含 checkpoint 表或按需全量重建）；
- `chiguo status`/`chiguo affect`/`chiguo relationship`/`chiguo commitments`/`chiguo threads` CLI（读 v2 库）。
出口条件：从 Phase 3 的事件流重建的 state 与旧 state 关键字段对账（容差定义）；
reducer 幂等（重放两次结果一致）。

### Phase 5 — Opportunity / Drive / Intent / Action（最小 planner）
交付：
- `domain/planning/`：`opportunities.py`（发现器：从 observations+state 派生，含 expiry）、
  `drives.py`（评估器）、`planner.py`（最小确定性规则版：opportunity→intent，含 defer/wait 分支）；
- `app/actions/`：Action 模型 + executor（send_message 只记录为 `pending`，不真正发送）；
- `app/autonomous/turn.py`：`autonomous_turn(reason)` 主循环（collect events → reduce →
  discover → evaluate → plan → execute/wait → persist）；
- `chiguo autonomous-turn` CLI（默认 shadow：只落库不发送）。
出口条件：scenario 测试 1–3 绿（见 §6）。

### Phase 6 — 主动系统切换
交付：
- `sources/schedule`、`sources/holiday`、`sources/netease` 全量接入 + `sources/weather`（可选，配置后启用）；
- `scheduler`：`schedules` 表 + `chiguo tick`（替代 `scripts/chiguo-tick.sh` 的决策部分；
  cron/systemd/heartbeat/manual 统一入口 → `wake` 事件 → `autonomous_turn`）；
- 发送执行：intent=send_message → Pi 生成（暂沿用 agent-run/RPC 通道，Phase 7 收口）→
  bridge `/send` → `message.sent`/`delivery_failed` 事件 → relationship/memory 更新；
- 旧触发/话题链停用（代码暂留，Phase 8 删除）。
出口条件：scenario 1–5 绿；影子对照记录（新 planner 决策 vs 旧 trigger 决策）产出首份对比。

### Phase 7 — Pi 接入新 runtime
交付：
- `integrations/pi/` + Pi extension（`chiguo-pi-extension`，TS）：
  `before_agent_start` 注入 personality/relationship/agenda/memory/intent/world context；
  `message_end`/`agent_end` 回写 transcript 事实；
- `chiguo serve`（127.0.0.1 回环 HTTP，本地单用户）：bridge/extension 的唯一 runtime 接口，
  取代每条消息 4-5 个 CLI 子进程；bridge 瘦身为「微信传输 + 命令路由 + 鉴权」；
- 会话管理交还 Pi（session id/轮换/备份），删 bridge 内 session-rotate/RPC 预算链自建逻辑。
出口条件：端到端集成测试（微信入 → Pi 生成 → SQLite 落库 → 微信出）绿；
回复侧不再拼 attention/memory/instruction 前缀（由 extension 注入）。

### Phase 8 — 删除旧系统
删除清单（详见 §5）：`chiguo_state.json` 全家桶、`chiguo_trigger.py`、`chiguo_topics.py`、
`chiguo_composer.py`、`decision/`、`runner/loop.py`、`ops/`、旧 `cli/`、旧 `state/`、
`schedule/`（数据面并入 `sources/`）、`netease/`（并入 `sources/`）、`monitor/`（并入 CLI/告警）、
`chiguo_pending.py`、`chiguo_bayesian.py`（若 user_state 估计器重写）、`chiguo_circadian.py`（并入 affect/rhythm）、
`scripts/chiguo-tick.sh`、`scripts/agent-run.mjs`（由 extension/RPC 取代）、
bridge 的 `agent.mjs`/`agent-rpc.mjs`/`session-rotate.mjs`/`schedule.mjs`（对话上下文移交 runtime）。
出口条件：全库 grep 无旧状态文件读写路径；旧测试迁移或删除后全绿。

### Phase 9 — replay / shadow
交付：`chiguo replay <range>`（重放 events → state/opportunity/drive/planner，不发送，输出
每步 decision + why）；`chiguo shadow <range>`（在线影子运行，记录 would-have-sent）；
`autonomous_turns` 审计表 + `chiguo events show <id>` 因果链展示。
出口条件：对历史事件区间能完整重放并回答「为什么」链；shadow 记录可读。

### Phase 10 — 测试 / 文档 / 清理
交付：三层测试（unit/integration/scenario）补全；dead-code/dependency/state-file 全库审计；
README / architecture / database / configuration / developer 文档重写；
最终报告（§7）。

## 5. Phase 8 删除清单（breaking changes）

**删除（被完全取代）**：
- 状态文件：`chiguo_state.json(+bak/tmp/lock)`、`chiguo_decisions.jsonl`、`chiguo_messages.jsonl`、
  `chiguo_state_audit.jsonl`、`chiguo_alerts.json`、`agent_health.json`、`chiguo_events.jsonl`、
  `schedule_plan.json`、`schedule_clarify.json`、`chiguo_loop.pid`、`break_state.json`
  （数据留待 import 工具，格式不再支持）；
- 模块：旧 `decision/`、`cli/`、`ops/`、`state/`、`runner/`、`monitor/`、
  `chiguo_state*.py`、`chiguo_trigger.py`、`chiguo_topics.py`、`chiguo_composer.py`、
  `chiguo_pending.py`、`chiguo_bayesian.py`、`chiguo_circadian.py`、`chiguo_personality.py`
  （人格并入 domain/self）、`schedule/`、`netease/`、`trigger_types.py`、`decision_schema.py`
  （schema 由 storage 约束取代）；
- 脚本：`scripts/chiguo-tick.sh`、`scripts/agent-run.mjs`（若 RPC/extension 完全替代）、
  `scripts/chiguo-daemon.service`、bridge 侧 `agent-rpc.mjs`/`session-rotate.mjs`/`schedule.mjs`/`agent.mjs`
  （视 Phase 7 收敛结果）；
- 保留：`chiguo_math.py` 的数学内核（降为 domain 内部实现）、`chiguo_locks`/`chiguo_atomic`
  （若 SQLite 事务替代后仍需要则保留）、`chiguo_time`/`chiguo_paths`/`chiguo_net`/`chiguo_auth`/
  `chiguo_version`、`personality/`、`holidays.json`（静态数据）、`update_holidays.py`、
  `solar_terms.py`（并入 sources/holiday）。

**Breaking changes（对外契约）**：
- CLI：`chiguo_daemon.py --*` 36 参数 → `chiguo <subcommand>`；
- 状态文件格式：JSON → SQLite（提供一次性 `db import-legacy`）；
- 决策 JSON schema → events/intents/actions 表结构；
- bridge HTTP 契约：`/send`/`/agent/prompt` → 新的 loopback runtime API + `/send` 传输。

## 6. 测试计划

三层：

- **unit**：storage（迁移/并发/约束/损坏）、reducer 幂等、opportunity/drive/planner 纯逻辑、
  memory supersession、sources 解析；
- **integration**：bridge↔runtime↔SQLite（内存/临时库）、Pi extension 注入（mock Pi）、
  executor 的 send/refund 闭环、scheduler 唤醒；
- **scenario**（用户指定 6 条，Phase 5/6 起建立）：
  1. 「明天考试」→ 次日考试结束 → open commitment + opportunity + follow-up intent；
  2. 长期无新世界事件 → loneliness 不单独无限主动发送（drive 受控，产出 wait/defer）；
  3. 天气变化 + open thread → weather 作为 secondary cue 而非强制 topic；
  4. 主动消息未获回复 → relationship/initiative/future planning 正确更新
     （而非 `messages_without_reply += 1`）；
  5. Mem0 不可用 → structured state 与核心运行不受影响；
  6. SQLite 损坏 / schema 过期 → fail-fast 与恢复行为明确（备份→重建→告警，不静默）。

补充测试：`replay` 确定性（同区间两次重放一致）；shadow 不产生发送副作用。

## 7. 最终验收标准（用户给定）

1. 完整 proactive flow 可用：world/user event → persistent event → state update →
   opportunity discovery → drive evaluation → planner → intent → Pi/LLM realization →
   action → delivery → result event → relationship/memory/state update；
2. 任何主动消息可从数据库反查：为什么产生/由什么事件触发/当时 relationship/affect/
   有哪些 opportunity/planner 为什么选此 intent/执行了什么 action/用户是否回复；
3. 全仓库 dead-code/dependency/state-file 审计完成，被取代系统删除；
4. `pytest` 全部通过；
5. 最终报告（12 项：新架构/SQLite schema/Event model/Opportunity-Drive-Intent-Action/
   Pi 集成/Mem0 职责/删除清单/breaking changes/数据迁移/测试结果/已知问题/下一阶段建议）。

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 双跑期间事件双写拖慢旧链 | 双写旁路吞异常 + 定期批量；出口条件不含旧链时延回归 |
| reducer 与旧状态漂移 | Phase 4 对账测试（容差 + 差异报告），Phase 6 前必须收敛 |
| Pi extension 能力/版本差异 | 锁定 Pi 版本；extension 最小依赖 `before_agent_start`/`message_end`；降级路径 = bridge 注入（临时） |
| 删除清单误删仍被引用的模块 | Phase 8 前用依赖图 + grep 全量核对；先删写入路径再删读路径 |
| 一次性 import 工具的时区/乱码/坏行 | 导入器逐行容错 + `--dry-run` 报告 + 幂等（重跑不重复） |
| 单用户系统回归不可感知 | shadow 期保留新旧决策对照；Phase 6 首周逐日 review shadow 报告 |
```