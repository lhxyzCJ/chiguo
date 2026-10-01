# 迟菓 v2 架构设计文档（Phase 1 审计产物）

> 状态：**Phase 1 审计完成前的草稿**——本文档由全库直读 + 并行子代理审计产出，
> 描述 **as-is 现状架构**、**问题清单**、**to-be 目标架构（v2 runtime）**、**迁移策略**与**模块边界**。
> 实施顺序见文末「迁移策略」，任一阶段的实施不得绕过本文档的边界约定。
>
> 审计范围：仓库根全部 Python（`chiguo_*.py`、`decision/`、`state/`、`ops/`、`runner/`、`cli/`、
> `schedule/`、`memory/`、`netease/`、`monitor/`）、`wechat-bridge/` 全部 .mjs、
> `scripts/`（tick/agent-run/deploy/服务脚本）、`tests/`、`personality/`、配置与文档。
> 行号引用以审计时刻工作区为准；本文件优先引用「文件 + 函数名」，行号仅作辅助。

---

## 0. 执行摘要

迟菓当前是一套 **「零 LLM 数学决策引擎 + LLM 生成 + 微信触达」** 的双进程系统：

- **决策**：`chiguo_daemon.py`（拆包后由 `decision/`、`state/`、`ops/`、`runner/`、`cli/` 组合）
  以 5 维情绪 + 14 种触发 + 8 路话题源 + Bayesian 用户状态推断做「要不要发、发什么类型」的判定；
- **生成**：`scripts/agent-run.mjs` 把决策 JSON / 分析请求包装成 prompt，交给 Pi agent 二进制
  （`pi -p`）或常驻 RPC（`pi --mode rpc`）生成微信文本；
- **触达**：`wechat-bridge/`（Node 常驻）收消息、跑路由（白名单/命令/澄清/聊天）、发消息；
- **持久化**：运行时状态散落在 **十余个 JSON/JSONL 文件**（`chiguo_state.json` 为最大的一个）
  加 mem0（qdrant 嵌入式）语义记忆库。

系统能跑、且经过大量加固（并发锁、退款闭环、超时对齐、NAT/时钟防护），但架构上存在四个
结构性问题，这是 v2 重构的动机：

1. **消息是系统的基本单位，而不是 Action**。主动行为的所有可能性都被压缩成
   「send / idle」二值——observe、remember、reflect、wait、defer、follow_up、check
   无处表达；反过来，一切输入（天气、课表、听歌、记忆）都被硬编码为「触发类型」，
   而不是「世界观测」。
2. **状态没有单一权威，也没有历史**。情绪/冷却/人格/生物钟/待办话题/记忆去重标记全塞在
   一个 `chiguo_state.json` 里靠字段名维系语义；schedule/netease/健康/澄清各自另有 JSON；
   决策历史是 `chiguo_decisions.jsonl`。没有任何机制能回答「昨天为什么发那句话」的完整因果链。
3. **Python ↔ Node ↔ Pi 三层协议手工对齐**。决策 schema 以注释互引、超时预算以注释互引、
   会话与 prompt 模板在 RPC 与 spawn 两条路径双份维护——协议漂移没有运行时检测。
4. **Pi 的能力没有被充分利用**。Chiguo 自己在 Python 里重建了一套「触发→话题→情境文本」的
   上下文拼装，而 Pi 的 extension/`before_agent_start`/session/compaction/RPC 机制本可以
   承载注入与执行，让 Chiguo 专注角色/世界/关系状态。

v2 的目标（用户定义）：把 Chiguo 从「带情绪算法和主动触发器的聊天机器人」演变成
**一个单用户、长期运行、具有持续内部状态、关系状态、记忆、未完成事项、环境感知和
自主行动能力的私人 companion agent**。

核心模型变为：

```
External Sources / User Interaction → Events → State Reducer
  → World / Relationship / Self State → Memory
  → Opportunities → Drives / Agenda → Planner → Intent
  → Pi / LLM → Action → Action Result → Event
```

**SQLite 成为唯一 canonical structured state store**；Mem0 降级为 memory subsystem 的
语义检索层；scheduler 只负责唤醒；主动系统从
`trigger → topic → send` 改为 `observation → opportunity → drive → intent → action`；
任何主动消息都必须能从数据库反向追溯完整因果链。

---

## 1. 现状架构（as-is）

### 1.1 运行形态总览

运行时由四个常驻/周期组件组成：

| 组件 | 形态 A（默认） | 形态 B（loop 常驻） | 职责 |
|---|---|---|---|
| 决策引擎 | cron `*/15 * * * *` → `scripts/chiguo-tick.sh` → `chiguo_daemon.py --compact` | systemd `chiguo-daemon.service` → `--loop 900 --compact` | 每 tick 评估「要不要主动发」 |
| 生成后端 | tick 内按需 `node scripts/agent-run.mjs --send-mode` 或 bridge RPC | 同左（`_loop_send` 内聚） | 决策 JSON → 微信文本；回复分析 |
| 微信桥 | `wechat-bridge/bridge.mjs`（Node 常驻，HTTP 127.0.0.1:18790） | 同左 | 收消息路由 / 发消息 / 常驻 agent RPC（`agent-rpc.mjs`） |
| 监控 | cron `--alerts-push`（alert-cron.sh）+ 手动 `--stats/--monitor` | 同左 | 统计/告警/健康 |

互斥语义：cron tick 与 `--loop` 由运行期互锁（`cli/dispatch.py: startup_conflict`，
`chiguo-tick.lock` flock 与 `chiguo_loop.pid` 双向识别），防止双发送。

### 1.2 Python 决策链（主动发送侧）

```
chiguo_daemon.py（薄 facade）
 └─ cli/parser.py（36 参数）→ cli/dispatch.py（表驱动分发）
     ├─ 轻量子命令（不经引擎）：--attention / --schedule-recall / --schedule-change / --memory-search
     ├─ handlers：anniversary / rotation / send（record-send, send-result）/ conversation /
     │   break / tune / consolidate / monitor / health / light
     └─ 需引擎分支：--status / --user-msg / --loop / 默认 evaluate
 └─ decision/engine.py DecisionEngine
     = DecisionCoreMixin + ContextMixin + AccountingMixin + LoopSenderMixin + DecisionEngineBase
```

**evaluate() 主链**（`decision/core.py`）：

```
① _maybe_reload_config()（loop 模式 toml mtime 热重载，RELOADABLE_COMPONENTS 注册表重建）
② _fetch_play_proof(now)   ← 锁外网络 IO（网易云近期播放，仅静默窗口内）
③ state_lock 内：
   state._load()  ← 锁内从磁盘重载（防并发丢更新）
   _tick(now)     ← 情绪推进（半衰期/弹性衰减/交互矩阵/OU 噪声/基线漂移；含 NTP 前跳封顶）
   infer_user_state(now)  ← Bayesian 6 状态 + 转移矩阵 + 信息增益
   sync_quiet_window(now) ← 生物钟双桶学习窗口同步
   _apply_play_proof(now, plays) ← 听歌反证（睡眠窗口内播放 → 压 sleeping 置信度 + 反哺学习）
   can_send(now, quiet_ok=...)    ← 门控：日限额/最小间隔/低能量/静默窗/busy 抑制/Bayesian 睡觉
     └─ 被拦时二次门禁探测（escape_valve / must_send / reminder 突破钥匙）
   evaluate_triggers(state, now)  ← 14 种触发评估（见 1.4）
   _build_context(trigger, ...)   ← 情境/人格指引/话题/课表提示/instruction 组装
   on_character_message(...)      ← 记账：energy -20、anxiety +2、messages_today+1、Hawkes 事件
   adapt_personality / update_emotion_baseline
   save()  ← 原子写 chiguo_state.json（失败 → 阻断 send，输出 idle(state_save_failed)）
④ _log(decision)  ← 追加 chiguo_decisions.jsonl（带 contract=1 schema 校验）
```

**输出**：`{"action": "send", "msg_id", "trigger", "intensity", "context", "state", "bayesian"}` 或
`{"action": "idle", "reason", "state", "next_evaluation_at"}`（idle reason 枚举见 `decision/idle.py`）。

**记账/回执**（`ops/engine_ops.py`）：
- `record_user_message(text, analysis_json, recv_id)`：被动侧记账（含 recv_dedup 升级语义）；
- `record_send_text(msg_id, text, trigger, intensity)`：发送成功后的文本归档 + reply_stats；
- `record_send_result(msg_id, status, error)`：success / failed（退款 refund_send）/ uncertain
  （轻量清算 messages_without_reply）；幂等由日志尾窗去重 + `refunded_msg_ids` FIFO(200)；
- `_mem0_autowrite`：对话后自动写入 mem0（LLM 事实提取，24h 文本 hash 去重）。

### 1.3 状态与持久化现状

**canonical-ish 状态文件**（全部 gitignore，0600，多数原子写）：

| 文件 | 写者 | 内容 |
|---|---|---|
| `chiguo_state.json`（+`.bak`/`.tmp`/`.lock`） | `state/persistence.py` | 情绪+冷却+生物钟+人格+人格历史+pending_topics+bayesian 缓存的整份 JSON（`_version=10`，SHA256，mono/wall 锚点对，tick_seq CAS） |
| `chiguo_decisions.jsonl` | `decision/core.py::_log` | send/idle/recv/recv_upgrade/send_result 五类决策记录 |
| `chiguo_messages.jsonl` | `ops/engine_ops.py` | 人读对话归档（in/out）|
| `chiguo_state_audit.jsonl` | `state/persistence.py` | 状态损坏/恢复审计 |
| `chiguo_events.jsonl` | `chiguo_rotation.py` | 轮转等事件时序 |
| `chiguo_alerts.json` | `monitor/alerts.py` | 告警生命周期 |
| `data/chiguo_memories.json` | 手动/`--schedule-change` | reminder/habit 条目（内容唯一事实源）|
| `break_state.json` | `--break` CLI | 寒暑假手动覆盖 |
| `schedule_cache.json` | `schedule/parser.py` | xlsx 解析缓存 |
| `schedule_overrides.json` | `schedule/override_store.py` | 停课/调课/加课/考试周/提醒覆盖 |
| `schedule_plan.json` | `schedule/plan_store.py`（replan 生成） | 每日计划与触发权重修饰 |
| `schedule_clarify.json` | `wechat-bridge/schedule.mjs`（**JS 私有**） | 追问澄清记录（6h）|
| `anniversaries.json` | `schedule/anniversary.py` | 纪念日 |
| `holidays.json` | `update_holidays.py` | 节假日覆盖 |
| `netease/netease_health.json` | `netease/service.py` | 网易云健康/配额 |
| `netease/netease_cache.json` / `recent_play_cache.json` | `netease/bridge.py` | 推荐/播放缓存 |
| `netease/netease_cookie.txt` | `netease/bridge.py` | 登录 cookie（600） |
| `agent_health.json`（+lock） | `scripts/agent_health.py` | agent 假死状态机 |
| `data/mem0/`（qdrant + history.db） | `memory/mem0_backend.py` | mem0 语义记忆库 |
| `~/.chiguo/session-*`（activity/rotate-last/backups） | bridge / tick.sh | 会话轮换 |
| `~/.pi/agent/sessions/**` | Pi 本体 | 会话转录（三个会话 id + 4 个 schedule 会话） |
| `logs/agent-run.log` | agent-run.mjs | agent 遥测 |
| `chiguo_loop.pid`、`~/.chiguo/run/chiguo-tick.lock` | loop/tick | 形态互斥锁 |

**并发模型**：`chiguo_locks`（fcntl 可重入 flock，5s 超时降级无锁 + 审计）；写前 `.bak`；
`state/persistence.py` 完整 RMW 临界区（锁内 `_load` 重载 → 修改 → CAS tick_seq → atomic_write + verify）。
所有敏感路径（cron 新进程）靠「锁内重载 + 磁盘 CAS」防丢失更新。

### 1.4 触发 / 话题 / 门控（决策的核心业务逻辑）

**14 种触发** = 情绪类 8（`lonely_low/mid/high`、`anxiety`、`playful`、`reflect`、`longing`、`comfort`）
+ 仪式类 6（`special`、`morning`、`night`、`meal`、`memory`、`follow_up`）——
`trigger_types.py` 为枚举单一事实源；`chiguo_trigger.py::evaluate_triggers` 主链：

```
逃生阀（longing_break_eligible）→ 直接返回 longing{escape_valve}
→ backoff_level（未回复退场：normal/backing_off/silent；silent 下仅放行窗口内 reminder）
→ _collect_ritual_candidates / _collect_followup_candidates / _collect_emotion_candidates
→ A3 日程乘数×抖动 → A6 repeat 阻尼 → A2 回复率反馈 → A4 三段激活
   （低段沉默 / 中段加权随机 / 高段 must_send 必发；reminder 窗口内优先）
→ 安全阀降级（crash 后 24/48h）→ Trigger(type, intensity, data)
```

**8 路话题源**（`chiguo_topics.py::TOPIC_REGISTRY`）：schedule / memory / solar_terms /
anniversary / preference_followup / netease / weather_season / general——
只在 lonely_low/mid（+ netease 跨触发）注入，产出 `{type, hint, tone, data}` 拼进 `context.situation/instruction`。

**门控**（`state/limits.py::can_send`）：日限额（活跃 4 / 沉默 2）、最小间隔 30min、
低能量 12（孤独变化率紧急覆写）、静默窗口（生物钟学习 > 配置默认）、busy 抑制、Bayesian 睡觉阻塞；
三把突破钥匙：longing 累积溢出 / 72h 逃生阀 / must_send 高段必发（含 reminder 复用）。

**情绪推进**（`state/emotion.py::tick`）：elastic_recover（弹性半衰期）→ 交互矩阵 → OU 噪声（默认关）
→ 基线漂移淡忘；事件响应（`state/interaction.py::on_user_message` / `on_character_message`）
含回复饱和阻尼（A10）、惯性阻尼（A11）、事件类型化 delta（B1）等灰度开关。

**Bayesian**（`chiguo_bayesian.py`）：6 状态（chatting/browsing/busy/sleeping/away/needs_care）
朴素贝叶斯 + EMA 在线学习 + 转移矩阵前向滤波（默认关）+ 信息增益门控（默认关）；
似然缓存随 state 落盘。

### 1.5 主动发送链（完整时序）

```
crontab */15 → scripts/chiguo-tick.sh
  ① flock 防重入；source agent-auth.sh（auth.json key → OPENCODE_API_KEY）
  ② 前置解析 OWNER（credentials.json userId → toml wechat_recipient）与 node 检查
     —— 失败早退（R7：必须在 --compact 之前，防幻影记账）
  ③ .venv/bin/python chiguo_daemon.py --compact  → 决策 JSON（idle → exit 0，~90%）
  ④ action=send → 写 session-activity-last → 生成链（有界循环 MAX_GEN_ATTEMPTS=2）：
     RPC 优先：curl --max-time 125 POST <bridge>/agent/prompt {text:决策JSON, mode:send}
       bridge 侧总预算 110s（排队≤30s + restart≤3s + ready≤10s + prompt）
       → AgentRpc.prompt（pi --mode rpc，会话 chiguo-send，每轮 restart 全新）
     失败 → spawn 回退：AGENTRUN_SESSION=chiguo-send AGENTRUN_ROTATE_SESSION=1
       node scripts/agent-run.mjs --prompt <决策JSON> --send-mode（pi -p，120s）
  ⑤ 生成失败 → sleep 5s 重试一次；仍失败：
     daemon --send-result <msg_id> --send-status failed（退款闭环）+ agent_health record fail
     → 连续 3 次 → state down + 微信告警「后端异常」+ 暂停探测
  ⑥ curl --max-time 35 POST <bridge>/send {to, text}
     ok → record_health success + daemon --record-send <msg_id> --text ... --trigger ...
     timeout_uncertain → 不退款不重发，--send-status uncertain 轻量清算
     prepare failed（context_token 过期 ≈35h）→ 退款 + 明确恢复指引
     bad_json（响应非 JSON）→ 按不确定处理；transport fail → send_fail + exit 1
```

### 1.6 被动回复链（wechat-bridge）

`handleMessage`（`message.mjs`）为唯一入口，路由顺序：

```
SDK inboundDebounce(4s 合并) → onMessage → writeActivity → handleMessage：
 ① 白名单门（isAllowedContact；缺省空 = 仅 owner）→ 非白名单固定拒答（零 LLM）
 ② owner 门：白名单非 owner → 仅 askChat（不进状态/记忆/命令）
 ③ recordUserMsg：daemon --user-msg <原文> --recv-id <uuid>（确定性记账，30s）
 ④ 澄清检查：schedule_clarify.json 有待澄清且非退出词 → 回到安排链路（或合并原意+回答）
 ⑤ 斜杠命令（detectSlashCommand）：/new /status /记忆 /记得什么 /help —— 纯 node，零 LLM
 ⑥ 特殊命令（detectSpecialCommand）：纪念日/放假/开学 → daemon --anniversary/--break/--schedule-change
 ⑦ 安排意图（detectScheduleIntent 词表命中）：
    extract（agent-run --schedule-extract，会话 chiguo-extract，180s）
    → verify（agent-run --schedule-verify，会话 chiguo-verify，180s）
    → daemon --schedule-change（30s 落盘）；信息不足 → 写澄清 → 追问循环
 ⑧ 聊天链：getAttention（daemon --attention 30s）+ getMemories（--memory-search 30s）
    → askAgentWithAttention → RPC（pi --mode rpc，会话 chiguo-main）或 spawn
      （agent-run --analysis-mode，180s）→ 一次产出 {分析 JSON + 回复}
    → analysis.recall 存在 → daemon --schedule-recall + 第二趟（会话 chiguo-recall，仅 spawn）
    → upgradeAnalysis（daemon --user-msg --analysis --recv-id，同 uuid 升级语义）
    → bot.reply（失败 → 「⚠️ 处理失败」+ recordAgentHealth fail）
```

会话模型：`chiguo-main`（回复，TurnQueue 串行 + 每日整点空闲轮换）、`chiguo-send`（主动，每轮全新）、
`chiguo-extract/verify/recall/replan`（安排链路独立会话）。RPC 常驻进程按 `mode|session` 分片，
pidfile 在 `~/.pi/agent/`，`agent_settled` 为回合终点。

### 1.7 Pi / agent 后端集成现状

- `[host].runner`：`agent`（默认，`pi` 二进制）| `command`（任意 CLI agent，统一契约
  `<cmd> --prompt <完整提示词> --mode <analysis|send|extract|verify|recall|replan>`，stdout JSON）。
- prompt 模板分散在 `scripts/agent-run.mjs`：`buildSendPrompt`（把决策 JSON 整段拼进 prompt）、
  `buildAnalysisPrompt`（要求输出 `<<ANALYSIS>>{...}<<END>>` 块）、`runSchedule` 四种模式模板。
- 人格注入：`--append-system-prompt` ×3（`迟菓人格-精简版.md` + `记忆用法.md` + `工具用法.md`），
  RPC 与 spawn 共用 `buildBaseAgentArgs`。
- 会话：`--session-id` 指定；`pi` 本体管理 session 文件 / compaction / 上下文。
- 未使用：Pi extension（`pi.on()`、`before_agent_start`、`tools`、`skills`）目前为零——
  所有上下文注入靠 Python 侧拼 `context.situation/instruction` 或 bridge 拼 prompt 前缀。

### 1.8 记忆子系统（mem0）

- `memory/base.py`：`MemoryBackend` 抽象（四原语 available/search/random_memory/stats）+
  基类包装（Ebbinghaus 遗忘权重 C1 巩固计划 / C2 复习强化 / B2 情绪标签加权）。
- `memory/mem0_backend.py`：mem0ai（LLM=opencode 网关 deepseek-v4-flash 事实提取；
  embedder=本地 ollama qwen3-embedding:0.6b；vector=qdrant 嵌入式 `data/mem0/`）；
  读链 `_MEM0_TIMEOUT=10s`、写链 `_MEM0_ADD_TIMEOUT=30s` + 连续 3 次熔断、60s 节流自愈。
- 消费面：daemon 触发层（`random_memory`/`user_relevant` 10s 超时）、话题源（memory/preference_followup）、
  回复侧 `--memory-search`、C1 空闲巩固（默认关）。
- **mem0 当前被视为话题素材来源与检索层，但写侧（LLM 提取的事实）事实上成为部分长期事实的
  实际载体**——没有 provenance/置信度/supersession 结构，覆盖与去重全靠 mem0 内部。

### 1.9 schedule / netease 数据面

**schedule/ 包（18 文件，时间安排域）**——读路径干净：`sources.load_sources`（唯一读文件函数）
聚合 5 个来源 → `day_plan.py` 纯函数（`resolve_classes` 应用 move/add/cancel 例外、
`availability_base`/`class_load_adjust`/`bayesian_adjust`）→ 由 `state/schedule.py::ScheduleMixin`
暴露 `availability()` / `schedule_status()` 给决策层；`attention.py`（T1/T2/T3）注入回复侧；
`recall.py` 供 `--schedule-recall`。

写路径唯一入口 `api.py::ScheduleApi`（validate→normalize→check→materialize→persist），
落盘 `schedule_overrides.json`（OverrideStore）、`anniversaries.json`、`break_state.json`；
`replan.py`（cron `*/15` 判脏）调 agent 生成 `schedule_plan.json`（触发权重修饰），
由 `state/schedule.py::trigger_scale_now` 消费。`migrations.py` 负责历史迁移
（countdown/toml exam_weeks/special_dates → overrides）。

**netease/ 包（2 文件，听歌联动域）**：`bridge.py` 数据面（HTTP/缓存/QR 登录/播放记录，
运行时文件锚定 `netease/`）；`service.py` 策略层（健康探针/登录失效/降级链/共享日配额/
随机选源/peek-consume 两阶段/`fetch_play_proof` 单入口）。消费面：
- 决策链：`PlayProofProvider`（**位于 `schedule/facade.py`，依赖方向倒挂**）→
  `_fetch_play_proof`（锁外）/`_apply_play_proof`（锁内，反哺生物钟）；
- 话题源：`TopicPicker._netease_music_topic` → `peek_music_topic` / 选中后 `consume_*`；
- 监控：`monitor/health.py` 读 `netease_health.json`。

**审计确认的该域结构性问题**（并入 §2.2）：
- `schedule_status` 双实现（`schedule/facade.py` 与 `state/schedule.py` 近乎逐行重复、
  输出形状已分叉）；
- `ChiguoState` 进程内多份陈旧 store/holiday 实例（`trigger_scale_now` 用构造时载入的
  `override_store`，`_resolved_for` 每次新建 sources——三份状态新鲜度不一致）；
- xlsx→cache 只在 `ChiguoState` 构造时刷新（`--loop` 形态替换课表需重启进程；
  `--attention` 等轻量 CLI 根本不刷新）；
- schedule 域声明「api.py 唯一写入口」但 `schedule_clarify.json` 由 bridge JS 直接读写
  （跨语言绕过锁）；`schedule_overrides.json` 无文件锁；
- `break_state.json` 4 套解析 / `holidays.json` 3 份进程内实例 / `semester_start`
  默认值硬编码 4 处——同一文件多解码器；
- 网易云配额/健康文件跨进程丢失更新（service 构造时快照 health，consume 整文件覆写，无锁）；
- 降级语义不一致（sources 回退内嵌节假日数据 vs facade 置 None 后 fail-open）；
- 死代码：`api.plan_store`、`facade.plans`、`state/schedule.exam_season_now`、
  `parsing.py` 三处重复 fallback、`override_store.json.bak` 无恢复读取方。

另注：`netease/service.py` 与 `monitor/*` 使用 PEP 758 无括号 `except` 语法（项目 3.14-only
为既定约定，非缺陷，但任何 3.13 环境会导致 netease 整包 SyntaxError）。仓库根存在
未被引用的 `xskb_2.xlsx`（9/25，比配置指向的 `data/xskb.xlsx` 新）——数据源摆放疑似失误。

### 1.10 监控 / 告警 / 轮转

- `monitor/`（base/stats/alerts/health/helpers）：流式解析 `chiguo_decisions.jsonl`，
  产出 stats（发送/回复/情绪趋势/时段分布/D1 proactive_stats）、alerts（12 类异常，生命周期
  active→acknowledged→resolved，持久化 `chiguo_alerts.json`）、health（daemon 活性/磁盘/内存/mem0）。
- `chiguo_daemon.py --alerts-push`（cron alert-cron.sh）经 bridge /send 推新增 critical/warn。
- `chiguo_rotation.py`：按月轮转 decisions/messages/state_audit → `archive/`，轮转事件写
  `chiguo_events.jsonl`；daemon 启动与 `--rotate` 触发。
- `scripts/agent_health.py`：agent 假死状态机（fail_streak / down / transition 告警恢复）。

### 1.11 测试体系现状

- pytest 驱动 109 个 py 测试文件 + 15 个 mjs/sh 脚本测试（`scripts/ci-test.sh` 为唯一入口，
  计数动态化；本地与 GitHub Actions 同入口）。
- `tests/conftest.py` 提供全局隔离：CWD 固定项目根、env 快照还原、random 状态还原、
  `frozen_now` 时钟桩、状态文件污染守卫（会话结束断言项目根无新增行）。
- 测试形态：大量单元测试 + `test_integration.py` 类集成测试（临时目录注入 `_base_dir`）；
  少量 eval 型测试（`test_proactive_eval.py`）。scenario 级端到端（多事件因果链）目前为零。

---

## 2. 问题清单（为什么必须 v2）

> 按「结构性问题」与「具体缺陷」两级组织；具体缺陷是结构问题的表征，不是驱动因素。

### 2.1 结构性问题

**P1. Action 缺位：send/idle 二值压缩了一切主动性**
- 主动系统中唯一可能的输出是「发一条消息」或「不发」。无法表达 defer（今晚不合适，明早再提）、
  follow_up（发了没回复，明天换角度再试）、check（先确认一件事再决定）、record（记下但不动）、
  reflect（内部反思）。所有「延迟/等待/多步」语义都被迫折叠进情绪数值与门控副作用。
- 「未完成事项」无处安放：reminder 塞在 `data/chiguo_memories.json` 的
  habit/reminder 两种形态里，follow_up 塞在 `chiguo_state.json.pending_topics`（48h 即过期），
  二者都不是一等公民。

**P2. 状态没有单一权威，没有事件历史**
- 情绪/冷却/人格/生物钟/pending/bayesian/去重标记挤在一个 JSON；schedule/netease/health/clarify
  各自为政；跨模块状态变更没有统一记录（只有 decisions 记录决策结果，不记录输入事件）。
- 「为什么昨天发了那句话」无法回答：decisions.jsonl 只存决策输出与快照，没有 causation/correlation
  链——触发来源（哪个 source、哪次观测）、当时 relationship 状态、planner 选择理由均不可回溯。
- 每个文件各自实现原子写/锁/迁移（虽有 `chiguo_atomic`/`chiguo_locks` 收敛，但语义仍分散），
  状态版本各自演进（STATE_VERSION、cache_version、各文件无版本）。

**P3. Python↔Node↔Pi 协议手工对齐，漂移无检测**
- 决策 schema：`decision_schema.py` 与 `agent-run.mjs::DECISION_SEND_FIELDS` 靠注释 keep-in-sync；
- 超时预算：tick curl 125s ↔ bridge 110s ↔ RPC 120s ↔ restart 3s ↔ ready 10s ↔ /send 30s ↔curl 35s，
  全靠注释互引人工维持不等式；
- 双路径分叉：同一「回复」在 RPC 与 spawn 两套模板/解析/错误语义；recall 第二趟仅 spawn；
- 输入输出全走 CLI 子进程 + stdout JSON（每条 owner 消息 4-5 个 Python 子进程），
  参数校验靠 `cli-dto.mjs` argv 白名单补齐。

**P4. trigger/topic 模型把「世界」硬编码进了决策**
- 天气、课表、网易云、节假日、纪念日、记忆都是「话题源/触发条件」而非「世界观测」——
  它们无法独立于「要不要发消息」存在，无法被 Memory/Agenda 消费，也无法组合
  （primary opportunity + secondary context）；
- 情绪 → 发送的直接通路（阈值触发）导致「情绪是发送的原因」——v2 中情绪应只影响
  Drives，由 planner 决定。

**P5. Pi 能力未用：Cheguo 自建了半套 agent runtime**
- Python 侧拼 `context.instruction/situation` 来「教育」LLM（本质是 prompt 工程散落在 Python）；
- bridge 侧拼 attention/memory 前缀；RPC/spawn 两套会话管理；轮换逻辑自建。
- Pi 已提供 extension 事件（`before_agent_start`、`context`、`message_end`、工具注册、
  `appendEntry`）、session/compaction、RPC——Chiguo 应通过一个 Pi extension 注入
  人格/关系/议程/记忆/意图，而不是让每个调用点各拼各的 prompt。

**P6. 记忆分层缺失**
- mem0 是唯一记忆载体（事实上成为 canonical 的一部分），但没有：结构化事实（谁、何时、
  置信度、有效期）、supersession（「不喜欢A乐队了」应 supersede 旧事实而非并行堆叠）、
  assistant self-memory（我主动说了什么/是否被回应/是否需要 follow-up）；
- `data/chiguo_memories.json`（reminder/habit）与 mem0 平行存在，语义重叠但机制不同。

**P7. 情绪数学即公共 API**
- 全仓大量代码直接读写 `state.emotion.loneliness` 等；情绪维度、冷却字段、personality
  结构成为事实上跨模块契约——模型替换/维度调整的爆炸半径巨大。

### 2.2 具体缺陷（表征，供迁移时清理）

按子代理审计与直读结果，列出已确认的缺陷/技术债（不含全部历史审计项）：

- **bridge（wechat-bridge/*.mjs）**：
  - `firstAnalysis` 的 `runOverride.exec` 未守卫（潜在 TypeError，现被调用方掩盖）；
  - `globalThis.__agentRpc` 隐式单例（两处懒建无同步）；`fallbackTurnQueue` 使
    「同会话禁并发」契约只在约定层面成立；
  - `timeout_uncertain` 判定依赖精确匹配 `^timeout \d+ms$` 错误串格式（bug.send 文案
    格式变化会把不确定误判为明确失败 → 退款 + 制造重发窗口）；
  - 陌生人的被拒消息也会刷新 `session-activity-last`（白名单门在 writeActivity 之后），
    会话轮换的空闲保护被绕过，chiguo-main 上下文可被持续膨胀；
  - `runCli` 死代码；`export {spawn}` 纯 DI；`runWithAttention` 等测试专用路径；
  - 澄清链与聊天链两次 `queue.run` 间存在消息交错窗口；clarify 读取在队列外（TOCTOU）；
  - 鉴权细节：`isLocalOrigin` 缺失 Origin 返回 true、token 普通不等比较（非 timing-safe）、
    body 上限不预检 Content-Length；token 泄漏即本机任意进程可消耗 LLM 配额；
  - 子进程 cwd 不一致（daemon 调用继承 wechat-bridge/，special command 用 repo 根），
    靠脚本位置锚定状态文件的隐式契约。
- **runner/state**：
  - `state/persistence.py` 单文件 483 行承载序列化+迁移+锁+CAS+审计；
  - 迁移逻辑交织在 hydrate 函数中（`_hydrate_cooldown` 兜底迁移旧事件、`migrate_circadian_v8`）；
  - 状态文件字段只增不删（历史遗留字段靠默认值兼容）；
  - 情绪/冷却字段是事实上跨模块契约（P7）。
- **决策链**：
  - `decision/core.py::evaluate` 承担 RMW 编排 + 二次门禁探测 + 逃生阀 + reminder 特殊路径，
    认知负荷极高（单函数 ~250 行）；
  - 话题注入条件散落（lonely 分支 / netease 跨触发 / follow_up 专用路径）；
  - `_build_context` 拼 200+ 行中文 prompt 指引（人格铁律/层指引/能量注解……）。
- **schedule/netease**（子代理 B 审计，见 §1.9）：
  - `schedule_status` 双实现且输出分叉；`ChiguoState` 内陈旧 store 实例；
    xlsx 缓存只在构造时刷新；`schedule_clarify.json` 跨语言直写绕过锁；
    `schedule_overrides.json` 无文件锁；同一文件多解码器；netease health/配额文件
    跨进程无锁覆写；降级语义不一致（fail-open vs fail-closed）；多处死代码。
- **跨语言**：
  - `chiguo-tick.sh` 316 行 shell 内嵌 Python 解析 JSON（多处 `"$PY" -c`），可维护性差；
  - agent-run.mjs 485 行同时承载解析/模板/runner 抽象/CLI；
  - 超时预算不等式（125/110/120/3/10/30/35 秒七个数）跨四种语言手工保持。
- **测试**：
  - 无 scenario 级测试；跨进程行为（cron↔bridge↔daemon）靠 mock 隔离，真实链路无覆盖。

---

## 3. 目标架构（v2）

### 3.1 核心模型

```
External Sources / User Interaction
        ↓
     Events（持久化事件日志）
        ↓
  State Reducer（事件 → 物化状态）
   ┌────┼────┐
   ↓    ↓    ↓
 World Relationship Self
 State   State    State
   └────┼────┘
        ↓
     Memory（结构化事实 + 语义检索 + provenance/supersession）
        ↓
  Opportunities（世界观测 × 状态 → 可行动契机）
        ↓
  Drives / Agenda（当前想做什么 + 一段时间内关注什么）
        ↓
     Planner（机会+驱动力+议程+关系 → Intent）
        ↓
    Pi / LLM（角色化实现）
        ↓
      Action（send_message/record_memory/schedule_check/follow_up/wait/...）
        ↓
   Action Result → Event（闭环）
```

关键变化：
- **Action 是基本单位**，消息只是 `action.type = send_message`；
- **事件流 + 物化状态双层**：`events` 表为因果链载体，状态表为当前快照；
- **scheduler 不再决策**：cron/loop/heartbeat 只产出 `wake` 事件，统一进入 `autonomous_turn()`。

### 3.2 SQLite canonical store

- 单一数据库 `~/.chiguo/chiguo.sqlite`（WAL、synchronous=NORMAL、foreign_keys=ON、busy_timeout=5000），
  schema_version + migrations 表 + integrity check；
- 逻辑域（示意，实施时按需裁剪）：
  `meta / events / sessions / messages / affect_state / relationship_state / self_state /
  user_state / commitments / threads / opportunities / drives / intents / actions /
  world_observations / source_observations / memories / memory_links / schedules / deliveries`；
- JSON/TOML/YAML 只保留：静态配置、人格配置、插件配置、部署配置。

### 3.3 Pi = Agent Runtime / Chiguo = Character & World Runtime

- Pi 负责：session、context、模型、工具、compaction、会话转录、LLM 执行、hooks；
- Chiguo 负责：affect、relationship、memory、world state、opportunities、drives、planner、
  commitments、threads、proactive policy；
- 通过 **一个 Pi extension**（`chiguo-pi-extension`）在 `before_agent_start`/`context` 注入
  personality/relationship/agenda/memory/intent/world context；
- Python 与 Node 的边界收敛为：Python 负责状态与规划（SQLite），Node/Pi 负责对话执行。

### 3.4 模块边界（目录）

```
chiguo/
├── app/            # application services：runtime/autonomous/conversation/actions
├── domain/         # affect/relationship/memory/agenda/commitments/threads/planning
├── storage/        # sqlite/{schema,migrations,repositories} + events/
├── sources/        # weather/schedule/holiday/netease/wechat —— 只 observe()
├── integrations/   # pi/、wechat/
├── personality/    # 人格静态配置（保留）
├── cli/            # debug/runtime interface
└── tests/          # unit/integration/scenario 三层
```

（不机械搬运文件；以实际依赖重组。旧模块凡被新架构完全取代即删除，不保留兼容 adapter。）

### 3.5 CLI（debug/runtime interface）

`chiguo status / db {status,migrate,integrity,backup} / events recent|show <id> /
relationship / affect / agenda / commitments / threads / autonomous-turn /
replay <range> / shadow <range>`；`status` 覆盖 affect、relationship、open commitments/threads、
agenda、recent opportunities、last autonomous turn/action/proactive message、db 状态、agent health。

### 3.6 replay / shadow

- `chiguo replay <range>`：读历史 events 重跑 state/opportunities/drives/planner，不发送；
- `chiguo shadow <range>`：新 planner 在线运行、只记录 decision 不发送，
  与旧系统对照（why/what opportunity/what drive/what intent/what would have been sent）。

### 3.7 Event 模型

事件是所有重要外部输入与 agent 行为的统一载体。字段：

| 字段 | 说明 |
|---|---|
| `event_id` | 全局唯一（建议 ULID：时间可排序 + 无协调生成） |
| `type` | 点分命名：`message.received`、`message.sent`、`message.delivery_failed`、`schedule.created`、`schedule.completed`、`weather.changed`、`music.observed`、`affect.updated`、`relationship.changed`、`commitment.created`、`commitment.resolved`、`intent.created`、`action.started`、`action.completed`、`wake`、`user.state_inferred` … |
| `occurred_at` | 事件在现实中发生的时间（如用户发消息的时刻） |
| `observed_at` | 系统观察到它的时间（补报/批处理时可能晚于 occurred_at） |
| `source` | 产生者：`wechat` / `user` / `scheduler` / `weather` / `schedule` / `netease` / `planner` / `executor` / `memory` |
| `correlation_id` | 同一 interaction / thread / workflow 的关联 ID（如一次主动联系的完整链） |
| `causation_id` | 直接导致本事件的上游 `event_id`（因果链的边） |
| `payload` | JSON（自由形状，按 type 约定） |

写入规则：所有 reducer 的输入必须是事件；所有 action 的结果必须回写为事件。
因果链目标：`user message → commitment → schedule event → opportunity → drive → intent →
action → message.sent → user reply` 每一跳都有 `causation_id`，`/status` 与 `chiguo events show`
可沿链回溯。

### 3.8 Opportunity / Drive / Intent / Action 模型

**Opportunity（机会）**——世界观测 × 状态推导出的「可行动契机」：

```
id, kind,                        # exam_finished / weather_changed / long_silence /
                                 # open_thread / anniversary / commitment_due / music / ...
novelty, relevance, urgency,     # 0..1 评分（planner 的输入信号，不是发送决策）
emotional_affordance,            # 0..1 情感可供性
expires_at,                      # 过期即作废（禁止无限期复用）
observation_event_id,            # 来源观测（因果链）
status,                          # open / consumed / expired / dismissed
```

一次 planner 运行消费一个 **opportunity context**：primary opportunity + secondary cues
（其它 opportunity）+ relationship state + recent thread + world state。允许组合，
不再「从 8 个 source 里随机选一个 topic」。

**Drive（驱动力）**——agent 当前「想做什么」：`care / curiosity / reconnect / playfulness /
loneliness / unfinished_business / reflection ...`。由 affect、relationship、unfinished
threads/commitments 等评估产生；`drive` 是情绪与行为之间的缓冲层——情绪不直接触发发送。

**Intent（意图）**——planner 的输出：`check_in / share / follow_up / comfort / remind /
celebrate / defer / wait / reflect / record ...`。携带 `why`（引用的 opportunity/drive IDs）
与生成指引（语气/素材/时限）。`Intent` 是「为什么发这条消息」的语义答案。

**Action（行动）**——执行单元（消息只是其中一种）：

```
Action {
  id, type,        # send_message / record_memory / schedule_check / follow_up /
                   # wait / defer / observe / reflect
  status,          # pending / started / completed / failed / cancelled / deferred
  intent_id, cause_event_id, correlation_id,
  created_at, started_at, completed_at,
  input, output, error
}
```

delivery 结果（`message.sent` / `message.delivery_failed` / `message.uncertain`）
再次形成事件与 `deliveries` 行，驱动 relationship/memory 更新。

### 3.9 记忆模型

分层：

```
semantic memory      # 事实（用户偏好、人物、地点…）
episodic memory      # 经历（某天发生了什么）
relationship memory  # 关系里程碑与模式
user facts           # 用户画像事实（可冲突、可 supersede）
commitments          # 未完成承诺（一等公民，见 domain/commitments）
assistant self-memory# 我为什么主动联系、谈了什么、用户是否回应、是否需要 follow-up
```

每条长期记忆带 provenance：`source / origin / confidence / observed_at / created_at /
updated_at / valid_from / valid_to / status(active|stale|superseded|forgotten|conflicted)`。
同一事实更新 = 新 observation + 旧记录置 `superseded`（保留历史，不覆盖不删除）。
SQLite `memories` 为 canonical；Mem0 保留为语义检索索引（`memories.mem0_id` 关联），
embedding 永远不是事实来源；Mem0 不可用时 structured state 与核心运行不受影响。
普通聊天不逐句进长期记忆——写入需经 memory policy（确定性规则 + 可选 LLM 提取候选）。

### 3.10 Source 插件接口

```
class Source(Protocol):
    name: str
    def observe(self, now) -> list[Observation]: ...
```

`Observation` = `{type, source, observed_at, expires_at, payload}`（对应 §3.7 的一条
`*.observed` 事件）。**Source 只提供事实**：不允许决定 `send=true`、不允许决定触发类型。
内置来源：`schedule`（课表/考试周/假期，来自现有 schedule 数据面）、`holiday`、
`netease`（听歌）、`weather`（新，可选，配置 API 后启用——现状的「weather_season」
只是按月模板，不是真实天气源）、`commitments`（到期检查）、`wechat`（收发事实）。

### 3.11 Pi 集成方式

- Pi = Agent Runtime（session/context/compaction/tools/transcript/LLM 执行），
  Chiguo = Character & World Runtime（affect/relationship/memory/world/opportunities/
  drives/planner/commitments/threads/proactive policy）。
- **上下文注入走 Pi extension**（`chiguo-pi-extension`）：在 `before_agent_start` 读取
  runtime 提供的上下文（personality 指令 + relationship 摘要 + 当前 agenda +
  relevant memory + 当前 intent + world context），以增量方式附加到 system prompt 段落；
  对话轮次结束经 `agent_end`/`message_end` 把 transcript 事实回写 runtime。
- bridge 不再手工拼 attention/memory 前缀；Pi 不再需要 Python 侧拼 `instruction` 文本。
- runtime ↔ Pi 的通道：本机回环（`chiguo serve`，仅 127.0.0.1）+ SQLite 单写者；
  取代「每条消息 4-5 个 Python CLI 子进程」的现协议（Phase 7 落地，Phase 3-6 期间
  保留旧 CLI 双跑）。
- 会话管理（session id / 轮换 / 备份）交给 Pi 原生机制；Chiguo 只维护
  `sessions` 表与语义 thread。

---

## 4. 迁移策略

按 Phase 2-10 顺序执行（每阶段结束跑全量测试）；各阶段完成情况见 §5 实施状态。

| Phase | 内容 | 出口条件 |
|---|---|---|
| 2 | SQLite schema/migration/storage/event model | db 单测绿；migrate 幂等；integrity 通过 |
| 3 | user message / send / schedule / world source 写为 events（旧逻辑照跑） | 事件双写对账一致 |
| 4 | materialized state：affect/relationship/commitments/threads/world | 状态可从 events 重建 |
| 5 | Opportunity/Drive/Intent/Action 最小 planner | 单测 + scenario 1-3 绿 |
| 6 | 主动系统切到 observation→opportunity→drive→intent→action | 主动链 scenario 绿；旧 trigger 退役 |
| 7 | Pi 接入新 runtime（extension 注入） | 端到端（bridge→pi→sqlite）绿 |
| 8 | 删除旧 JSON state / trigger state / 重复 scheduler/decision infra | 全库 grep 无旧状态写路径 |
| 9 | replay / shadow | 可对历史 events 重放 |
| 10 | 全面测试 + 文档 + dead code 清理 | pytest 全绿 + 文档同步 |

数据迁移：提供一次性 import 工具（旧 JSON/JSONL → sqlite），新 runtime 不永久支持旧格式。
```


---

## 5. 实施状态（分支 `refactor/v2-runtime`，2026-10-02）

### 5.1 已实现（测试全绿）

| 模块 | 内容 |
|---|---|
| `storage/sqlite/` | Database（PRAGMA/事务/完整性/备份/0600/损坏 fail-fast）+ 迁移器（checksum 防漂移、未来版本拒绝）+ v1/v2 schema |
| `storage/events.py` | EventStore（uuid7 时间有序 id；append/get/recent/since/after 游标/chain 因果链/caused_by） |
| `storage/repositories/` | messages/sessions、commitments、threads、opportunities、drives+intents、actions+deliveries、observations、memories(+links)、schedules、checkpoints、turns |
| `storage/dualwrite.py` | 旧链事件双写（Phase 3；旧链照跑，旁路失败静默） |
| `domain/affect` `domain/relationship` | 事件驱动纯模型（与旧引擎逐字段 1e-6 对账，37 测试） |
| `domain/planning/` | 机会发现 + 驱动力评估 + planner（Intent/Wait/Defer；score 门槛；why 可解释） |
| `app/runtime/` | reducer（事件→物化状态；原子游标）、extractor（确定性提取 承诺）、replay（副本重放零副作用）、serve（回环 HTTP /context /turn） |
| `app/autonomous/turn.py` | autonomous_turn 主循环（提取→归约→观测→机会→驱动→约束→planner→落库） |
| `app/actions/executor.py` | send_message 执行（生成→bridge /send→message.sent/delivery_failed 事件；生成可注入） |
| `sources/` | base 协议 + schedule/holiday/netease/weather 观测源（只提供事实） |
| `integrations/pi/` | Pi extension（before_agent_start 注入 /context；message_end 回写 /turn；fail-open） |
| `cli/`（新 `chiguo`） | db/events/status/commitments/threads/autonomous-turn/execute/tick/serve/replay |
| 测试 | scenario 1-6（考试承诺→follow_up、孤独单独不发送、天气仅 secondary、静默 deferred、mem0 不可用、库损坏 fail-fast）全绿 |

### 5.2 未完成（诚实清单）

- **Phase 8 删除旧系统**：清单与扫描证据见 `doc/DELETION_AUDIT.md`；执行被 6 项
  未移植功能阻塞（微信写命令链、告警推送、bridge 对话路由切换、会话轮换、import 工具、
  executor 生成路径 Phase 7 收敛）。**当前生产链路（cron tick + bridge + daemon）不变**。
- **Phase 7 完整收敛**：extension/runtime 已就绪，但 bridge 尚未切换到 `serve`；
  executor 仍经 `scripts/agent-run.mjs` 生成。
- **Phase 9 shadow 对照报告**：replay 已可用（可对历史事件重放），新旧并行对照报告未产出。
- **数据 import 工具**（`chiguo db import-legacy`）：未实现。
- **文档**：README 仍描述 v1 架构（`doc/SYSTEM.md` 为 v1 权威）；v2 文档为
  `doc/ARCHITECTURE_V2.md` / `doc/DATABASE.md` / `doc/MIGRATION_PLAN.md` / `doc/DELETION_AUDIT.md`。

### 5.3 运行时切换路径（待用户批准后执行）

1. `chiguo db migrate`（初始化 `~/.chiguo/chiguo.sqlite`）；
2. 双写观察期：`CHIGUO_EVENT_DUALWRITE=1`（默认开）+ `chiguo status` / `chiguo events recent` 对账；
3. shadow 期：cron 加 `chiguo tick`（不 `--execute`），`chiguo replay` 复盘历史窗口；
4. 切换发送：`chiguo tick --execute` 替代 `scripts/chiguo-tick.sh` 的决策+发送；
5. 完成 §5.2 的 6 项移植后执行 Phase 8 删除（见 deletion-audit）。
