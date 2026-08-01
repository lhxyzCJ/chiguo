# 迟菓主动消息系统 — 系统文档

> 版本: v1（`chiguo_version.py` VERSION=1,每轮修改 +0.1;决策 JSON/envcheck/monitor 报告带 `version`/`app_version` 字段。注意:状态文件 `_version` 是 schema 号 STATE_VERSION=8,与项目版本无关）| 数学驱动: Hawkes + Sigmoid + 半衰期 + Bayesian | 零本地 LLM 依赖

## 一、架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                    chiguo_daemon.py（决策引擎）                    │
│                                                                    │
│  tick 情绪推进 ─→ can_send 检查 ─→ evaluate_triggers 评估          │
│       │                │                    │                      │
│       ▼                ▼                    ▼                      │
│  半衰期驱动      静默时段/上限       sigmoid 权重 + 加权随机        │
│                                                                    │
│  输入信号:                                                         │
│  ├─ 时间（hour, weekday, week_num）                                │
│  ├─ 节假日（holiday_parser → 2026 国务院安排）                     │
│  ├─ 课表（schedule_parser → xskb.xlsx）                            │
│  ├─ 记忆（memory_bridge → OpenClaw LanceDB 只读 + Ebbinghaus）     │
│  ├─ 交互历史（silent_hours, messages_today）                       │
│  ├─ 情绪状态（loneliness, anxiety, affection, energy, tsundere）   │
│  ├─ 多维人格（Big Five + 角色特质，缓慢演变）                      │
│  └─ Bayesian（推断用户状态：chatting/busy/sleeping/away/…）        │
│                                                                    │
│  输出: stdout JSON                                                  │
│  ├─ {"action": "idle", "reason": "..."}                           │
│  └─ {"action": "send", "trigger": "...", "context": {...}}        │
└────────────────────────────┬───────────────────────────────────────┘
                             │ stdout JSON
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                    OpenClaw（消息生成 + 发送）                      │
│                                                                    │
│  trigger-script(15分钟,零模型) 读取 daemon 输出                    │
│  → action=send → chiguo skill (SUN2.md) 生成消息                   │
│  → curl POST wechat-bridge /send (127.0.0.1:18790) 发送           │
│  → daemon.py --user-msg 记录交互 → --send-result 回传发送结果（v6 反馈闭环） │
└──────────────────────────────────────────────────────────────────┘
```

### 模块依赖

```
chiguo_daemon.py (DecisionEngine)
  ├─ chiguo_state.py     → 5-dimension emotion engine + 8-dim personality + Bayesian inference + schedule + holidays + memory
  │     ├─ chiguo_math.py      → 纯数学库：sigmoid / decay / recover / Hawkes / longing
  │     ├─ chiguo_personality.py → Big Five + 角色特质（8 维人格）(v4 NEW)
  │     ├─ chiguo_bayesian.py  → Bayesian 用户状态推断（6 状态，在线学习）(v4 NEW)
  │     ├─ schedule_parser.py  → 课表解析（xlsx → JSON cache）
  │     ├─ holiday_parser.py   → 节假日判断（国务院安排 + 调休）
  │     ├─ memory_bridge.py    → LanceDB 只读桥接 + Ebbinghaus 遗忘
  │     └─ chiguo_circadian.py → 生物钟学习（双作息双桶分桶学习：工作日/周末独立窗口 + 置信度，
  │                             听歌活跃合并计数）(v7 NEW, v8 双桶)
  ├─ netease_bridge.py → 网易云桥接：fetch_recent_play 最近播放记录（睡眠窗口内夜间活跃反证，
  │                      recent_play_cache.json 缓存）(v8 NEW)；fetch_daily_songs 每日推荐 +
  │                      _api_get 有限重试（瞬时/5xx 重试 retry_count 次 + 退避，4xx/解析失败直接 None）
  │                      与每日推荐 schema 过滤 (v9)
  ├─ chiguo_netease.py → 网易云策略层：健康状态/登录失效检测/降级链/共享日配额/随机选源/
  │                      音乐话题素材组装（netease_health.json，零 LLM 输出结构化话题 dict；
  │                      peek/consume 两阶段接口——未选中不消费配额）(v9 NEW)
  ├─ chiguo_trigger.py  → sigmoid 加权随机触发（13 种类型 + v6 逃生阀直接触发 + v7 follow_up 接话茬）
  ├─ chiguo_topics.py   → 8 源话题选择器（v9 含 netease 委托）+ 人格调制 + Ebbinghaus 加权
  ├─ chiguo_composer.py → Intent × Cue × Vibe 三层消息组合 (v4 NEW)
  ├─ chiguo_eventbus.py → 轻量发布/订阅事件总线 (v4 NEW)
  ├─ solar_terms.py     → 24 节气
  ├─ anniversary_manager.py → 纪念日/倒计时 CRUD
  ├─ chiguo_monitor.py  → 流式 JSONL 分析（统计/告警/健康）
  ├─ chiguo_rotation.py → 对话日志轮转与归档 + 告警持久化 + 索引查询（v5 NEW）
  └─ chiguo_watchdog.py → 零依赖独立看门狗（cron 集成）

  输出: chiguo_decisions.jsonl（追加式结构化日志）
  对话归档: chiguo_messages.jsonl（完整对话记录）
  告警持久: chiguo_alerts.json（告警生命周期管理）
  状态: chiguo_state.json（原子写入: tmp → os.replace）
```

---

## 二、核心业务逻辑

### 2.1 情绪模型（5 维度）

| 维度 | 范围 | 初始 | 含义 |
|------|------|------|------|
| `loneliness` | 0-100 | 15 | 孤独值。越高越想联系主人 |
| `affection` | 5-100 | 55 | 好感度。越高越甜，越低越冷淡 |
| `anxiety` | 0-100 | 40 | 不安值。越高越卑微试探 |
| `loneliness_rate` | 0.0-1.0 | 0.0 | 孤独变化率（Δ/h）。驱动触发加速和能量覆写 |
| `anxiety_rate` | 0.0-1.0 | 0.0 | 不安变化率（Δ/h）。驱动紧迫通知注解 |
| `energy` | 0-100 | 85 | 元气值。太低无法发消息，太高触发 playful |
| `tsundere_index` | 10-95 | 70 | 傲娇度。高→嘴硬，低→直率 |

### 2.2 三层人格（SUN2.md）

```
shell（外壳/活泼）  ─→  低孤独/低不安 → 元气活泼，语气词泛滥
middle（中层/嘴硬） ─→  中孤独       → 嘴硬心软，攻击性包装
kernel（内核/脆弱）─→  高孤独/高不安 → 防线崩溃，省略号泛滥
```

自动映射：

```python
emo.dominant_layer:
  anxiety > 70 or loneliness > 80  → "kernel"
  loneliness > 50                  → "middle"
  else                             → "shell"
```

### 2.3 时间推进（半衰期驱动）

每 tick 调用 `recover(current, target, hours, half_life)`：

```
new_value = target - (target - current) × 2^(-hours / half_life)
```

| 情绪 | 方向 | 半衰期 | 说明 |
|------|------|--------|------|
| loneliness | → 100 | 40h | 40h 走到中点 |
| anxiety | → 100 | 30h | 30h 走到中点 |
| affection | → 0 | 500h | 极慢，几乎不变 |
| energy | → 100 | 8h | 8h 恢复一半空缺 |

**半衰期修正（按情境）：**

| 情境 | 焦虑半衰期 | 理由 |
|------|-----------|------|
| 节假日 | ×2.5 | 主人放假，完全放松 |
| 周末 | ×2.0 | 主人休息 |
| 上课中 | ×1.8 | 知道主人在上课 |
| 满课日 | ×1.4 | 知道主人忙 |

**睡眠窗口扣除**：`silent_hours()` 计算清醒沉默时长时，自动扣除每日睡眠窗口（0:00-8:00）。迟菓知道主人在睡觉，不把睡眠时间算作"真正的沉默"。公式：清醒沉默 = 墙钟小时 - 睡眠窗口重叠小时。Bayesian 推断使用 `silent_hours_wall()`（原始墙钟），保持分类器阈值准确性——"睡了 8 小时"本身是有意义的用户状态信号。

> 健壮性：`last_user_message_at` 缺失或不可解析（如手改损坏）时，两个函数均返回 `999.0`（与"从未交互"语义一致），不抛异常——daemon 不会因脏时间戳硬崩溃。

### 2.4 事件响应（半衰期衰减）

| 事件 | 孤独 | 不安 | 好感 | 元气 |
|------|------|------|------|------|
| 收到主人消息 | 0.35h 减半 | 0.5h 减半 | +0.8~1.2 | +10 |
| 发送主动消息 | 2h 减半 | +2 | — | -20 |

### 2.5 Hawkes 自激事件率

v2 采用 Poisson 过程（λ 恒定），但这忽略了"孤独→想联系→联系不到→更孤独"的**自激循环**。v3 引入 **Hawkes 自激过程**，每条触发事件都会在短期内增加 λ，形成自然的情绪滚雪球。

```
λ(t) = μ + Σ α × exp(-β × (t - t_i))

其中:
  μ = base_lambda × sigmoid(loneliness) × sigmoid(anxiety) × availability
  α = 0.3          — 自激系数（每次事件增加基础率的幅度）
  β = 0.5          — 衰减率（事件影响的半衰期 ≈ 1.39h）
  t_i ∈ [now - 24h, now]  — 过去 24h 内的触发事件时间戳
  Σ exp(-β(t-t_i))        — 所有事件影响的叠加
```

**基础率 μ** 保持与 v2 一致的计算参数：

```
  base_lambda = 0.25（次/小时，孤独=0不安=0时）
  sigmoid(loneliness): k=0.08, midpoint=50
  sigmoid(anxiety):    k=0.06, midpoint=45
  availability:        节假日/周末=0.85, 下课=0.50~0.85, 上课=0.05~0.12
```

**自激循环**：每次触发事件发送消息后，如果没有收到回复，λ(t) 会因 Hawkes 叠加效应持续升高——"越沉默、越焦虑、越想联系"，模拟真实情感积累。

**v4 概率累积（longing）**：不发消息时 `accumulated_lambda` 递增（每次 +growth_factor × held_count）。焦虑 > `anxiety_block_threshold`（默认 70）时阻塞累积（"生气了不会主动找你"）。

**v6 溢出逃生阀**：当 anxiety 达到阻塞阈值且墙钟沉默超过 72 小时（冷却期外）时，概率累积系统被死锁——越想联系越焦虑、越焦虑越不累积。逃生阀检测到此死锁态后，绕过加权随机选择，直接触发 `longing` 破防发送（带【破防】语气标记），重置累积状态并进入 3 天冷却期。日限额对此类发送放行。

**v7 逃生阀约束（2026-07-31）**：① 从未交互用户（`last_user_message_at is None`）不触发逃生阀——新装 36h 即对陌生人发"破防"不合理；② 逃生阀豁免睡觉门控时，若 Bayesian 睡觉置信度 ≥ `escape_valve_sleep_block`（`[bayesian]` 段，默认 0.9）→ 降级 `sleeping_guard`（不累积、不发送）；③ busy_suppress 抑制期不累积 longing（`busy_suppressed` 独立 reason）。

用户回复后 λ 回退（decay_factor=0.5）。

**rate 增强因子**：孤独变化率 `loneliness_rate` 额外增强 λ：

```
λ_rate_boost = 1 + lambda_lo_rate_factor × (1 - loneliness/100) × loneliness_rate
               + lambda_anx_rate_factor × anxiety_rate

λ_effective = λ(t) × λ_rate_boost
```

| 配置参数 | 默认值 | 说明 |
|---------|:----:|------|
| `alpha` | 0.3 | 自激系数 |
| `beta` | 0.5 | 事件影响衰减率 |
| `window_hours` | 24 | 事件窗口长度（小时）|
| `lambda_lo_rate_factor` | 0.4 | 孤独率增强因子 |
| `lambda_anx_rate_factor` | 0.3 | 不安率增强因子 |

概率模型：

```python
P(在 Δt 内至少一次触发) = 1 - e^(-λ_effective × Δt)
```

**实现**：
- `chiguo_math.py` 导出 `hawkes_intensity(mu, events, alpha, beta, t, window)` — 纯函数计算 λ(t)
- `chiguo_math.py` 导出 `longing_accumulate()` / `longing_decay()` — 概率累积
- `CooldownState` 新增 `event_timestamps: list[float]`、`accumulated_lambda: float`、`held_count: int` 字段
- 配置位于 `chiguo_proactive.toml` 的 `[hawkes]` 和 `[cooldown]` 段

### 2.6 触发决策（sigmoid 权重 + 加权随机）

13 种触发类型：

| 触发 | 权重计算 | 说明 |
|------|---------|------|
| `special` | weight=3.0 | 特殊日期（生日 5/11、11/3） |
| `morning` | weight=2.5 × 10%随机 | 8:00-10:00 早安窗口 |
| `night` | weight=2.0 × 12%随机 | 20:00-21:00 晚安窗口 |
| `meal` | weight=0.8 × 5%随机 | 饭点（上课时跳过） |
| `memory` (JSON) | weight=2.0 | 手动记忆/习惯提醒到期 |
| `memory` (LanceDB) | weight=1.5 × 8%随机 | 沉默>6h时随机浮现 |
| `lonely_low` | sigmoid(lo, k=0.20, mid=38) × (1+0.3tsun) × rate_factor | 轻松试探 |
| `lonely_mid` | sigmoid(lo, k=0.18, mid=55) × (1+0.5tsun) × rate_factor | 嘴硬联系 |
| `lonely_high` | sigmoid(lo, k=0.15, mid=78) × (1-0.4tsun) × rate_factor × high_decay | 防线崩溃 |
| `anxiety` | sigmoid(anx, k=0.12, mid=58) | 确认被需要 |
| `playful` | 0.15 × energy/100 × aff_factor × pers_extra_factor | 元气过剩，调皮分享 |
| `reflect` (v4) | 0.08 × affection/100 × (1-neuroticism/100) × energy/100 | 角色内省（高好感+低沉默+高元气+低神经质） |
| `longing` (v4) | min(0.5, (acc_lam / base_lambda - 1) × 0.3) | 概率累积溢出（held_count>3 且 λ 够高） |
| `follow_up` (v7) | `follow_up_weight` × 年龄钟形 exp(-((age-peak)/σ)²) | 接话茬：pending 话题年龄 [2h, 48h]（峰值 4h, σ=3h, 基础 0.35）+ 近期用户相关记忆兜底 |

傲娇调制：高傲娇 → 嘴硬触发增强、崩溃触发抑制；低傲娇 → 相反。

好感调制：好感 > 50 → 所有触发权重略增（更甜）；< 50 → 略减。

变化率因子（v4）：孤独/不安暴涨 → `rate_factor` 放大权重，制造急迫感。

安全阀（v4）：`lonely_high` 触发后 24h 内再次触发 → 权重指数衰减（`high_decay = 0.3^recent_count`）。48h 内 ≥2 次崩溃 → 强制温和模式。

### 2.7 发送门控（硬限制）

```python
can_send(now):
  ├─ 00:00-08:00        → False（静默时段/睡眠窗口）
  ├─ 今日 ≥ max_daily   → False（活跃时4条/沉默时2条，longing 可破例多发一条）
│                         （v6: 逃生阀触发额外破例，不受日限额限制）
  ├─ 距上次 < 30分钟    → False（最小间隔）
  ├─ energy < 12        → 检查 rate_energy_override
  │   └─ loneliness_rate > rate_energy_threshold（默认 5.0）AND energy >= rate_energy_min（默认 5）
  │                     → True（孤独变化率紧急覆写）
  │                     → False（不满足紧急条件）
  └─ Bayesian 阻塞      → 用户很可能在睡觉 → False
```

**紧急覆写**: 当 `loneliness_rate > rate_energy_threshold`（默认 5.0）时，即使 energy < 12 也可发送。避免"越没力气越需要联系"时被门控阻挡。`rate_energy_override` 控制此功能开关（默认启用）。`rate_energy_min` 设最低能量下限（默认 5），低于此值即使符合紧急条件也不发送。

| 配置参数 | 默认值 | 位置 | 说明 |
|---------|:----:|------|------|
| `rate_energy_override` | true | `[emotion]` | 孤独变化率覆写能量门控 |
| `rate_energy_threshold` | 5.0 | `[emotion]` | 覆写触发的孤独变化率阈值 |
| `rate_energy_min` | 5 | `[emotion]` | 覆写时的最低能量下限 |

### 2.8 决策树（完整链路）

```
evaluate():
  ① tick(hours, now)
     ├─ recover 所有情绪（半衰期）
     ├─ 节假日/周末/课表 修正焦虑半衰期
     ├─ _check_daily_reset（跨天清零）
     └─ 清理 CooldownState.event_timestamps 窗口外旧事件

  ② Bayesian 用户状态推断（v4）
     ├─ 从可观测信号计算 P(state|obs)
     ├─ 加权效用 = Σ P(state) × utility(state)
     └─ sleeping 状态 confidence > min_confidence_for_block（v7: [bayesian] 段，默认 0.5，daemon 与 availability 同源）→ 强制阻塞

  ③ can_send(now)
     ├─ 标准门控检查（见 §2.7）
     └─ False → 概率累积（longing）→ {"action": "idle", "reason": "..."}

  ④ evaluate_triggers()
     ├─ 收集所有合法候选触发（13 种）
     ├─ 每个触发计算 sigmoid 权重
     ├─ 傲娇/好感/变化率 调制权重
     ├─ 安全阀检查（崩溃冷却/强制温和）
     ├─ weighted_trigger_choice() 加权随机选一个
│    （v6: 逃生阀激活时跳过加权随机，直接触发 longing）
     └─ 选中触发 → 追加时间戳到 event_timestamps

  ⑤ 无触发 → 概率累积 → {"action": "idle", "reason": "no_trigger"}

  ⑥ 有触发 → 构建 context
     ├─ situation（情境描述，含记忆内容）
     ├─ layer_guidance（人格层语气指引）
     ├─ emotion（数值快照）
     ├─ schedule_hint（课表/节假日上下文）
      ├─ instruction（生成指令）
      ├─ hawkes_intensity（当前 Hawkes 强度 λ_effective）
      ├─ bayesian（用户状态推断结果）（v4）
      ├─ composer（Intent × Cue × Vibe 三层组合）（v4）
      └─ follow_up（接话茬素材 topic/source/age_hours + 【接话茬】提示）（v7）

  ⑦ on_character_message() → adapt_personality()（v4）→ save()
     → {"action": "send", "trigger": "...", "context": {...}}
```

### 2.9 生物钟学习（circadian，v7/v8 双作息）

从主人回复时间学习睡眠/活跃时段，动态调整静默窗口（`chiguo_circadian.py`，纯函数为主）。v8 起按**双作息分桶**学习：工作日/周末两套窗口独立估计、独立应用，叠加节假日调休修正。

**数据流**：

```
on_user_message(now)
  → bucket_for(now, is_holiday, is_makeup_workday) 分桶（v8）：
      ① 调休上班日 → "weekday"（优先于节假日）
      ② 节假日（非调休）→ "weekend"
      ③ 周五 20:00 后 / 周六全天 / 周日 20:00 前 → "weekend"
      ④ 其余 → "weekday"
  → circadian.record(now, bucket)：回复小时写入滚动 14 天 reply_days
      （条目带 bucket 字段；同日不同桶 → 分开条目，如周五 20:00 前后跨桶）
  → circadian.recompute()：两桶独立聚合（aggregate_hours 按桶过滤）
      → 各自环形滑动窗口（宽度 ∈ [min_width=5, max_width=12]，(sum, width, start) 字典序，确定性）
      → 置信度 confidence = 完整度(桶内 sample_days/history_days) × 安静度(1 - 窗口均回复/全天均回复)
      → 写 weekday_*/weekend_* 两套独立字段；桶内数据不足 → 该桶不覆盖（保持当前值）
  → _sync_quiet_window(now)：按当前时刻 bucket_for(now) 选桶，置信度 ≥ min_confidence(0.5)
      → 应用该桶学习窗口 {quiet_start, quiet_end}，并把兼容字段 quiet_start/quiet_end/confidence
        同步为当前生效桶快照（门禁经 CooldownState.quiet_window() 读取不变）
      否则该桶回退配置默认 0-8（不影响另一桶）
```

**听歌活跃并入**（v8）：`record_active(now, bucket)` 把睡眠窗口内的播放时刻记入 `active_days`（与 reply_days 同结构、同分桶规则）；`recompute` 对该桶把 reply + active 逐小时计数合并后估计——深夜多次听歌使该时段活跃计数上升，窗口自动偏移。

**迁移语义**（`_migrate_circadian_v8`，加载时执行，幂等）：
- reply_days/active_days 无 bucket 字段的旧条目 → 按日期 `weekday() < 5` 启发式补桶（历史数据无节假日判定，解析失败丢弃）
- 旧单桶窗口继承：仅当 weekday_* 与 weekend_* 全部默认且旧 `confidence > 0` → 继承旧 quiet_start/quiet_end/confidence 到 weekday_*（防止 v8 风格状态里"周末快照"被误继承）

**降级语义**：

- 数据不足（桶内 sample_days < min_sample_days=7 / 无数据 / 非法计数）→ 该桶不覆盖当前值，保持默认 0-8
- 置信度 < 0.5 → 该桶学习窗口不生效，回退配置默认
- 损坏记录（非法日期/缺 hours 键/越界小时/无 bucket 字段）→ 逐条防护不崩溃，非法条目丢弃
- 窗口语义与 cooldown 一致：`quiet_end` 不含 end，`qe < qs` 表示跨午夜
- 热重载：`_maybe_reload_config()` 检测 toml mtime 变化后重新 `_sync_quiet_window()`，避免学习窗口陈旧

### 2.10 接话茬（follow_up，v7）

把"没聊完的话题"变成自然续聊，触发类型第 13 种。

**数据流**：

```
主人回复 + --analysis JSON
  → on_user_message 摄入：analysis.topic（非空）→ pending_topics 追加 {topic, source, created_at, attempted}
  → analysis.topic_resolved=true → resolve_pending_topic()（同话题移除，活跃对话不触发）

evaluate_triggers()
  → prune_pending_topics()：年龄 > follow_up_max_age_hours(48h) 或已 attempted 的话题清理
  → 取年龄 ∈ [min_age_hours=2h, max_age_hours=48h] 的 pending 话题
  → 权重 = follow_up_weight(0.35) × 钟形 exp(-((age - peak=4h)/σ=3h)²)，> follow_up_min_weight(0.03) 才成候选
  → 选中后 mark_pending_topic_attempted()（单次尝试，防重复刷屏）
  → 无 pending 时兜底：近期（48h）用户相关记忆（user_relevant）作接话茬素材（source=memory，不落盘）

_build_context()
  → context.follow_up = {topic, source, age_hours}
  → guidance 追加【接话茬】提示 + instruction 注入素材（聊天式提起，不要汇报腔）
```

**降级语义**：

- 话题过期（>48h）顺带清理，状态不膨胀（pending_topics 上限 20 条，超出丢弃最旧）
- 同话题重复出现 → 视为已接续重新计时
- LanceDB 不可用 → 记忆兜底自动跳过，仅剩 analysis 话题
- 坏时间戳条目（非法 ISO / 非 dict）→ prune 直接丢弃

### 2.11 听歌双向联动（netease，v8）

网易云最近播放记录作为"夜间活跃反证"：睡眠窗口内刚有播放 → 用户醒着，压制 Bayesian sleeping 推断，同时反向校正生物钟窗口。

**数据流**：

```
evaluate(now)
  → _in_quiet_window(now, qs, qe)？否 → 本轮不拉取（白天无意义）
  → _check_play_proof(now)：
      netease_bridge.fetch_recent_play(limit=20, ttl_minutes=15)
        → GET /user/record?type=1&limit=20（近一周播放记录）
        → 缓存 recent_play_cache.json（原子写；TTL 内命中直接复用，负年龄/损坏缓存不命中，失败不缓存）
      → 取播放时刻 ∈ [now - play_proof_window_hours(2h), now] 且落在睡眠窗口内的条目
      → 反证成立 → sleep_window 内播放时刻记入 circadian.record_active(按播放时刻分桶)
      → circadian.recompute() + _sync_quiet_window(now)（反向校正，深夜听歌使窗口偏移）
  → sleeping 置信度压制：effective_conf = raw_conf × sleeping_confidence_factor(0.5)
      evaluate() 睡觉门控与逃生阀 sleeping_guard 均用 effective_conf 比较
      （压到阻塞阈值以下 → 不阻塞发送；仅评估时点生效，不持久化"醒着"状态）
```

**降级语义**（全链路，不阻塞、不告警）：

- API 不可用 / 未登录（无 cookie）→ fetch 返回 None，本轮跳过反证
- 缓存过期 / 损坏 / 未来时间戳（时钟回拨）→ 不命中缓存，重新请求或跳过
- 播放条目 playTime 缺失 / 非 int / 非法 → 逐条过滤，不崩溃
- 睡眠窗口动态变化 → 反证只在评估时点生效，不持久化"醒着"状态（避免状态污染）

### 2.12 音乐话题源（netease，v9）

网易云音乐内容作为话题注入**第 8 源**（破冰素材）：`chiguo_netease.py` 策略层提供，`chiguo_topics.py` 委托接入（`netease_weight=0.12`，来源权重表见 §4.1；策略层可注入可省略——未注入 → 静默跳过不阻塞话题选择）。

**两阶段配额（peek/consume）**：候选生成阶段 `pick()` 调 `peek_music_topic(now, in_class, in_quiet_window)` **只探测不消费配额**（fault 分支内联、配额检查只读；拉取成功仍 `_sync_success` 恢复健康——「拉取成功=API 正常」是事实且不消费配额；两源全失败仍 `refresh_health` 探针）；抽选命中 `netease_music`/`netease_fault` 后才调 `consume_music_topic`/`consume_fault_topic` 确认消费——**配额只在真正发出时消耗**，避免每天 ~6 次 pick 把配额耗尽（未选中不消费）；`music_topic` = peek + consume 包装（返回话题即已消费，供非 TopicPicker 调用方）。

**配额**：
- 音乐话题共享日配额 2（`netease_daily_quota`）：daily（每日推荐）与 recent（播放历史）两源共享、跨天重置；双源全挂 → None 不消费配额
- 故障话题日配额 1（`netease_fault_daily_quota`）：faulty 期间跳过网络直出故障话题，**不受上课/睡眠时段门控**，但发送仍受 daemon 总体发送门控（can_send/日限额）约束

**随机选源**：`netease_source_weights`（默认 [0.5, 0.5]）加权随机选 daily（每日推荐）/recent（最近播放，取 playTime 最大者）；选中源不可用自动换另一源；负权重钳制非负、两权全 0 回退 [0.5, 0.5]。

**fail-closed 守卫**：`_netease_music_topic` 中 schedule_status/quiet_window 异常 → 直接返回 None 不发（防上课/睡眠时误发音乐话题）；`peek_music_topic`/`consume_*` 调用异常 → try/except 兜底不阻塞话题选择。

**健康与降级链**：`netease_health.json` 健康文件（tmp→os.replace 原子写，缺失/损坏/非 dict → 默认重建不崩溃）；`refresh_health(now)` 真实探针（api_alive=False → faulty=unreachable；api_alive 且未登录 → faulty=login_expired；均 OK → 恢复并清 last_failure）；faulty 且未到 `reprobe_minutes`（默认 30）→ 跳过网络直出故障话题；`_sync_success` 拉取成功即恢复。`chiguo_monitor` 只读健康文件展示，**不触发探针**。

**热重载**：`_maybe_reload_config()` 检测到 toml mtime 变化后同步重建 `NeteaseService` 与 TopicPicker（chiguo_daemon.py:121-124，重试/配额参数可能被改）——非法 `retry_count` 会在此路径抛异常（见 §十七 已知局限）。

**素材安全**：fault/daily/recent 话题 data 仅 `{source, reason}` / `{source, name, artist}`，不含 share_url/链接（链接由 OpenClaw 发送层按需拼接）。

---

## 三、决策树

### 3.0 寒暑假检测

```python
on_break:
  ① break_state.json 中 manual_override=true → True  (手动无限期)
  ② 今天在 breaks[] 任一区间内              → True  (日期区间，如寒假)
  ③ datetime.now() > semester_end           → True  (学期自动结束)
  ④ 以上皆否                                  → False
```

- `semester_end` 在 `chiguo_proactive.toml` `[schedule]` 段配置
- 日期区间通过 CLI 管理：`--break add YYYY-MM-DD YYYY-MM-DD [备注]`
- 手动覆盖：`--break on`（无限期）/ `--break off`（清空）
- OpenClaw 检测到"1月12放寒假"→ `--break add 2026-01-12 2026-02-22 寒假`
- OpenClaw 检测到"2月23开学"→ `--break remove 0`

### 3.1 availability 决策

```
availability(now):
  ├─ on_break? ── yes → 0.85（寒暑假，跳过一切）
  ├─ is_holiday? ── yes → 0.85（跳过课表）
  ├─ is_school_day? ── no → 0.85（普通周末）
  └─ yes（含调休）→ schedule_parser.query()
       ├─ in_class → 0.05(heavy) / 0.08(normal) / 0.12(light)
       ├─ remaining=0 → 0.85（课上完了）
       └─ remaining>0 → 0.50~0.70（还有课）
```

### 3.2 节假日判断

数据来源：[国务院办公厅 2026 年节假日安排](https://www.gov.cn/zhengce/content/202511/content_7047090.htm)

```
7 个法定假期:
  元旦 1/1-1/3 | 春节 2/15-2/23 | 清明 4/4-4/6
  劳动 5/1-5/5 | 端午 6/19-6/21 | 中秋 9/25-9/27
  国庆 10/1-10/7

6 个调休上班日:
  1/4, 2/14, 2/28, 5/9, 9/20, 10/10

is_school_day = 非节假日 AND (工作日 OR 调休上班日)
```

更新方式：改 `holiday_parser.py` 内置数据，或放 `holidays.json` 覆盖（ChiguoState 构造时以 `_base_dir` 锚定传入，不依赖 cwd；显式指定路径后不再回退 cwd 的同名文件，无参构造 `HolidayParser()` 保留 cwd 默认行为）。

### 3.3 课表解析

```
data/xskb.xlsx（替换即更新）
  → mtime 检测 → openpyxl 读取
  → 解析格式: "课程名-教师【周数】教室"
  → schedule_cache.json（加速 + 可序列化）
  → query(now) → {in_class, current_course, class_load, ...}
```

xlsx/cache 路径由 ChiguoState 以 `_base_dir` 锚定（cron 工作目录漂移不会静默空课表）。v10.1 起数据文件统一收进 `data/` 子目录：课表源文件 `data/xskb.xlsx`、手动记忆 `data/chiguo_memories.json`、网易云二维码 `data/netease_qr.png`；toml 中的相对路径（如 `xlsx_path = "data/xskb.xlsx"`、`manual_path = "data/chiguo_memories.json"`）与代码默认值均经 `_anchored`（`_base_dir` + 相对路径拼接，绝对路径原样保留）解析为项目根下路径，与 cwd 无关。解析失败（xlsx 损坏等）降级空课表但**不覆盖已有有效缓存**：`_parse()` 返回 bool，仅成功才 `_save_cache()`。

缓存带 `cache_version=2`：旧版本缓存（含合并单元格吞课的脏数据）启动时强制重解析（`_parsed_at=0`）；`_parse_cell` 按 2+ 连续空白拆分课程段，合并课存 `alternates`，`_parse_weeks` 支持后缀单双周。

> 现网缓存已于 2026-07-31 手工迁移 v2（xskb.xlsx 缺失，自动重解析无法自愈）：v1 原文件备份为 `schedule_cache.json.v1.bak`，20 条脏条目（10 个合并 cell × 2 节次）按 `_parse_cell` 逻辑重建为 20 主课 + 32 alternates（52 门课实例），location 全干净。已知遗留（v1 吞掉 `-` 分隔符，无法无猜恢复）：周一 7/8 节 `安全教育(理论)` 周次存为 `1016(双)周`（推断应为 `10-16(双)周`）、周五 3/4 节 `大学英语（二）(理论)` 周次存为 `24,6-7,9-10,12-15,17周`（推断应为 `2-4,6-7,9-10,12-15,17周`），需人工确认后修正（或恢复 xskb.xlsx 后重新解析自愈）。

周数计算：`(now.date() - semester_start).days // 7 + 1`

节次映射：中国大学标准 11 节（08:00-21:25）

### 3.4 记忆系统

```
三层记忆:
  ① JSON 手动（data/chiguo_memories.json，v10.1 起收进 data/，相对路径经 `_anchored` 解析）
     类型: reminder（定时）/ habit（习惯窗口）
     → daemon 直接读取，不用 OpenClaw

  ② LanceDB 只读（OpenClaw 记忆系统）
     路径: ~/.openclaw/memory/lancedb-pro/memories.lance（~ 展开为 $HOME，v10 起多机可移植）
     访问: memory_bridge.py（FTS BM25 关键词搜索 + Ebbinghaus 加权）
     表结构: id, text, vector(1024d), category, scope, importance, timestamp, metadata
     降级: LanceDB 不可用 → available=False → 自动跳过，JSON 兜底
     - lancedb 在 `_ensure_table()` 内**惰性导入**：未安装时 daemon 照常启动（import 失败也被 available=False 捕获）
     - 探测失败后按 60s 节流重试：`--loop` 长驻时 LanceDB 故障恢复可自愈，不永久禁用
     - 结果行防御：importance 的 None/NaN 统一清洗为 0.0，行级异常整体降级为空列表

  ③ Ebbinghaus 遗忘曲线（v4）
     R = e^(-t / (S × importance))
     新/重要记忆权重高，旧/不重要记忆权重低
     S=168h（7天），min_weight=0.1（不彻底遗忘）
     memory_bridge 导出 ebbinghaus_weight(), search_with_forgetting(), random_memory_with_forgetting()
```

---

## 四、话题注入系统

lonely_low/mid 触发时，从 8 个来源加权随机选话题，让消息成为自然关心而非纯情绪宣泄。

### 4.1 话题来源

| 来源 | 权重 | 数据源 | 说明 |
|------|:--:|------|------|
| schedule | 0.30 | schedule_parser + holiday_parser | 课表/假期/周末/调休 |
| memory | 0.25 | memory_bridge (LanceDB + Ebbinghaus) | 随机高重要性记忆 |
| general | 0.25 | 当前小时数 | 按时段通用关心 |
| weather_season | 0.20 | 当前月份 | 季节感知 |
| anniversary | 0.15 | anniversary_manager | 纪念日/倒计时 |
| solar_terms | 0.10 | solar_terms | 24节气 |
| preference_followup | 0.10 | memory_bridge (LanceDB) | 偏好追问 |
| netease | 0.12 | NeteaseService (chiguo_netease) | v9: 网易云音乐话题（策略层委托） |

- `chiguo_topics.py`: TopicPicker 类，`pick(now)` → weighted_trigger_choice
- v9: `TopicPicker.__init__(state, config, netease_service=None)` — 策略层可注入可省略（None → 静默跳过，向后兼容）；daemon 构造（chiguo_daemon.py:73-75）与热重载分支（:121-124）均已注入 `NeteaseService`（v9 已接线）；`_netease_music_topic(now)` 计算上课/睡眠门控（schedule_status + cooldown.quiet_window + `_in_quiet_window` 跨午夜语义；门控信息异常 → fail-closed 不发）后委托 `peek_music_topic`（不消费配额），抽选命中 netease_music/netease_fault 后才 consume——配额只在真正发出时消耗；未注入/异常 → 返回 None 不阻塞话题选择；委托细节见 §2.12
- 连续 3 次孤独触发 → 强制注入话题
- `topic_probability=0.70` 控制注入概率
- v4: 人格调制话题多样性。高开放性（openness）→ 更多 memory/anniversary 话题；低开放性 → 更多 schedule/general 话题

### 4.2 节气

24 节气近似日期硬编码在 `solar_terms.py`。±1 天窗口命中。零依赖。

### 4.3 纪念日/倒计时

`anniversary_manager.py` 管理 `anniversaries.json`。两种类型：
- **anniversary**: 每年重复，存 "MM-DD"
- **countdown**: 一次性，存 "YYYY-MM-DD"

**路径锚定（2026-07-31）**：无参构造时，若 cwd 已存在同名 `anniversaries.json` 则沿用（兼容旧版/隔离目录），否则锚定模块目录（项目根），防止从其他 cwd（如 /tmp）运行把数据写散；显式传绝对路径仍原样生效。

CLI CRUD：`--anniversary "add anniversary 11-03 主人生日"` 等。

OpenClaw skill 检测主人提到日期 → 自动调用 CLI 记录。详见 OPENCLAW_INTEGRATION.md §五（特殊命令）。

---

## 五、LLM 内容分析

主人回复时，OpenClaw 可调用 LLM 分析消息内容，传入 `--analysis` 参数实现差异化情绪变化。

### 5.1 分析维度

| 维度 | 范围 | 含义 |
|------|------|------|
| `warmth` | -1.0~1.0 | 情感温度。负=冷淡，正=温暖 |
| `effort` | 0.0~1.0 | 用心程度 |
| `attention` | 0.0~1.0 | 对迟菓的关注度 |

### 5.2 情绪映射

```
warmth → affection += warmth × 1.5, energy += warmth × 4.0
warmth < 0 → anxiety += |warmth| × 3.0（冷淡→不安回升）
effort  → affection += effort × 1.0, tsundere -= effort × 2.0
attention → energy += attention × 4.0
attention < 0.3 → anxiety += (0.3 - attention) × 2.0
```

所有系数在 `chiguo_proactive.toml` `[emotion]` 段可调。

### 5.3 CLI

```bash
# 热情回复
python3 chiguo_daemon.py --user-msg "我在呢菓菓" \
  --analysis '{"warmth":0.7,"effort":0.8,"attention":0.9}'

# 敷衍回复
python3 chiguo_daemon.py --user-msg "嗯" \
  --analysis '{"warmth":-0.3,"effort":0.1,"attention":0.2}'

# 不带 --analysis → 降级为纯长度模式
python3 chiguo_daemon.py --user-msg "任意消息"
```

### 5.4 回复速度差异化

用户秒回 vs 几小时后回复 → 情感变化量不同。秒回更开心，很久才回效果打折。

| 回复速度 | 延迟 | 好感倍率 | 其他效果 |
|------|:--:|:--:|------|
| 秒回 | ≤5min | ×1.5 | 元气+5, 傲娇额外-2 |
| 较快回 | 5min-1h | ×1.0 | 正常值 |
| 隔了一段时间 | 1-6h | ×0.7 | — |
| 很久才回 | >6h | ×0.4 | 不安回升+3 |

阈值在 `chiguo_proactive.toml` `[emotion]` 段可调（`reply_fast_threshold` 等）。

### 5.5 忙碌抑制

用户表达忙碌/结束对话时，OpenClaw 通过 `--analysis` 回传 `suppress_hours` 字段，daemon 在抑制期内 `can_send()` 返回 False。

**原理**：daemon 不做语义理解（保持零 LLM 数学引擎的纯净性）。忙碌检测完全交给 OpenClaw LLM。

```bash
# LLM 分析消息 → 设置 suppress_hours
python3 chiguo_daemon.py --user-msg "开会去了回头聊" \
  --analysis '{"warmth":0.2,"effort":0.1,"attention":0.1,"suppress_hours":4}'
```

抑制逻辑（`_apply_emotion_impact()`）：
- `suppress_hours > 0` → 设置 `cooldown.busy_suppress_until` = now + suppress_hours
- `can_send()` 检查 `is_busy_suppressed()` → True 时禁止触发
- 若已有抑制期 → 取两者中较晚的截止时间

OpenClaw SKILL.md 建议判断标准：
- 表达忙碌/有事（"在忙""开会""有事""上课"）→ `suppress_hours: 2-4`
- 表达结束对话（"晚安""睡了""bye""先这样"）→ `suppress_hours: 8`
- 表达暂时离开（"回头聊""等一下""等会"）→ `suppress_hours: 1-2`
- 其他情况 → 不传或 0

### 5.6 人格自适应（v4）

每次互动微调人格（变化 < 0.2 每步，经数周/月才显著变化）：
- 正面互动（收到回复、温暖回复）→ 外向性/宜人性微增，神经质微降
- 负面互动（沉默期、冷淡回复）→ 外向性微降，神经质微增
- `PersonalityDelta` 计算变化量，记录在 `personality_history` 供回溯

### 5.7 消息组合系统（v4）

参考 Sebastian 的 combo 系统。替代旧的单一 situation 描述。

**Intent (A)** × **Cue (B)** × **Vibe (C)** 三层组合：

- **Intent**: 对话意图——"为什么发这条消息"。按触发类型分组（lonely_low 有 5 种意图，lonely_mid 有 5 种，lonely_high 有 4 种等）
- **Cue**: 人格面具——"用什么风格发"。8 种 cue（tsundere, tsundere_soft, tsundere_cool, dere, playful, anxious, caring, cool），按 personality + trigger_type 调制权重
- **Vibe**: 时间/情境氛围——"在什么环境下发"。按时段（清晨/上午/午休/下午/傍晚/深夜）、周末、考试周、假日选择

Combo 尺寸概率：1 层（仅 Intent）20%、2 层（Intent × Cue）50%、3 层（Intent × Cue × Vibe）30%。

---

## 六、文件清单

| 文件 | 职责 | 依赖 |
|------|------|------|
| `chiguo_daemon.py` | **主入口**。决策引擎，输出 JSON | state, trigger, topics, composer, eventbus |
| `chiguo_state.py` | 情绪引擎 + 多维人格 + Bayesian + 课表 + 节假日 + 记忆 + circadian/pending_topics + 双作息迁移 (v8) | math, personality, bayesian, schedule_parser, holiday_parser, memory_bridge, chiguo_circadian |
| `chiguo_circadian.py` | 生物钟学习：双作息双桶分桶（weekday_*/weekend_* 独立估计 + 听歌活跃合并计数）（v7 新增，v8 双桶，纯函数） | 无 |
| `netease_bridge.py` | 网易云 API 桥接：`fetch_recent_play` 最近播放记录（睡眠窗口内夜间活跃反证 + recent_play_cache.json 缓存）（v8）；`fetch_daily_songs` 每日推荐 + `_api_get` 有限重试（瞬时/5xx 重试 retry_count 次 + 退避）与每日推荐 schema 过滤（v9） | 无（requests） |
| `chiguo_netease.py` | 网易云策略层（v9）：`NeteaseService` 健康探针/登录失效检测/故障降级链（netease_fault 话题）/音乐+故障双日配额（netease_health.json 原子写）/加权随机选源+换源兜底/peek-consume 两阶段（未选中不消费配额）/话题素材组装（不含链接，零 LLM）；测试 `test_netease_service.py` | netease_bridge |
| `chiguo_trigger.py` | 触发评估（13 种，含 v7 follow_up 接话茬）+ 加权随机选择 | state, math |
| `chiguo_topics.py` | 话题选择器（8 来源 + 人格调制 + v9 netease 委托） | math, solar_terms, anniversary_manager, chiguo_netease |
| `chiguo_composer.py` | Intent × Cue × Vibe 三层消息组合（v4） | 无 |
| `chiguo_math.py` | 纯数学库：sigmoid/decay/recover/Hawkes/longing/Ebbinghaus | 无 |
| `chiguo_personality.py` | Big Five + 角色特质（8 维人格）（v4） | 无 |
| `chiguo_bayesian.py` | Bayesian 用户状态推断（6 状态，在线学习）（v4） | 无 |
| `chiguo_eventbus.py` | 轻量发布/订阅事件总线（v4） | 无 |
| `schedule_parser.py` | 课表解析（xlsx → JSON cache → query） | openpyxl |
| `holiday_parser.py` | 节假日判断（2026 国务院安排 + 调休） | 无 |
| `solar_terms.py` | 24 节气日期查询（零依赖） | 无 |
| `memory_bridge.py` | LanceDB 只读桥接 + Ebbinghaus 遗忘 | lancedb（惰性导入）, pandas |
| `anniversary_manager.py` | 纪念日/倒计时 CRUD | 无 |
| `chiguo_monitor.py` | 流式 JSONL 分析（统计/告警/健康） | 无 |
| `chiguo_rotation.py` | 日志轮转 + 告警持久化 + 索引查询（v5） | 无 |
| `chiguo_watchdog.py` | 零依赖独立看门狗（cron 集成）（v4） | 无 |
| `chiguo_envcheck.py` | 环境就绪检查（v10.1）：5 组只读检查（Python/uv、OpenClaw skill、LanceDB、网易云、数据文件），网易云检查仅 HTTP 200 计 API 可达（不可达 → warn，含 cookie 存在时），JSON → stdout，退出码 0=就绪/1=警告/2=严重（与 watchdog 一致），路径单一事实来源为 `chiguo_proactive.toml`；测试 `test_envcheck.py` | 无 |
| `chiguo_version.py` | 项目版本号单一来源（`VERSION="1"`，每轮修改 +0.1；daemon/envcheck/monitor import 引用） | 无 |
| `chiguo_proactive.toml` | **配置文件**（所有参数） | 无 |
| `data/chiguo_memories.json` | 手动记忆（习惯/提醒） | 无 |
| `chiguo_state.json` | 运行时状态（STATE_VERSION=8，首次运行后生成） | 无 |
| `chiguo_decisions.jsonl` | 决策日志（首次运行后生成） | 无 |
| `recent_play_cache.json` | 最近播放记录缓存（v8，netease fetch_recent_play 原子写，TTL 15 分钟） | 无 |
| `netease_health.json` | 网易云健康状态文件（v9，chiguo_netease 原子写：api 存活/登录态/故障原因/音乐+故障双日配额） | 无 |
| `schedule_cache.json` | 课表缓存（首次运行后生成） | 无 |
| `anniversaries.json` | 纪念日数据（首次运行后生成） | 无 |
| `break_state.json` | 假期覆盖状态（首次 --break 后生成） | 无 |
| `.gitignore` | 忽略运行时备份/临时/锁/token（分析数据跟踪入库供本地分析，见 doc/README.md §运行时数据回流） | 无 |
| `data/xskb.xlsx` | 课表源文件（替换即更新） | 无 |
| `data/netease_qr.png` | 网易云登录二维码（--login 生成） | 无 |
| `test_chiguo_math.py` | 数学库单元测试（26 用例，含 sigmoid/负权重/负半衰期边界） | chiguo_math |
| `test_holiday_parser.py` | 节假日单元测试（7 用例） | holiday_parser |
| `test_integration.py` | 集成测试（17 用例，test_1/test_8 已从纯 print 强化为真断言） | chiguo_daemon |
| `test_monitor.py` | 监控测试 + fuzz 测试（42 用例） | chiguo_monitor |
| `test_eventbus.py` | EventBus 单元测试（10 用例） | chiguo_eventbus |
| `test_personality.py` | 人格系统单元测试（19 用例） | chiguo_personality |
| `test_bayesian.py` | Bayesian 推断测试（18 用例） | chiguo_bayesian |
| `test_composer.py` | 消息组合测试（10 用例） | chiguo_composer |
| `test_ebbinghaus.py` | Ebbinghaus 遗忘测试（8 用例） | memory_bridge |
| `test_longing.py` | 概率累积测试（8 用例） | chiguo_math |
| `test_escape_valve.py` | 逃生阀单元测试（15 用例，含 v7 sleeping_guard 降级 + 0.85 对照） | chiguo_state, chiguo_trigger |
| `test_feedback.py` | 反馈闭环测试（10 用例） | chiguo_state, chiguo_monitor |
| `test_trigger.py` | 触发器引擎单元测试（16 用例：softmax 竞争/anxiety 归一化/时间窗口/memory tz 防护） | chiguo_trigger |
| `test_topics.py` | 话题选择器单元测试（23 用例：8 源权重/人格调制/Ebbinghaus 路径/v9 netease 注入、门控参数、选中才消费、fail-closed） | chiguo_topics, memory_bridge, chiguo_netease |
| `test_circadian.py` | 生物钟学习单元测试（34 用例：环形滑动窗口/置信度/损坏数据防护/学习窗口作用于门禁/双桶分桶/迁移/按桶选窗/record_active 合并） | chiguo_circadian |
| `test_followup.py` | 接话茬单元测试（14 用例：pending 管理/钟形权重/多话题/记忆兜底/FakeBridge） | chiguo_state, chiguo_trigger, memory_bridge |
| `test_netease_proof.py` | 听歌反证单元测试（31 用例：fetch_recent_play 解析/缓存/降级 + `_api_get` 重试策略与每日推荐 schema 过滤 + 非 dict 响应降级 + 窗口内反证 sleeping 压制/按播放时刻分桶/逃生阀放行 + netease 跨触发注入规则） | netease_bridge, chiguo_daemon |
| `test_netease_service.py` | 网易云策略层单元测试（30 用例：健康文件缺失/损坏重建/原子写/脏值类型回退/非法配置回退/check_health 非 dict 降级、音乐+故障双配额与跨天重置、随机选源比例分布（seed 固定 2000 次抽样 0.5±0.08）/换源兜底/双源全挂探针判定不消费、时段门控、故障话题绕过门控+配额、登录失效检测、重探间隔、恢复、抓取失败置故障下一轮产故障话题、素材无链接、最新播放、naive tz 补齐、源权重配置与负权重钳制、两阶段 peek 不消费/consume 确认/music_topic=peek+consume） | chiguo_netease, netease_bridge |
| `test_envcheck.py` | 环境检查单元测试（10 用例：env 版本/uv、openclaw 目录缺失 critical/skill 缺 warn/正常、lancedb 缺失 warn、netease API 不可达/无 cookie warn、data 缺失 warn/正常、退出码 0/1/2 映射、run_checks 全场景不崩） | chiguo_envcheck |
| `doc/` | 文档目录 | 无 |

共计 **348** 个测试用例（19 个测试文件）。

> 已修复：`holidays.json` 已重新生成为 2026 国务院官方数据（`update_holidays.py`，`_generated_for=2026`），
> `test_holiday_parser.py` 7/7 用例通过。

---

## 七、CLI 参考

### chiguo_daemon.py

```bash
# 单次决策（输出 JSON 到 stdout）
python3 chiguo_daemon.py

# 版本号（chiguo_version.py: 规则 每轮修改 +0.1）
python3 chiguo_daemon.py --version

# 紧凑模式（idle 输出最小单行 JSON {"action":"idle","version":...,"time":...}）
python3 chiguo_daemon.py --compact

# 显示状态
python3 chiguo_daemon.py --status

# 记录主人消息
python3 chiguo_daemon.py --user-msg "主人发的消息原文"

# 持续运行（调试用，每 N 秒评估一次；最小间隔 60 秒，低于 60 自动按 60 处理并 stderr 提示）
python3 chiguo_daemon.py --loop 120

# 寒暑假模式
python3 chiguo_daemon.py --break add 2026-01-12 2026-02-22 寒假   # 添加日期区间
python3 chiguo_daemon.py --break remove 0    # 按序号删除
python3 chiguo_daemon.py --break list        # 列出所有区间
python3 chiguo_daemon.py --break clear       # 清空
python3 chiguo_daemon.py --break on          # 手动无限期
python3 chiguo_daemon.py --break off         # 关闭
python3 chiguo_daemon.py --break status      # 完整状态

# 健康检查
python3 chiguo_daemon.py --health            # 检测 daemon 最近是否正常运行

# 回传发送结果（反馈闭环）
python3 chiguo_daemon.py --send-result msg_xxx --send-status success
python3 chiguo_daemon.py --send-result msg_xxx --send-status failed --error "WeChat API timeout"

# 文件传参（避免 shell 转义问题）
python3 chiguo_daemon.py --user-msg-file /tmp/user_msg.txt
python3 chiguo_daemon.py --analysis-file /tmp/analysis.json

> **注意**：`--send-result` 是幂等的——重复报告同一条消息不会重复退款。

# 监控（委托给 chiguo_monitor.py）
python3 chiguo_daemon.py --stats             # 最近7天统计
python3 chiguo_daemon.py --stats 30          # 最近30天统计
python3 chiguo_daemon.py --alerts            # 异常检测
python3 chiguo_daemon.py --monitor           # 完整报告（stats + alerts + health）
```

### chiguo_demo.py

```bash
# 启动交互式 Demo
python3 chiguo_demo.py

# 交互命令：
#   回车    推进 30 分钟
#   t N     推进 N 分钟
#   h N     推进 N 小时
#   d N     推进 N 天
#   m 文本  模拟主人发消息
#   s       刷新状态显示
#   r       重置状态
#   q       退出
```

### schedule_parser.py

```bash
# 查询当前课表状态
python3 schedule_parser.py

# 导出完整解析结果
python3 schedule_parser.py --dump
```

### holiday_parser.py

```bash
# 查询今天
python3 holiday_parser.py

# 查询指定日期
python3 holiday_parser.py 2026-10-01
```

### memory_bridge.py

```bash
# 统计
python3 memory_bridge.py --stats

# FTS 搜索
python3 memory_bridge.py --search "菓菓"

# 随机记忆
python3 memory_bridge.py --random

# 最近记忆
python3 memory_bridge.py --recent 168
```

特性：
- lancedb 惰性导入：未安装时 `available=False` 优雅降级，daemon 不受阻塞
- `available=False` 后按 60s 节流重试，`--loop` 长驻下故障恢复可自愈
- importance 的 None/NaN 统一清洗为 0.0（结果循环有行级异常兜底）

### chiguo_monitor.py

```bash
# 人类可读摘要（默认7天）
python3 chiguo_monitor.py
python3 chiguo_monitor.py --summary --days 30

# 结构化统计（JSON）
python3 chiguo_monitor.py --json
python3 chiguo_daemon.py --stats        # 委托别名

# 异常告警（JSON）
python3 chiguo_monitor.py --alerts

# 增强版健康检查
python3 chiguo_monitor.py --health

# 完整报告（stats + alerts + health）
python3 chiguo_monitor.py --report
```

### chiguo_watchdog.py

```bash
python3 chiguo_watchdog.py              # 完整检查，输出 JSON
python3 chiguo_watchdog.py --quiet      # 仅异常时输出（退出码驱动）
python3 chiguo_watchdog.py --notify     # 异常时 stderr 输出告警摘要
```

退出码：0=正常, 1=警告(warn), 2=严重(critical)。适合 cron：

```
# crontab: 每30分钟检查一次
*/30 * * * * cd <仓库根目录> && .venv/bin/python chiguo_watchdog.py --notify 2>&1 | logger -t chiguo_watchdog
```

---

## 八、JSON 输出格式

### action=send

```json
{
  "action": "send",
  "version": "1",
  "trigger": "lonely_mid",
  "intensity": "medium",
  "context": {
    "character": "迟菓",
    "personality_source": "~/.openclaw/workspace/skills/chiguo/SUN2.md",
    "situation": "主人已经12小时没发消息了。菓菓开始焦虑不安。用嘴硬的方式联系……",
    "schedule_hint": "主人正在上工程测量实训（到14:45）。不要在上课时发消息。",
    "layer": "middle",
    "layer_guidance": "嘴硬心软，表面强硬（「谁要你管」「不·需·要」），但话里有话，试探性联系。",
    "emotion": {
      "loneliness": 62,
      "affection": 55,
      "anxiety": 48,
      "energy": 72,
      "tsundere_index": 68
    },
    "silent_hours": 12.3,
    "hawkes_intensity": 0.0312,
    "trigger_type": "lonely_mid",
    "intensity": "medium",
    "composer_intent": "嘴硬关心——用攻击性语言包装的关心",
    "composer_cue": "tsundere",
    "composer_vibe": "afternoon_silent",
    "instruction": "请以迟菓（SUN2.md 设定）的身份，用上述语气发一条微信消息给主人。1-3句话。自然。"
  },
  "state": { ... },
  "bayesian": {
    "most_likely": "browsing",
    "confidence": 0.62,
    "utility": 0.55
  }
}
```

### action=idle

```json
{
  "action": "idle",
  "version": "1",
  "reason": "quiet_hours",
  "next_evaluation_at": "2026-06-25 08:00:00",
  "state": {
    "emotion": { ... },
    "dominant_layer": "shell",
    "cooldown": { ... },
    "time": "2026-06-22 23:15"
  },
  "bayesian": {
    "most_likely": "sleeping",
    "confidence": 0.82,
    "utility": 0.0
  }
}
```

idle 输出中新增 `next_evaluation_at` 字段，预测下次可触发的最早时间。`--user-msg` 在紧凑模式（--compact）下也始终包含此字段，供 OpenClaw 调度下次心跳评估。

idle reason 枚举：
- `quiet_hours` — 00:00-08:00 静默时段/睡眠窗口（22:00-00:00 不再静默，可发消息）
- `daily_limit` — 超过每日上限
- `low_energy` — 元气不足
- `min_interval` — 距上次发送不足 30 分钟
- `no_trigger` — 没有触发条件满足
- `user_sleeping` — Bayesian 推断用户正在睡觉
- `user_busy` — Bayesian 推断用户正在忙
- `busy_suppressed` — 用户显式 busy_suppress 抑制期（优先于 Bayesian 判断，不累积 longing）
- `sleeping_guard` — 逃生阀豁免睡觉门控时 Bayesian 睡觉置信度 ≥ `escape_valve_sleep_block`（默认 0.9），降级为不发送

---

## 九、配置参考（chiguo_proactive.toml）

```toml
[openclaw]
wechat_channel = "openclaw-weixin"      # v12 起仅作元数据（实际发送走 wechat-bridge /send，见 doc/OPENCLAW_INTEGRATION.md）
wechat_recipient = "..."                 # 接收者 ID
personality_source = "~/.openclaw/workspace/skills/chiguo"  # SUN2.md 目录（~ 展开为 $HOME）

[character]
name = "迟菓"
age = 16
identity = "住在VPS里的外卖少女，哥哥的傲娇助手"

[memory]
lancedb_path = "~/.openclaw/memory/lancedb-pro"  # LanceDB 路径（~ 展开为 $HOME）
lancedb_table = "memories"              # 表名
manual_path = "data/chiguo_memories.json"  # 手动记忆文件
ebbinghaus_strength = 168               # 记忆强度 S（小时），168h=7天（v4）
ebbinghaus_min_weight = 0.1             # 最低权重，不彻底遗忘（v4）

[emotion]
# 初始值
loneliness = 15.0
affection = 55.0
anxiety = 40.0
energy = 85.0

# 自然半衰期（小时）
loneliness_gain_half_life = 40.0
anxiety_gain_half_life = 30.0
affection_loss_half_life = 500.0
energy_regen_half_life = 8.0

# 事件半衰期（小时）
loneliness_decay_on_reply = 0.35       # 收到回复：21分钟减半
anxiety_decay_on_reply = 0.5           # 收到回复：30分钟减半
loneliness_decay_on_send = 2.0         # 自己发：2小时减半
anxiety_gain_on_send = 2.0             # 自己发：不安小幅上升
energy_cost_per_message = 20.0         # 发一条消耗元气
energy_bonus_on_reply = 10.0           # 收到回复元气奖励

# 变化率对 λ 的影响系数
lambda_lo_rate_factor = 0.4     # 孤独变化率放大因子
lambda_anx_rate_factor = 0.3    # 不安变化率放大因子

# 好感变化
affection_gain_per_interaction = 0.8

# LLM 分析微调系数
affection_warmth_factor = 1.5
energy_warmth_factor = 4.0
anxiety_warmth_recovery = 3.0
affection_effort_factor = 1.0
tsundere_effort_factor = 2.0
energy_attention_factor = 4.0
anxiety_ignore_factor = 2.0

# 回复速度分级（小时）
reply_fast_threshold = 0.08           # ≤5分钟 = 秒回
reply_slow_threshold = 1.0
reply_very_slow_threshold = 6.0
reply_fast_affection_mult = 1.5
reply_fast_energy_extra = 5.0
reply_fast_tsundere_extra = 2.0
reply_slow_affection_mult = 0.7
reply_very_slow_affection_mult = 0.4
reply_very_slow_anxiety_rebound = 3.0

# 能量覆写（孤独变化率暴涨时允许低元气发送）
rate_energy_override = true            # 是否允许覆写
rate_energy_threshold = 5.0            # 孤独变化率阈值（Δ/h > 5 触发覆写）
rate_energy_min = 5                    # 覆写时最低允许元气值

# 紧迫通知阈值
urgency_rate_threshold = 3.0           # 孤独变化率 > 3 → 添加紧迫注解
urgency_anx_threshold = 2.0            # 不安变化率 > 2 → 添加紧迫注解

[sigmoid]
# 触发概率 sigmoid 参数
loneliness_low_k = 0.20
loneliness_low_mid = 38
loneliness_mid_k = 0.18
loneliness_mid_mid = 55
loneliness_high_k = 0.15
loneliness_high_mid = 78
anxiety_k = 0.12
anxiety_mid = 58

[trigger]
# v7: anxiety 触发候选归一化（与孤独三级同款 softmax"不触发基线"模式）
# w = raw / (raw + anxiety_baseline)，归一化权重 > anxiety_min_weight 才成为候选。
# 默认态 anxiety=40 → w≈0.171 < 0.3 → 不再确定性触发 anxiety 刷满日限额。
anxiety_baseline = 0.5
anxiety_min_weight = 0.3

# v7: 接话茬(follow_up)参数
follow_up_weight = 0.35         # 基础权重(乘年龄钟形调制)
follow_up_min_age_hours = 2.0   # 话题最早可接续年龄(小时)
follow_up_max_age_hours = 48.0  # 超过此年龄过期清理(小时)
follow_up_peak_hours = 4.0      # 钟形权重峰值年龄(小时)
follow_up_sigma_hours = 3.0     # 钟形宽度(小时)
follow_up_min_weight = 0.03     # 低于此权重不成为候选

[poisson]
base_lambda = 0.25                     # 基础事件率（μ 的一部分）
lambda_loneliness_mid = 50
lambda_loneliness_k = 0.08
lambda_anxiety_mid = 45
lambda_anxiety_k = 0.06

[hawkes]
enabled = true
alpha = 0.3                            # 自激系数
beta = 0.5                             # 事件影响衰减率
window_hours = 24                      # 事件窗口（小时）

[topic_picker]
# 话题选择器权重
schedule_weight = 0.30
memory_weight = 0.25
weather_season_weight = 0.20
general_weight = 0.25
solar_terms_weight = 0.10
anniversary_weight = 0.15
preference_followup_weight = 0.10
netease_weight = 0.12                   # v9: 音乐话题源权重
netease_daily_quota = 2                 # v9: 音乐话题日配额（每日推荐+播放历史共享）
netease_source_weights = [0.5, 0.5]     # v9: 每日推荐 vs 播放历史 随机选源权重
netease_fault_daily_quota = 1           # v9: 故障提及日配额

topic_probability = 0.70               # 孤独触发时注入话题的概率
force_topic_threshold = 3              # 连续 N 次孤独触发 → 强制注入
trigger_history_max = 6                # 触发历史记录最大长度

[schedule]
quiet_start = 0
quiet_end = 8
morning_start = 8
morning_end = 10
night_start = 20
night_end = 21
special_dates = ["05-11", "11-03"]     # 菓菓生日, 主人生日
xlsx_path = "data/xskb.xlsx"              # 课表文件
semester_start = "2026-02-23"          # 学期起始日
semester_end = "2026-07-04"            # 学期结束日，之后自动视为假期
exam_weeks = []                        # 考试周日期范围，如 ["2026-06-22,2026-07-03"]

[circadian]
# v7/v8: 生物钟学习 — 从主人回复时间学习睡眠时段,动态调整静默窗口
# v8: 双作息双桶（weekday/weekend 两套窗口独立估计与应用），以下参数两桶共用
history_days = 14        # 回复记录滚动窗口(天)
min_sample_days = 7      # 最少有数据天数才计算学习窗口（每桶各自判断）
min_confidence = 0.5     # 学习置信度低于此值 → 回退配置默认窗口(0-8)
min_width = 5            # 学习窗口最小宽度(小时)
max_width = 12           # 学习窗口最大宽度(小时)

[netease]
# v8: 听歌状态双向联动（夜间活跃反证）——睡眠窗口内最近有播放 → 用户醒着
play_cache_ttl_minutes = 15     # 播放记录缓存 TTL（分钟）
play_proof_window_hours = 2.0   # 播放证据时间窗（距评估时点的小时数）
sleeping_confidence_factor = 0.5  # sleeping 置信度压制系数（有播放证据时 effective = raw × 此值）
# v9: 策略层（chiguo_netease）
retry_count = 1                # 瞬时失败重试次数
retry_backoff_seconds = 2.0    # 重试退避（秒）
reprobe_minutes = 30           # 登录失效后重探间隔（分钟）

[cooldown]
max_daily_active = 4                   # 主人活跃时日上限
max_daily_silent = 2                   # 主人沉默时日上限
min_interval_minutes = 30              # 最小发送间隔
no_reply_lambda_decay = 0.7            # 无回复 λ 衰减因子
# v4: 概率累积参数
longing_growth_factor = 0.08           # 每次 held λ 增长量
anxiety_block_threshold = 70.0         # 焦虑大于此 → 不累积
max_lambda_multiplier = 5.0            # λ 最大倍数
longing_decay_factor = 0.5             # 用户回复后 λ 回退系数
# v6: 溢出逃生阀
longing_break_enabled = true             # 是否启用逃生阀（焦虑阻塞时强制 longing 发送）
longing_break_min_silence_hours = 72     # 逃生阀激活所需的最小墙钟沉默时长
longing_break_cooldown_days = 3          # 逃生阀冷却期（天内最多触发一次）
ritual_weight_scale = 1.0                # 仪式触发权重缩放（特殊日期/早安/晚安/用餐/记忆等固定事件权重的乘数，调低可减少仪式触发对情绪触发的压制）

[personality]
# v4: 多维人格初始值（0-100 量表）
openness = 55.0                        # 开放性
conscientiousness = 65.0               # 尽责性
extraversion = 45.0                    # 外向性
agreeableness = 70.0                   # 宜人性
neuroticism = 60.0                     # 神经质
tsundere_intensity = 70.0              # 傲娇强度
playfulness = 55.0                     # 贪玩程度
attachment_style = 60.0                # 依恋风格（高=焦虑型，低=回避型）

[bayesian]
# v4: Bayesian 用户状态推断参数
learning_rate = 0.05                   # 在线学习率（越小越保守）
min_confidence_for_block = 0.5         # 置信度高于此才阻塞发送
utility_threshold = 0.4                # 加权效用高于此推荐发送
escape_valve_sleep_block = 0.9         # v7: 逃生阀豁免睡觉门控时，睡觉置信度 ≥ 此值仍降级 sleeping_guard

[composer]
# v4: 消息组合系统参数
size_1_weight = 0.20                   # 仅 Intent 概率
size_2_weight = 0.50                   # Intent × Cue 概率
size_3_weight = 0.30                   # Intent × Cue × Vibe 概率
# Cue 基础权重（被 personality 调制）
cue_tsundere_weight = 0.30
cue_tsundere_soft_weight = 0.25
cue_tsundere_cool_weight = 0.10
cue_dere_weight = 0.10
cue_playful_weight = 0.20
cue_anxious_weight = 0.15
cue_caring_weight = 0.25
cue_cool_weight = 0.05

[safety]
# v4: 安全阀
enabled = true
crash_cooldown_hours = 24              # lonely_high 触发后冷却时间
crash_window_hours = 48                # 统计窗口
crash_max_in_window = 2                # 窗口内 ≥2 次崩溃 → 强制温和模式

[monitor]
disk_warn_mb = 500                     # 磁盘剩余小于此 → warn
disk_critical_mb = 100                 # 磁盘剩余小于此 → critical
memory_warn_mb = 500                   # 进程 RSS 大于此 → warn
memory_critical_mb = 1000              # 进程 RSS 大于此 → critical

[memories]
path = "data/chiguo_memories.json"        # 手动记忆文件
```

---

## 十、结构化监控

`chiguo_monitor.py` 提供零依赖的结构化监控。流式解析 `chiguo_decisions.jsonl`，一次遍历完成所有聚合。

### 10.1 CLI

```bash
# 人类可读摘要（默认7天）
python3 chiguo_monitor.py
python3 chiguo_monitor.py --summary --days 30

# 结构化统计（JSON）
python3 chiguo_monitor.py --json
python3 chiguo_daemon.py --stats        # 最近7天
python3 chiguo_daemon.py --stats 30     # 最近30天

# 异常告警
python3 chiguo_monitor.py --alerts
python3 chiguo_daemon.py --alerts

# 完整报告（stats + alerts + health）
python3 chiguo_monitor.py --report
python3 chiguo_daemon.py --monitor

# 增强版健康检查
python3 chiguo_monitor.py --health
```

### 10.2 统计指标

| 分类 | 指标 | 说明 |
|------|------|------|
| activity | total_sends / total_idles | 发送/空闲计数 |
| activity | sends_per_day | 日均发送量 |
| activity | by_trigger | 按触发类型分布 |
| activity | by_intensity | 按强度分布 (soft/medium/intense) |
| activity | by_hour / by_weekday | 时段/星期分布 |
| activity | by_layer | 人格层分布 (shell/middle/kernel) |
| activity | daily_counts | 每日发送量序列 |
| replies | reply_rate | 主人回复率（基于 mwr 变化推断）|
| replies | max_unreplied_streak | 最长连续无回复 |
| emotions | current | 当前5维情绪值 |
| emotions | trends | rising/stable/falling 趋势 |
| emotions | stats | min/max/avg 统计 |
| health | healthy | daemon 是否正常运行 |
| health | last_tick_age | 距上次 tick 小时数 |
| health | log_recent | 日志最近 12h 有写入 |
| health | config_ok | 配置文件存在 |
| health | disk | 磁盘剩余/总量/已用 (MB) |
| health | memory | 进程 RSS 内存 (MB) |
| health | lancedb_direct | LanceDB 直连状态 (True/False/None) |
| lancedb | likely_available | LanceDB 是否可能可用 |

### 10.3 异常检测

| 告警类型 | 条件 | 严重度 |
|---------|------|:--:|
| `crash_gap` | 无 tick > 6h | critical |
| `no_state` | 状态文件缺失或无 last_tick | critical |
| `consecutive_no_reply` | 主人连续 ≥5 条未回复 | warn |
| `emotion_stuck_high` | 孤独/不安 >90 持续 24h | warn |
| `frequent_crash` | kernel 层占比 >40% | warn |
| `low_reply_rate` | 14天回复率 <30% | warn |
| `emotion_stuck_low` | 元气 <15 持续 24h（≥6次评估中70%偏低） | info |
| `rapid_escalation` | 24h 孤独涨 >40 | info |
| `slow_tick` | 无 tick > 3h（可能 cron 频率过低） | info |
| `lancedb_possible_degradation` | 10+次发送无 memory 触发 | info |
| `manual_break_active` | 手动假期模式长期开启 | info |

### 10.4 独立看门狗（Watchdog）

`chiguo_watchdog.py` — 零依赖，可被 cron/systemd timer 独立调用。不依赖 daemon 进程存活，直接读状态文件 + 日志做判断。详见 §7 CLI。

**tick_seq 停滞/重启判定（2026-07-31 修复）**：`tick_seq` 回退（本次 < 上次）→ 视为 state 文件被删/重建后的重启：重置 `stall_since`、不告警、输出 `tick_restarted=True`；相等且 >3h 不增 → 停滞告警（现有逻辑）；前向推进 → 自动清除误报 `stall_since`。

### 10.5 Fuzz 测试

`test_monitor.py` 含 fuzz 测试：随机 200 条合法条目、边界极值（None/负数/超长字符串）、空日志+100条纯idle。确保 monitor 在任意输入下不崩溃。

### 10.6 设计原则

- **零新依赖** — 纯 stdlib
- **流式解析** — O(n) 时间, O(1) 内存（除 daily_counts）
- **优雅降级** — 文件缺失 → 空统计，不抛异常
- **防御式解析** — `state=None` / `emotion=None` / `cooldown=None` / 损坏行 → 自动规范化（`_normalize_entry`，stats() 与 alerts() 共用同一归一化，保证口径一致），不崩溃
- **回复率口径** — 相邻 send 的 `messages_without_reply` 双方均为数值才比较，None/非数值视为未知不计为回复变化（stats 与 alerts B5 一致）
- **配置回退** — `[monitor]` 配置相对路径在当前 cwd 找不到时回退模块目录，避免从其他 cwd 运行阈值静默回落默认值（与 health() 的 config 检测一致）；`lancedb_path` 优先 `[monitor]`，未定义时回退 `[memory]`（v10 统一，原代码只读 `[monitor]` 与 toml 注释约定不符）；路径值一律 `expanduser`（`~` 展开，v10）
- **独立可运行** — `python3 chiguo_monitor.py` / `python3 chiguo_watchdog.py`

### 10.7 对话日志与归档 (v5)

v5 新增完整的对话日志、归档、轮转、告警持久化和索引查询能力，所有模块零外部依赖。

#### 10.7.1 对话内容日志

`chiguo_decisions.jsonl` 新增两种记录类型：

**recv action** — 记录收到的主人消息：

```json
{
  "action": "recv",
  "ts": "2026-06-28T14:32:05",
  "text": "菓菓中午吃的什么",
  "analysis": {"warmth": 0.5, "effort": 0.6, "attention": 0.7},
  "emotion_after": {"loneliness": 38, "anxiety": 32, "affection": 56, "energy": 78}
}
```

**send/idle 条目新增 msg_id 字段** — 每条主动发送关联唯一消息 ID：

```json
{
  "action": "send",
  "msg_id": "msg_20260628_143205_a1b2c3",
  "trigger": "lonely_mid",
  "context": { ... }
}
```

`msg_id` 格式：`msg_{YYYYMMDD}_{HHMMSS}_{random6}`。idle 条目不含 `msg_id`（未产生消息）。

#### 10.7.2 对话归档

`chiguo_messages.jsonl` 独立存储完整对话记录，与 decisions 日志解耦。每条记录一行 JSON：

```json
{
  "msg_id": "msg_20260628_143205_a1b2c3",
  "ts": "2026-06-28T14:32:05",
  "direction": "out",
  "text": "谁、谁关心你中午吃什么了！只是刚好看到外卖单……",
  "trigger": "lonely_mid",
  "intensity": "medium",
  "emotion_snapshot": {"loneliness": 62, "affection": 55, "anxiety": 48, "energy": 72, "tsundere_index": 68},
  "user_emotion_analysis": null
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `msg_id` | string | 唯一消息 ID，与 decisions 中 send 条目对应 |
| `ts` | ISO 8601 | 消息时间戳 |
| `direction` | `"in"` / `"out"` | 方向：in=主人发送，out=菓菓发送 |
| `text` | string | 消息原文 |
| `trigger` | string or null | 触发类型（仅 out 方向），in 方向为 null |
| `intensity` | string or null | 触发强度（soft/medium/intense），in 方向为 null |
| `emotion_snapshot` | object | 发送/接收时的 5 维情绪快照 |
| `user_emotion_analysis` | object or null | LLM 分析结果（仅 in 方向有 --analysis 时） |

写入时机：
- **out 方向**：OpenClaw 生成并发送消息后，通过 `--record-send` CLI 写入
- **in 方向**：daemon 收到 `--user-msg` 时写入

#### 10.7.3 日志轮转

`chiguo_rotation.py` 模块按月轮转日志文件，防止单文件无限增长。

**轮转逻辑**：

```
check_rotation():
  ① 读取当前日志文件的最后一条记录时间戳
  ② 计算该时间戳所属月份
  ③ 如果月份 < 当前月份 → 触发轮转
  ④ rename: chiguo_decisions.jsonl → archive/decisions_2026-05.jsonl
  ⑤ rename: chiguo_messages.jsonl  → archive/messages_2026-05.jsonl
  ⑥ 新日志从当前月份重新开始写入
```

**archive/ 目录结构**：

```
archive/
├── decisions_2026-05.jsonl
├── decisions_2026-06.jsonl
├── messages_2026-05.jsonl
└── messages_2026-06.jsonl
```

**配置段** (`chiguo_proactive.toml` `[logging]`):

```toml
[logging]
rotate_enabled = true               # 是否启用日志轮转
rotate_interval = "monthly"         # 轮转间隔（monthly）
archive_dir = "archive"             # 归档目录（相对路径锚定模块目录，绝对路径原样保留）
decisions_log = "chiguo_decisions.jsonl"   # 决策日志路径
messages_log = "chiguo_messages.jsonl"     # 对话日志路径
alerts_log = "chiguo_alerts.json"          # 告警日志路径
max_archive_files = 24              # 最多保留归档文件数（超出删除最旧）
```

**路径锚定（2026-07-31）**：相对 `archive_dir`（如 `"archive"`）一律锚定 `chiguo_rotation.py` 所在目录（项目根），绝对路径原样保留——从任意 cwd 运行 `force_rotate`/`rotate_if_needed`/`--rotate` 都不会把日志移出项目（曾发生从 /tmp 运行把日志移进 /tmp/archive 的故障）。

轮转在 daemon tick 时自动触发（`_maybe_rotate_logs()`），每次 tick 前检查。

#### 10.7.4 告警持久化

`AlertManager` 类管理告警的完整生命周期，状态持久化到 `chiguo_alerts.json`。

**告警生命周期**：

```
active → acknowledged → resolved
  │           │              │
  │    (人工确认)     (问题修复后)
  ▼           ▼              ▼
P1 首次检测  管理员标记    自动/手动关闭
```

**chiguo_alerts.json 结构**：

```json
[
  {
    "alert_id": "alt_20260628_143205_crash_gap",
    "type": "crash_gap",
    "severity": "critical",
    "status": "active",
    "detected_at": "2026-06-28T14:32:05",
    "acknowledged_at": null,
    "resolved_at": null,
    "message": "daemon 无 tick 超过 6 小时",
    "context": {"last_tick": "2026-06-28T07:15:00", "gap_hours": 7.3}
  }
]
```

| 字段 | 说明 |
|------|------|
| `alert_id` | 唯一告警 ID，格式 `alt_{ts}_{type}` |
| `type` | 告警类型（同 §10.3 异常检测类型） |
| `severity` | critical / warn / info |
| `status` | active / acknowledged / resolved |
| `detected_at` / `acknowledged_at` / `resolved_at` | 各阶段时间戳 |
| `message` | 人类可读告警描述 |
| `context` | 告警上下文数据（触发时的具体值） |

**AlertManager 核心方法**：

```python
class AlertManager:
    def detect(self, monitor_result) -> list[Alert]     # 从监控结果检测新告警
    def acknowledge(self, alert_id: str)                # 确认告警
    def resolve(self, alert_id: str)                    # 解决告警
    def list_active(self) -> list[Alert]                # 列出活跃告警
    def list_all(self, status: str = None) -> list[Alert]  # 列出所有告警
    def _save(self)                                     # 持久化到 chiguo_alerts.json
    def _dedup(self, alert: Alert) -> bool              # 去重：同类型 active 告警不重复创建
```

**自动过期**：`severity=info` 的告警 7 天后自动标记 resolved。

#### 10.7.5 索引查询

`DecisionIndex` 类在内存中维护 decisions 日志的字节偏移量索引，支持 O(1) 随机访问。

**索引结构**：

```python
class DecisionIndex:
    _offsets: list[int]          # 每条记录的起始字节偏移量
    _timestamps: list[datetime]  # 对应时间戳
    _path: str                   # decisions 日志路径
    _dirty: bool                 # 是否需要重建索引

    def build(self)              # 全量扫描构建索引（O(n) 一次性）
    def refresh(self)            # 增量更新索引（读取新增行）
    def query(self, **filters)   # 按条件过滤查询
```

**query() 方法支持的过滤条件**：

```python
index.query(
    action="send",               # 按 action 过滤
    trigger="lonely_mid",        # 按触发类型
    date_from="2026-06-01",      # 起始日期
    date_to="2026-06-28",        # 结束日期
    limit=50,                    # 最大返回数
    offset=0                     # 分页偏移
)
# 返回: list[dict] — 完整的 decisions 条目列表
```

索引文件 `chiguo_decisions.index` 缓存偏移量数组（JSON），避免每次启动全量扫描。mtime 变化时自动增量刷新。

#### 10.7.6 新增 CLI

所有新增 CLI 通过 `chiguo_daemon.py` 统一入口：

```bash
# 对话查看
python3 chiguo_daemon.py --conversation              # 查看最近 20 条对话
python3 chiguo_daemon.py --conversation-days 7       # 查看最近 7 天对话
python3 chiguo_daemon.py --conversation-days 30      # 查看最近 30 天对话

# 对话导出
python3 chiguo_daemon.py --export                    # 导出全部对话为 JSON（stdout）
python3 chiguo_daemon.py --export --format csv       # 导出为 CSV
python3 chiguo_daemon.py --export --days 30          # 导出最近 30 天

# 记录发送（OpenClaw 回调）
python3 chiguo_daemon.py --record-send msg_20260628_143205_a1b2c3 \   # 记录菓菓发出的消息
  --text "谁、谁关心你了！" \
  --trigger lonely_mid --intensity medium   # 可选: 触发类型/消息强度

# 日志轮转（v7 补充: 锚定 base_dir，从任意工作目录运行都轮转项目文件）
python3 chiguo_daemon.py --rotate                    # 手动触发日志轮转
python3 chiguo_daemon.py --rotate --force            # 强制轮转（忽略月份检查）

# 告警管理
python3 chiguo_daemon.py --alerts-all                # 列出所有告警（含历史）
python3 chiguo_daemon.py --ack ALT_ID                # 确认指定告警（v7 补充: 不带 --alerts 时自动联动开启）
python3 chiguo_daemon.py --resolve ALT_ID            # 解决指定告警
```

**conversation 输出格式**（人类可读）：

```
2026-06-28 14:32 | 菓菓 → 主人 | 谁、谁关心你中午吃什么了！只是刚好看到外卖单……
2026-06-28 14:28 | 主人 → 菓菓 | 菓菓中午吃的什么
2026-06-28 09:15 | 菓菓 → 主人 | 早安……今天有课别忘了
```

---

## 十一、OpenClaw 集成

详见 `OPENCLAW_INTEGRATION.md`（v11）。关键两条链路：

1. **发送侧（trigger-script 门控）**：`openclaw cron add chiguo-check --every 15m --trigger-script scripts/chiguo-watch.js --session main` → 脚本零模型执行 `chiguo_daemon.py --compact` → idle 返回 `{fire:false}`（~90% 评估不唤醒 agent），send 返回 `{fire:true, message:<决策 JSON>}` → agent 按 SUN2.md 生成消息发送 → `--record-send <msg_id> --text <text> [--trigger] [--intensity]` 回写
2. **回复侧（standing order）**：微信消息到达 → agent 正常回复；standing order（agents/main/AGENTS.md）强制 LLM 情绪分析 → `--user-msg --analysis` 更新 daemon → SUN2.md 回复（替代 v4 的 UserPromptSubmit hook，无双重记录）

安装/卸载/校验由 `scripts/install_integration.sh` 完成（deploy.sh 第 5 步接入）；旧版 v4 cron system-event + hook 方案见 OPENCLAW_INTEGRATION.md §八降级路径。

---

## 十二、维护指南

### 更新课表
直接替换 `xskb.xlsx` 文件。下次 tick 检测到 mtime 变化 → 自动重解析。

### 更新学期
修改 `chiguo_proactive.toml` 中 `semester_start` 日期。`semester_end` 之后的日期自动视为假期。

### 更新节假日（2027+）
- 方式 A：修改 `holiday_parser.py` 中的 `HOLIDAYS` 和 `MAKEUP_WORKDAYS` 字典
- 方式 B：创建 `holidays.json` 文件，`HolidayParser(data_path="holidays.json")` 自动加载

holidays.json 格式：
```json
{
  "holidays": {
    "春节": {"start": "2027-02-05", "end": "2027-02-13"}
  },
  "makeup_workdays": {
    "2027-02-14": "春节调休"
  }
}
```

### 添加手动记忆
编辑 `chiguo_memories.json`：
```json
[
  {
    "type": "reminder",
    "content": "每周五晚上提醒主人看新番更新",
    "trigger_at": "2026-06-26T19:00:00"
  },
  {
    "type": "habit",
    "content": "午休时间提醒喝水",
    "trigger_window": [12, 13, 14]
  }
]
```

### 调整参数
编辑 `chiguo_proactive.toml`。关键旋钮：

| 想达到的效果 | 调的参数 |
|-------------|---------|
| 更频繁发消息 | ↓ `loneliness_gain_half_life` 或 ↑ `base_lambda` |
| 更少发消息 | ↑ `loneliness_gain_half_life` 或 ↓ `base_lambda` |
| 更快对沉默反应 | ↓ `loneliness_low_mid`（降低触发阈值） |
| 崩溃更罕见 | ↑ `loneliness_high_mid` |
| 收到回复后更快冷静 | ↓ `loneliness_decay_on_reply` |
| 每天最多发 N 条 | 改 `max_daily_active` / `max_daily_silent` |
| 减少概率累积 | ↓ `longing_growth_factor` 或 ↑ `anxiety_block_threshold` |
| 改变人格演变速度 | 改 `learning_rate`（`[bayesian]` 段） |

### 查看决策日志
```bash
tail -50 <仓库根目录>/chiguo_decisions.jsonl
```

### 重置状态
```bash
rm <仓库根目录>/chiguo_state.json
# 下次 daemon 启动自动重建，情绪回到初始值
```

---

## 十三、设计原则

1. **零 LLM token 消耗** — 决策引擎纯 Python 数学，不调用任何 LLM
2. **只读不写** — 对 OpenClaw 的 LanceDB 只读，不获取文件锁
3. **解耦 OpenClaw** — 所有强依赖通过 TOML 配置注入，路径/表名/通道均可改
4. **优雅降级** — LanceDB 不可用（未安装/连接失败，`memory_bridge` 惰性导入 + 60s 节流重试）→ 自动跳过，JSON 记忆兜底；课表不可用 → 默认开放
5. **确定性优先** — 课表/节假日用确定性解析，不靠 LLM
6. **平滑概率** — sigmoid 替代硬阈值，Hawkes 自激过程替代固定间隔，半衰期替代线性增减
7. **决策/生成分离** — daemon 只输出结构化 JSON，消息生成由 OpenClaw 的 chiguo skill 完成
8. **模块正交** — 情绪（快变量）、人格（慢变量）、Bayesian（用户推断）三者正交但互相调制

---

## 十四、v4 新增功能（2026-06-27）

### 多维人格系统
参考 soulforge 的 Big Five + MBTI。8 维人格：openness, conscientiousness, extraversion, agreeableness, neuroticism, tsundere_intensity, playfulness, attachment_style。情绪快变量 + 人格慢变量，互相调制。tsundere_index 向 personality.tsundere_intensity 基线回归。

### Bayesian 用户状态推断
参考 revive-companion。6 种隐藏状态：chatting, browsing, busy, sleeping, away, needs_care。从可观测信号（回复延迟/消息长度/沉默时长）推断 P(state|obs)。加权效用 = Σ P(state) × utility(state)。发送决策 = Bayesian 效用 × 课表可用性 × can_send 硬门控。

在线学习：BayesianLearner 用指数移动平均（lr=0.05）从观察中调优似然参数。

### Ebbinghaus 遗忘曲线
参考 MATE。R = e^(-t / (S × importance))。新/重要记忆权重高，旧/不重要记忆权重低。S=168h（7 天），min_weight=0.1（不彻底遗忘）。memory_bridge 新增 ebbinghaus_weight(), search_with_forgetting(), random_memory_with_forgetting()。

### 概率累积（Longing）
参考 revive-companion。不发消息时 `accumulated_lambda` 递增（每次 +growth_factor × held_count）。上限 base×5。焦虑 > `anxiety_block_threshold`（70）时阻塞累积。用户回复后 λ 回退（decay_factor=0.5）。

### 消息组合系统（Composer）
参考 Sebastian。Intent(A) × Cue(B) × Vibe(C) 三层组合。combo 尺寸概率：1 层 20%、2 层 50%、3 层 30%。Cue 按 personality + trigger_type 调制权重。Vibe 按时间/周末/考试周/假日选择。替代旧 situation_map 固定描述。

### 人格自适应
参考 soulforge。每次互动微调人格（< 0.2）。正面互动→更外向/宜人/低神经质；负面互动→反之。新增 reflect 触发（高好感+低沉默+高元气+低神经质→角色内省）。

### EventBus 架构
轻量发布/订阅模式。模块通过事件通信而非直接 import。事件类型：tick, user_message, character_message, trigger_evaluated, decision_made, state_changed, config_reloaded。

### 动态休眠调度
参考 Sebastian。`--loop` 模式不再固定 sleep。按 idle reason 计算最优休眠：quiet_hours → sleep 到 quiet_end, daily_limit → sleep 到明天 8:00, low_energy → sleep 到能量恢复, user_sleeping/busy → 等 1-2 小时。上限 2h 下限 1min。用户设定的 `--loop N` 作为最大上限（`N < 60` 时按 60 处理并 stderr 提示）。

---

## 十五、旧版文件

以下文件为 v1 遗留，daemon v2 起不依赖，已删除：

| 文件 | 原用途 | 代替 |
|------|--------|------|
| `main.py` | v1 主入口 | `chiguo_daemon.py` |
| `state.py` | v1 状态引擎 | `chiguo_state.py` |
| `daemon.py` | v1 决策引擎 | `chiguo_daemon.py` |
| `triggers.py` | v1 触发器 | `chiguo_trigger.py` |
| `generator.py` | v1 消息生成 | `chiguo_generator.py`（v2+ 曾沿用，2026-08-01 删除——消息生成移交 OpenClaw，daemon 只输出 JSON） |
| `sender.py` | v1 发送器 | `chiguo_sender.py`（v2+ 曾沿用，2026-08-01 删除——发送移交 OpenClaw） |
| `proactive.toml` | v1 配置 | `chiguo_proactive.toml` |
| `memories.json` | v1 记忆 | `chiguo_memories.json` |
| `demo_scenario.py` | v1 场景测试 | `chiguo_demo.py` |

## 十六、版本历史

| 版本 | 日期 | 变更 |
|:----:|:----:|------|
| v1 | 2025-12 | 初始版本。线性情绪 + 硬阈值触发 |
| v2 | 2026-01 | sigmoid 概率替代硬阈值，Poisson 过程，半衰期情绪模型 |
| v3 | 2026-03 | Hawkes 自激过程，话题注入，LLM 分析接口，忙碌抑制 |
| **v4** | **2026-06-27** | **Bayesian 用户推断，多维人格，Ebbinghaus 遗忘，Composer，Longing 概率累积，EventBus，人格自适应，动态休眠** |
| v5 | 2026-06-30 | 鲁棒性增强：状态备份/fsync/tick_seq/dampen/审计日志/PID锁/校验和 |
| **v6** | **2026-07-31** | **溢出逃生阀、状态路径锚定、flock写锁、反馈闭环、文件传参** |
| v7 | 2026-07-31 | 逃生阀约束：从未交互用户不触发逃生阀；`escape_valve_sleep_block` 睡觉门控二次确认；busy_suppressed 独立 reason 不累积 longing；**v7 补充（daemon 遗留修复）**：cwd 隔离（移除模块级 os.chdir）、`--loop` 最小 60s 提示、`--ack` 自动联动 `--alerts`、`--rotate` 锚定 base_dir |
| **v8** | **2026-07-31** | **用户状态渠道增强：双作息分桶学习（工作日/周末两桶独立窗口 + 调休/假期修正，STATE_VERSION 7→8）+ 听歌双向联动（睡眠窗口内播放反证：sleeping 置信度 ×0.5 压制 + record_active 反向校正生物钟）** |
| **v9** | **2026-07-31** | **网易云音乐渠道增强：对话内容源 + 鲁棒性 —— TopicPicker 第 8 源 netease（netease_weight=0.12）+ 网易云策略层 chiguo_netease（健康探针/登录失效检测/降级链/共享日配额 2/随机选源/peek-consume 两阶段，netease_health.json；STATE_VERSION 不变，无状态迁移）** |

### v3→v4 迁移

首次运行 v4 版本时自动检测 `STATE_VERSION=3`（config 中 `_version` 字段缺失或 =3），自动执行以下迁移：
- 创建 `personality` 字段（PersonalityTraits 默认值）
- 创建 `cooldown.accumulated_lambda` 字段（默认 0.0）
- 创建 `cooldown.held_count` 字段（默认 0）
- 创建 `cooldown.personality_history` 字段（默认空列表）
- 更新 `_version` 为 4
- 更新 `tsundere_index` 向 `personality.tsundere_intensity` 基线回归

配置文件 `chiguo_proactive.toml` 需手动补充 v4 新增段（`[personality]`、`[bayesian]`、`[composer]`、`[safety]`、`[monitor]`，以及 `[cooldown]` 和 `[memory]` 的新增字段）。推荐直接用最新的配置文件模板替换后按需调整。

### v4→v5 迁移

首次运行 v5 版本时自动检测 `STATE_VERSION=4`，自动执行以下迁移：
- 创建 `tick_seq` 字段（默认 0）
- `accumulated_lambda: null` → `0.0`（类型漂移修复）
- 更新 `_version` 为 5

v5 新增功能：
- **状态备份 `.bak`**：每次 save 前备份当前状态，JSON 损坏时自动恢复
- **`fsync` 落盘**：`os.replace` 前强制冲刷内核缓冲区
- **`tick_seq` 计数器**：每次 save 递增，watchdog 可检测前向进展
- **tmp 验证**：`os.replace` 前验证 tmp 是合法 JSON，防止截断写入覆盖好状态
- **OSError 保护**：磁盘满时不崩溃，跳过本次 save
- **损坏审计日志**：`chiguo_state_audit.jsonl` 记录所有恢复/删除事件
- **长时间停机 dampen**：`_tick()` 中 >24h 部分按 50% 强度推进（避免停机 7 天→情绪瞬间满格）
- **monotonic 时钟防护**：壁钟被 NTP 跳变时信任 `time.monotonic()`
- **naive datetime 时区补全**：旧状态文件中无时区的 ISO 时间戳自动补 `CST`

### v5→v6 迁移

首次运行 v6 版本时自动检测 `STATE_VERSION=5`，自动执行以下迁移：
- 创建 `cooldown.last_longing_break_at` 字段（默认 None）
- 更新 `_version` 为 6

v6 新增功能：
- **溢出逃生阀**：anxiety≥阻塞阈值 + 墙钟沉默≥72h + 冷却期外 → 强制 longing 破防发送，打破死锁态
- **状态路径锚定**：所有运行时文件基于 config 所在目录解析，daemon 启动打印路径
- **flock 跨进程写锁**：state save() 前获取 fcntl.LOCK_EX，防止 cron 并发写覆盖
- **反馈闭环**：`--send-result` CLI（幂等退款）、`--user-msg-file`/`--analysis-file` 文件传参
- **SKILL.md §5**：OpenClaw agent 发送后回传 `--send-result` 指令
- **monitor stats 增强**：统计新增 `send_result: {success, failed}` 字段

### v6→v7 迁移（v7 拟人化增强，2026-07-31）

首次运行 v7 版本时自动检测 `STATE_VERSION=6`，自动执行以下迁移：
- 创建 `circadian` 字段（`CircadianTracker`：reply_days/quiet_start/quiet_end/confidence/sample_days，默认睡眠窗口 0-8，字段白名单过滤加载）
- 创建 `pending_topics` 列表（默认 `[]`，条目 `{topic, source, created_at, attempted}`）
- 更新 `_version` 为 7

v7 新增功能：
- **生物钟学习**（`chiguo_circadian.py`）：每次主人回复记录小时 → 滚动 14 天 → 环形滑动窗口（宽度 5-12h）取回复最少时段为睡眠窗口；置信度 = 完整度 × 安静度，≥ `min_confidence`（0.5）才应用到静默窗口，否则回退配置默认 0-8
- **接话茬**（follow_up）：`on_user_message` 摄入 analysis topic/topic_resolved → `pending_topics` 管理（add/resolve/mark_attempted/prune，上限 20）；触发评估年龄 [2h, 48h] 窗口 + 钟形权重，单次尝试，过期清理；无 pending 时近期用户相关记忆兜底
- **热重载同步**：`_maybe_reload_config()` 热重载后重新 `_sync_quiet_window()`，学习窗口不因配置重载而陈旧

### v7 补充：daemon 遗留修复（cwd 隔离 / CLI 语义，2026-07-31）

- **cwd 隔离**：删除模块级 `os.chdir()`（原导入时静默劫持调用方 cwd）；默认 config 路径锚定到脚本所在目录；所有运行时文件继续走 `_base_dir` 锚定，从任意工作目录运行 CLI 均读写项目文件
- **`--loop N` 语义**：help 明确"最小 60 秒"；`N < 60` 时 stderr 提示 `interval < 60, using 60`（`max(60, …)` 下限逻辑不变）
- **`--ack` 自动联动**：给 `--ack` 但未给 `--alerts` 时自动开启 alerts 处理（stderr 提示），不再静默忽略
- **`--rotate` 路径锚定**：轮转文件与 `archive/` 目录均锚定 `engine._base_dir`，从其他目录运行不再轮转错文件

### v7→v8 迁移（v8 用户状态渠道增强，2026-07-31）

首次运行 v8 版本时自动检测 `STATE_VERSION=7`，自动执行 `_migrate_circadian_v8()`：

- reply_days/active_days 无 `bucket` 字段的旧条目 → 按日期 `weekday() < 5` 启发式补桶（历史数据无节假日判定；日期解析失败 → 丢弃该条）
- 旧单桶窗口继承：仅当 `weekday_*` 与 `weekend_*` 全部为默认值（0/8/0.0）且旧 `confidence > 0` → 把旧 quiet_start/quiet_end/confidence 继承到 `weekday_*`（防止 v8 风格状态中"周末桶快照"被误继承为工作日窗口）
- 更新 `_version` 为 8

v8 新增功能：
- **双作息分桶学习**（`chiguo_circadian.py`）：`bucket_for(dt, is_holiday, is_makeup_workday)` 分桶（调休上班日 → weekday，节假日 → weekend，周五 20:00 后/周六全天/周日 20:00 前 → weekend）；`CircadianTracker` 存两套并列字段 `weekday_*`/`weekend_*` + `active_days`（听歌活跃），`quiet_start/quiet_end/confidence` 保留为"当前生效桶快照"（`set_active_bucket`/`_sync_quiet_window` 更新）；`recompute` 两桶独立估计（reply+active 逐小时合并计数、桶内日期去重、数据不足不覆盖）
- **听歌双向联动**（`netease_bridge.py` + `chiguo_daemon.py`）：`fetch_recent_play(limit=20, ttl_minutes=15)` 拉取近一周播放记录（`recent_play_cache.json` 原子写缓存，负年龄/损坏缓存不命中，失败不缓存）；daemon `_check_play_proof(now)` 仅睡眠窗口内拉取，2h 证据窗内播放时刻在窗口内 → `record_active`（按播放时刻分桶）→ recompute + `_sync_quiet_window` 反向校正；evaluate() 睡觉门控与逃生阀 `sleeping_guard` 用 `effective_conf = raw_conf × sleeping_confidence_factor`（有播放证据时，默认 0.5）
- **`[netease]` 配置段**：play_cache_ttl_minutes=15 / play_proof_window_hours=2.0 / sleeping_confidence_factor=0.5

## 十七、鲁棒性设计

### OpenClaw 停机场景

OpenClaw cron 停止时 daemon 不执行。恢复后：

1. **情绪推进**：`_tick()` 读取 `last_tick` 时间戳计算间隔，≤24h 全量推进，>24h dampen (50%)
2. **日期计数重置**：`_check_daily_reset()` 按 `current_date != today` 比较，跨多天自动归零
3. **状态恢复**：`_load()` 优先读主文件→.tmp 恢复→.bak 恢复→删除损坏文件→默认值
4. **崩溃循环防护**：safety_level 2 时所有触发降级为温和模式

### 状态持久化保证

- **原子写入**：tmp→fsync→验证→os.replace（POSIX 原子 rename）
- **`.bak` 备份**：每次 save 前 `shutil.copy2`，单文件永远只有最新快照
- **损坏恢复链**：main → .tmp → .bak → 删除重建
- **tick_seq 单调递增**：每次成功 save +1，watchdog 持久化上次值到 `chiguo_watchdog_state.json`，seq 停滞超过 3h 则告警（`stall_since` 记录首次停滞时刻，seq 恢复自动清除）
- **审计日志**：所有恢复/删除/校验失败/时钟异常事件记录到 `chiguo_state_audit.jsonl`
- **校验和**：`save()` 时 SHA256(payload) → `_checksum` 字段，`_load()` 时验证，不匹配 → raise ValueError 强制走 `.bak` 恢复链（v6，位翻转/手改后宁可回退也不带病运行）
- **跨进程写锁**：`state_lock()` contextmanager（fcntl flock + 模块级 fd 缓存可重入 + 5s LOCK_NB 超时审计），`save()` 复用统一锁；cron（trigger-script 评估）与 agent（standing order 调 --user-msg）并发时 read-modify-write 临界区可显式加锁
- **PID 锁文件**：`--loop` 模式启动时检查 `chiguo_loop.pid`，已存在且进程存活则拒绝启动，退出时清理
- **时钟异常检测**：壁钟倒退和 NTP 前跳均记录审计日志

### 已知局限

- PID 锁仅 `--loop` 模式生效，cron 单次执行无需
- 反馈闭环依赖 OpenClaw agent 主动回传 --send-result，若 agent 未配置此步骤则发送结果仍不可知
- `state_lock()` 是显式锁：`save()` 内部已持锁，但 cron 与 agent（--user-msg）各自的 read-modify-write 若未包在 `with state_lock():` 内，仍存在 lost update 窗口
- watchdog 的 `stall_since` 检测对 state 文件重建（tick_seq 归零）会误报 —— **2026-07-31 已修复**：tick_seq 回退（< prev_seq）视为重启（重置 `stall_since`、不告警、输出 `tick_restarted` 标记）；仅相等且 >3h 不增才告警停滞；下一次运行自动自愈清除旧误报（现网 `stall_since=16:41` 误报已实测清除）
- 生物钟学习依赖主人回复样本：冷启动（<7 个有回复日或置信度 <0.5）该桶回退配置默认静默窗口 0-8；学习窗口是统计估计，异常作息（考试周熬夜/时区变化）由置信度门槛与滚动窗口自动衰减
- 双作息迁移是启发式：历史无 bucket 字段的条目按 `weekday() < 5` 补桶，无节假日判定——节假日/调休日的历史回复可能被补入错误桶，随滚动窗口自然衰减
- 周末数据天然稀疏（每周仅 2/7 天）：周末桶的 sample_days 累积慢，约 3-4 周才可能激活周末学习窗口；未达标时周末回退配置默认 0-8（不影响工作日桶）
- 听歌反证依赖网易云登录态与网络：未登录/API 不可用时本轮跳过反证（不阻塞、不告警），sleeping 推断回到纯 Bayesian；反证只在评估时点生效，不持久化"醒着"状态
- 音乐话题源依赖网易云登录态与 API：未登录/API 不可用 → 策略层降级为故障话题（日配额 1）或静默跳过；登录失效后最长 `reprobe_minutes`（30）重探间隔内不出网络请求
- 网易云每日推荐需登录 cookie（MUSIC_U），匿名账号无每日推荐权限；红心歌单/听歌情绪分析 YAGNI 暂缓（需 LLM，违背零 LLM 铁律）
- 热重载非法 `retry_count` 会抛异常（Minor）：`set_api_retry_policy` 的 int()/float() 强转对非数值配置未兜底（`NeteaseService` 构造即调用，仅配置错误时触发）
- `_in_quiet_window` 双份拷贝：`chiguo_daemon` 与 `chiguo_netease` 各一份（同语义跨午夜），`chiguo_topics` 复用 chiguo_netease 版——语义修改需两处同步
- 接话茬素材依赖 OpenClaw 传入 `--analysis` 的 topic 字段；未传 topic 时仅剩记忆兜底路径
