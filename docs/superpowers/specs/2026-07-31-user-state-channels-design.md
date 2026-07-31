# 迟菓用户状态渠道增强设计 — 双作息学习 + 听歌双向联动

> 版本: v1 | 日期: 2026-07-31 | 状态: 已批准
> 路线: 延续 v7 零 LLM 决策引擎架构,扩展现有生物钟模块与网易云桥接,保持决策/生成分离铁律

## 一、目标

在 v7(生物钟学习 + 接话茬)基础上,新增两个用户状态获取渠道:

1. **双作息学习**:工作日/周末两套睡眠窗口独立学习与应用,叠加节假日调休修正(调休上班日算工作日、假期算周末)
2. **听歌状态双向联动**:网易云最近播放记录作为"夜间活跃反证"——睡眠窗口内刚有播放 → 用户醒着,压制 Bayesian sleeping 推断,同时反向校正生物钟窗口

均为**决策引擎侧**改动,消息生成仍由 OpenClaw LLM 侧负责。

## 二、Section 1 — 双作息学习

### 2.1 分桶规则(纯函数,可注入判定器)

`bucket_for(dt: datetime, is_holiday: Callable, is_makeup_workday: Callable) -> str`:

1. 调休上班日(`is_makeup_workday`)→ `weekday`
2. 节假日(`is_holiday`,非调休)→ `weekend`
3. 周五 20:00 后、周六全天、周日 20:00 前 → `weekend`
4. 其余 → `weekday`

判定器由 `ChiguoState.holiday_parser`(已挂载,含 `is_holiday`/`is_makeup_workday`)注入;测试用 fake callables 保证确定性。

### 2.2 状态结构(CircadianTracker 改造)

- `reply_days` 条目新增 `"bucket": "weekday" | "weekend"` 字段;`track_reply_hour` 增加 bucket 参数
- 两桶各自独立聚合(`aggregate_hours` 按桶过滤)→ 各自独立 `estimate_sleep_window`
- tracker 存两套并列字段:`weekday_quiet_start/weekday_quiet_end/weekday_confidence` + `weekend_quiet_start/weekend_quiet_end/weekend_confidence`(不嵌套,保持与现有 dataclass 字段过滤加载一致)
- **旧状态迁移**:无 bucket 字段的条目按 `weekday() < 5` 启发式补桶(历史数据无节假日判定,可接受)
- **稀疏保护**:周末数据天然稀疏(2/7),`min_sample_days=7` 意味着周末窗口约 3-4 周才可能激活;未达标 → 该桶回退配置默认 0-8,不影响另一桶

### 2.3 应用

- `_sync_quiet_window` 按当前时刻 `bucket_for(now)` 选取对应桶窗口(置信度 ≥ min_confidence 0.5 才应用,否则回退默认)
- 冷启动/单桶未达标 → 行为与 v7 完全一致
- 发送门禁/睡眠循环继续经 `CooldownState.quiet_window()` 读取,无需改动

### 2.4 参数

复用现有 `[circadian]` 段参数,不新增键(min_sample_days/history_days/min_confidence/min_width/max_width 两桶共用)。

## 三、Section 2 — 听歌状态双向联动

### 3.1 数据源

- `netease_bridge.py` 新增 `fetch_recent_play(limit=20) -> list[dict] | None`:`GET /user/record?type=1&limit=20`(近一周播放记录,条目含 `playTime` epoch-ms 时间戳与歌曲信息)
- 新增独立缓存 `recent_play_cache.json`(与现有 `netease_cache.json` 分开),TTL 15 分钟(config 化),防每次评估打 API
- 未登录/服务不可用/解析失败 → None,优雅降级

### 3.2 触发时机(YAGNI)

- 仅当**当前时刻落在生效睡眠窗口内**(按当日双作息窗口选取)才拉取播放记录;白天不拉
- 播放证据判定:窗口内最近 `play_proof_window_hours`(2h)内有播放(playTime ∈ [now-2h, now] 且落在窗口时段)

### 3.3 双向联动(daemon evaluate 内)

1. **反证 sleeping**:有播放证据 → 用户醒着
   - Bayesian sleeping 置信度 × `sleeping_confidence_factor`(0.5);压到阻塞阈值以下 → 不阻塞发送
   - 逃生阀 sleeping_guard 同步放行
2. **反向校正生物钟**:窗口内播放时间点记入 `CircadianTracker.record_active(now)`(新增 `active_days` 数组,与 reply_days 同结构,条目带 bucket 按同一分桶规则);聚合时同小时计数与回复合并 → 深夜多次听歌使该时段活跃计数上升 → 窗口自动偏移
   - 挂机播放风险:频率低 + 置信度门槛天然保护

### 3.4 参数(新 `[netease]` 段)

| 键 | 默认 | 含义 |
|----|------|------|
| `play_cache_ttl_minutes` | 15 | 播放记录缓存 TTL |
| `play_proof_window_hours` | 2.0 | 播放证据时间窗 |
| `sleeping_confidence_factor` | 0.5 | sleeping 置信度压制系数 |

### 3.5 错误处理

| 场景 | 行为 |
|------|------|
| API 不可用/未登录 | 返回 None,本轮跳过反证,不阻塞不告警 |
| 缓存过期/解析失败 | 同上 |
| 播放时间戳缺失/非法 | 跳过该条,不崩溃 |
| 睡眠窗口动态变化 | 反证只在评估时点生效,不持久化"醒着"状态(避免状态污染) |

## 四、范围外(YAGNI)

- 实时"正在播放"接口(网易云个人账号无此能力)
- 听歌作为"空闲/忙碌"信号(用户只选了双向联动语义)
- 歌单风格/情绪分析(需 LLM,不在决策引擎)
- 第三套作息(假期/寒暑假)——现有 holiday 判断已覆盖假期,桶只有两套

## 五、测试计划

- `test_circadian.py` 扩展:分桶纯函数(调休/假期/周五晚边界/周日晚边界)、双桶独立估计、旧状态补桶迁移、周末稀疏回退、应用时按桶选窗、record_active 合并计数
- 新 `test_netease_proof.py`(或并入 test_followup 同级):fetch_recent_play 解析/缓存/降级(fake 服务)、窗口内反证触发 sleeping 压制、窗口外不拉取、播放时间戳非法跳过、逃生阀 sleeping_guard 放行
- 确定性:固定种子 + 固定 CST 时间;netease API 用 monkeypatch/fake
- 全量 16 文件回归不受影响(听歌拉取仅在窗口内,测试环境 fake 不可用 → 跳过)

## 六、文档同步

按项目规范:改后同步 `doc/SYSTEM.md`、`doc/IMPROVE.md`、`doc/README.md`、`MEMORY.md`、`CLAUDE.md`/`AGENTS.md`(测试文件数若有变化)。
