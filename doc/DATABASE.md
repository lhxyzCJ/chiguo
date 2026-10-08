# Chiguo v2 数据库文档（SQLite canonical store）

> 单一数据库文件，默认 `~/.chiguo/chiguo.sqlite`（toml `[storage].db_path` 可改；
> 相对路径锚定 config 所在目录；env `CHIGUO_DB_PATH` 覆盖，测试/运维用）。
> 它是 Chiguo 的 **canonical structured state**：事件、状态、承诺、机会、记忆的
> 唯一事实来源。Mem0（`data/mem0/`）只作语义检索索引，embedding 永不作为事实来源。

## 1. 连接与 PRAGMA

`storage/sqlite/db.py::Database`（进程内单连接，懒建）：

```sql
PRAGMA journal_mode = WAL;        -- 多读者 + 单写者
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;       -- 跨进程并发写等待上限
```

- 写事务统一 `BEGIN IMMEDIATE`（`Database.transaction()`；DDL/DML 原子提交）；
- 打开时强制读 header（`PRAGMA schema_version`）——损坏/非数据库文件 **fail-fast**
  抛 `StorageError`，不静默重建；
- 数据库文件与 `-wal`/`-shm` 统一 0600（对话/记忆为隐私数据）。HTTP 服务
  （`chiguo serve`）每请求独立连接（sqlite3 连接不可跨线程）。

## 2. 表清单（migration v1 + v2）

| 域 | 表 | 说明 |
|---|---|---|
| meta | `schema_migrations` | 迁移版本 + checksum + applied_at |
| 事件 | `events` | append-only 事件日志（因果链载体；见 §3） |
| 对话 | `sessions` / `messages` | 会话与消息镜像（Pi transcript 的语义侧） |
| 状态 | `affect_state` / `relationship_state` / `self_state` / `user_state` | 物化状态单行表 |
| 承诺 | `commitments` | 一等公民未完成事项（open/done/cancelled/expired） |
| 线索 | `threads` | 语义话题（open/closed；last_interaction_at） |
| 规划 | `opportunities` / `drives` / `intents` / `actions` | 机会/驱动力/意图/行动 |
| 送达 | `deliveries` | 每次发送尝试（sent/failed/uncertain + error） |
| 观测 | `world_observations` | source 观测（带 expires_at） |
| 记忆 | `memories` / `memory_links` | canonical 事实 + supersedes/derived_from 链接 |
| 调度 | `schedules` | wake/deferred work（cron/heartbeat/once） |
| 审计 | `autonomous_turns` | 每回合：事件窗口/状态快照/产出指针/结果 |
| 游标 | `runtime_checkpoints`（v2 迁移） | 增量消费者游标（stream/last_event_id/state） |

索引：`events(type, occurred_at)`、`events(correlation_id)`、`events(causation_id)`、
`commitments(status, due_at)`、`threads(state, last_interaction_at)`、
`opportunities(status, expires_at)`、`actions(status, created_at)`、
`world_observations(source, observed_at)`、`memories(kind, status)`、`schedules(status, next_fire_at)`。

## 3. 事件模型

```jsonc
{
  "event_id": "<uuid7 hex，时间有序>",
  "type": "message.received",          // 点分命名
  "occurred_at": "2026-10-01T20:00:00+08:00",  // 现实发生时刻
  "observed_at": "2026-10-01T20:00:01+08:00",  // 系统观察时刻
  "source": "wechat",                  // wechat/user/scheduler/source名/planner/executor
  "correlation_id": "同一 interaction/thread/workflow",
  "causation_id": "直接上游 event_id（因果链的边）",
  "payload": { }
}
```

**增量消费**：`EventStore.after(cursor)` 以 event_id 字典序（≈时间序）读取；
reducer / extractor 各自持 `runtime_checkpoints` 游标，重放/重启从游标续读。
**因果链**：`chiguo events show <id>` 沿 `causation_id` 回溯根因（`chain`），并列出
直接后果（`caused_by`）——「为什么发这条消息」的调试入口。

主要事件类型（当前实现）：`message.received` / `message.sent` / `message.delivery_failed` /
`message.uncertain` / `conversation.replied` / `wake` / `commitment.created` / `commitment.resolved` /
`thread.opened` / `thread.closed` / `schedule.created` / `action.failed` /
`schedule.state` / `schedule.course_starting` / `holiday.upcoming` / `anniversary.upcoming` /
`music.observed` / `weather.changed`。

## 4. 迁移机制

`storage/sqlite/migrations.py`：`MIGRATIONS = (版本, 名称, SQL)` 顺序列表；
- `migrate(db)` 应用未执行迁移（幂等；返回本次应用版本列表）；
- 已应用迁移的 **checksum 漂移 → 拒绝启动**（迁移一经应用不得修改，须新增版本）；
- 数据库版本 **高于代码**（旧代码打开新库）→ `SchemaTooNewError` 拒绝写入。

新增迁移：在 `MIGRATIONS` 追加 `Migration(n, "name", _SQL_Vn)`（勿改动旧项）。

## 5. 备份 / 完整性 / 恢复

```bash
chiguo db status      # 路径/schema 版本/表计数/文件大小（不存在 → initialized:false，不建库）
chiguo db migrate     # 应用待执行迁移（幂等）
chiguo db integrity   # PRAGMA integrity_check + foreign_key_check（ok=false 退出 1）
chiguo db backup      # 在线备份（sqlite3 backup API）→ <db_dir>/backups/chiguo-<ts>.sqlite（0600）
```

**失败模式与恢复**（明确 fail-fast，不静默）：
- 文件损坏/非数据库 → 所有业务命令抛 `StorageError`，CLI 返回 1 + JSON error；
  恢复 = `chiguo db backup` 的最近备份拷回（备份为完整一致快照）。
- schema 过期（低于代码）→ 跑 `chiguo db migrate`。
- schema 超前（高于代码）→ 拒绝写入；升级代码或恢复对应版本备份。

## 6. CLI 速查（v2 运行时接口）

```bash
chiguo db status|migrate|integrity|backup
chiguo events recent [--limit --type --source]     # 事件流（新→旧）
chiguo events show <event_id>                       # 单事件 + 因果链 + 直接后果
chiguo status                                       # affect/关系/承诺/线程/机会/最近回合
chiguo commitments | threads                        # open 承诺 / open 话题
chiguo autonomous-turn [--execute --now --reason]   # 自主回合（默认 shadow）
chiguo execute <action_id> [--dry-run]              # 执行 send_action（生成+发送）
chiguo tick [--execute]                             # 唤醒入口（cron/systemd 调用）
chiguo serve [--port 8790]                          # 回环 HTTP（Pi extension 服务端）
chiguo replay --since <ISO> [--until <ISO>]         # 历史重放（副本上跑，零副作用）
```

## 7. 维护约定

- 每次 schema 变更 → `doc/DATABASE.md` 与 `doc/MIGRATION_PLAN.md` 同步；
- 备份建议：日志轮转/多日任务前后 `chiguo db backup`（单文件快照，可整库恢复）；
- 隐私：数据库含对话原文与记忆，禁止进 git（默认路径在 `~/.chiguo/` 之外仓库）；
  权限 0600 由 `Database._harden_permissions` 每次连接收紧。
