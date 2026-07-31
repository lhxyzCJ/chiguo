# 迟菓拟人化增强设计 — 生物钟学习 + 接话茬

> 版本: v2 | 日期: 2026-07-31 | 状态: 已批准(复盘已砍)
> 路线: 方案 A — 全部融入现有零 LLM 决策引擎,新增独立小模块,保持决策/生成分离铁律

## 一、目标

在现有 5 维情绪 + 8 维人格 + 12 触发类型 + Bayesian 用户推断的基础上,增加两项行为让迟菓更拟人:

1. **生物钟学习**:从用户实际回复时间学习睡眠/活跃时段,动态调整静默窗口
2. **接话茬**:记住用户上次没聊完的话题,几小时后自然接续,形成连续对话感

两项均为**决策引擎侧**改动(何时发、发什么素材),消息文本仍由 OpenClaw 侧 LLM 生成。

**设计原则**:拟人的关键是"像真人在对面"——行为的时机由真实信号(对方作息、对话内容)驱动,而不是固定排程。固定时间点的"复盘总结"被砍掉,正因为它本质是报表行为,与拟人目标相悖。

## 二、架构总览

```
chiguo_daemon.py (DecisionEngine)
  ├─ chiguo_circadian.py (NEW) → 作息学习:滚动窗口 → 睡眠时段 + 置信度
  └─ chiguo_state.py           → 新增 pending_topics / circadian 持久化字段
      └─ chiguo_trigger.py     → 新增 follow_up 触发类型
```

- 所有新增参数进 `chiguo_proactive.toml`(新增 `[circadian]` 段 + `[trigger]` 新键)
- 状态持久化进 `chiguo_state.json`(沿用原子写 + 校验和机制,state 版本号 +1)
- 无需新增运行时文件

## 三、Section 1 — 生物钟学习(动态静默窗口)

### 3.1 新模块 `chiguo_circadian.py`

纯函数为主,可独立测试:

- **数据采集**:`ChiguoState.record_user_message()` 时,把回复的小时(0-23)追加到滚动数组 `reply_hours`(最近 14 天,天数为 toml `[circadian] history_days`,默认 14),连同日期一起存 `chiguo_state.json` 的 `circadian` 字段
- **学习算法**(`estimate_sleep_window(hour_counts, config)` 纯函数):
  1. 统计 14 天内每小时回复频率
  2. 找"最低活跃的连续时段"为候选睡眠窗口(允许跨午夜,如 23:00-07:00)
  3. 置信度 = 数据完整度(有数据天数 ≥ `min_sample_days` 默认 7)与窗口内活跃度低程度的组合
- **输出**:`{quiet_start, quiet_end, confidence, sample_days}`

### 3.2 应用点

- 替换 `_sleep_hours_in_range` 的窗口来源:置信度 ≥ 阈值(默认 0.5)时用学习窗口,否则回退 toml `[schedule] quiet_start/quiet_end`(现 0-8,冷启动行为不变)。审计修复:发送门禁(can_send)、睡眠循环(_idle_reason/_dynamic_sleep_interval/_estimate_next_check)、_is_free_time 统一经 `CooldownState.quiet_window()` 读活动窗口,学习窗口全局生效
- 通过现有 `set_quiet_window()` 注入机制,不改 `silent_hours()` 语义

### 3.3 可调参数(`[circadian]`)

| 键 | 默认 | 含义 |
|----|------|------|
| `history_days` | 14 | 回复记录滚动窗口 |
| `min_sample_days` | 7 | 最少有数据天数才计算 |
| `min_confidence` | 0.5 | 低于此置信度回退默认窗口(与 min_sample_days 匹配,7 天数据可激活) |

## 四、Section 2 — 接话茬(对话连贯)

### 4.1 数据流

- **analysis JSON 扩展**:OpenClaw 侧 `UserPromptSubmit` 分析用户在 `--analysis` 中新增可选字段:
  - `topic`: 当前消息话题关键词(如"比赛")
  - `topic_resolved`: bool,此消息是否把之前的话题聊完了
- **`pending_topics` 状态**(存 state,列表项 `{topic, source, created_at, attempted}`):
  - 收到带 `topic` 的分析 → 若与现有条目关键词相同视为已接续(更新/移除),否则追加 `source="analysis"`
  - 收到 `topic_resolved=true` → 移除对应话题
  - **兜底源**:评估时若 `pending_topics` 为空,从 `memory_bridge.recent(48h)` 挑 `user_relevant` 且重要度 ≥ 0.4 的记忆作为候选 `source="memory"`(不落盘,仅本次候选)

### 4.2 `follow_up` 触发类型

- **资格条件**:存在 pending 话题,年龄在 [min_age=2h, max_age=48h] 内,冷却期内,今日发送数未达上限
- **权重**:`[trigger] follow_up_weight`(初始 0.35),随年龄钟形调制(峰值在 2-6h 最自然),低于 `min_weight`(0.03)不触发。审计修复:窗口内**所有**话题逐个计算权重并加候选(原实现只取第一个,最老话题权重过低会饿死更年轻话题),选中者才被标记
- **频控**:每个话题最多尝试 1 次,触发后标 `attempted=true`
- **输出 context**:`{topic, source, age_hours}`,LLM 侧生成"对了,你上次说的……后来怎么样了"
- **清理**:每次评估顺带移除过期(>48h)/已解决/已尝试的话题,防状态膨胀(上限 20 条,超出丢弃最旧)

## 五、错误处理与降级

| 场景 | 行为 |
|------|------|
| 回复数据不足(冷启动) | 静默窗口回退默认 0-8,行为与现在完全一致 |
| analysis 无 topic 字段 | 走记忆兜底,或本功能不触发 |
| memory_bridge 不可用 | 兜底候选跳过,不影响其他触发 |
| pending_topics 膨胀 | 上限 20,丢弃最旧 |
| state 旧版本无新字段 | 加载时补默认值,不崩溃(沿用现有迁移机制) |

## 六、测试计划

- 新建 `test_circadian.py`:纯函数测(窗口检测/跨午夜/置信度/冷启动回退)+ 状态集成测(record_user_message 追加、set_quiet_window 生效、sample_days 不足回退)
- 新建 `test_followup.py`(或并入 test_trigger.py):资格窗口边界(2h/48h)、钟形权重、单次尝试、topic_resolved 移除、过期清理、记忆兜底、上限 20
- 确定性:固定种子 + 固定 CST 时间,复用现有测试基建
- 全量 14 文件回归不受影响

## 七、文档同步

按项目规范:改后同步 `doc/SYSTEM.md`、`doc/IMPROVE.md`、`doc/README.md`,并在 `MEMORY.md` 记录。

## 八、范围外(YAGNI)

- 早安/晚安窗口自适应(本次只做静默窗口)
- 用户活跃曲线完整建模(只做睡眠时段)
- 固定排程型"复盘/总结"消息(与拟人目标相悖,已砍)
- 消息侧表达变化(错别字/标点/表情包) — LLM 侧职责,不在本设计
- 朋友圈/分享行为 — 另行迭代
