# 用户状态渠道增强实现计划 — 双作息学习 + 听歌双向联动

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 双作息(工作日/周末)独立睡眠窗口学习 + 网易云最近播放记录作为夜间活跃反证(压制 sleeping 推断 + 反向校正生物钟)。

**Architecture:** 扩展 `chiguo_circadian.py`(bucket_for 分桶 + CircadianTracker 双桶字段 + active_days)、`chiguo_state.py`(按桶记录/迁移/按桶选窗)、`netease_bridge.py`(fetch_recent_play + 缓存)、`chiguo_daemon.py`(窗口内拉取 + sleeping 压制 + record_active)、`chiguo_proactive.toml`([netease] 段)。保持 v7 的 `quiet_window()` 门禁链路不动:tracker 的 `quiet_start/quiet_end/confidence` 保留为"当前生效桶快照",`_sync_quiet_window` 同步它。

**Tech Stack:** Python 3.14 (uv), stdlib, 本地 NeteaseCloudMusicApi (localhost:3000, 不可用时优雅降级)。

**Spec:** `docs/superpowers/specs/2026-07-31-user-state-channels-design.md`(v1,已批准)

---

### Task 1: 双作息 — bucket_for 纯函数 + CircadianTracker 双桶改造

**Files:**
- Modify: `/root/character_test/chiguo_circadian.py`
- Modify: `/root/character_test/test_circadian.py`

**接口契约(精确):**

```python
def bucket_for(dt: datetime, is_holiday: Callable, is_makeup_workday: Callable,
               weekend_start_hour: int = 20, weekend_end_hour: int = 20) -> str:
    """分桶:调休上班日 → weekday;节假日(非调休) → weekend;
    周五 weekend_start_hour 后、周六全天、周日 weekend_end_hour 前 → weekend;其余 → weekday。
    判定顺序:调休优先(is_makeup_workday 为真直接 weekday),再节假日(is_holiday 为真直接 weekend),
    然后时间规则(dt.weekday(): 4 且 hour >= start → weekend;5 → weekend;6 且 hour < end → weekend)。"""

def track_reply_hour(reply_days, now, history_days, bucket: str) -> list[dict]:
    """v7 基础上:新条目带 "bucket" 字段;同日同桶合并,同日不同桶 → 分开条目(一天可跨桶,如周五 20:00 前后)。
    修剪逻辑不变(按最晚日期排他 > cutoff)。"""

def aggregate_hours(reply_days, bucket: str | None = None) -> list[int]:
    """v7 基础上:bucket 参数过滤(bucket=None 全部)。合并 active_days 由调用方传入同一数组或单独调用相加。"""

def count_sample_days(reply_days, bucket: str | None = None) -> int:  # 同过滤

def estimate_sleep_window(hour_counts, sample_days, history_days, ...) -> dict | None:  # 不变
```

`CircadianTracker` 字段(v8):
```python
reply_days: list[dict]                       # {"date", "hours", "bucket"}
active_days: list[dict] = field(default_factory=list)  # 听歌活跃,同结构
# 兼容保留(当前生效桶快照,由 _sync_quiet_window 更新):
quiet_start: int = 0
quiet_end: int = 8
confidence: float = 0.0
sample_days: int = 0
# 双桶(独立学习):
weekday_quiet_start: int = 0
weekday_quiet_end: int = 8
weekday_confidence: float = 0.0
weekend_quiet_start: int = 0
weekend_quiet_end: int = 8
weekend_confidence: float = 0.0
```

方法:
```python
def record(self, now, history_days=14, bucket="weekday"):
    """reply 记录(带桶)。非列表 reply_days → 重置。"""
def record_active(self, now, history_days=14, bucket="weekday"):
    """听歌活跃记录(带桶)。非列表 active_days → 重置。"""
def recompute(self, min_sample_days=7, history_days=14, min_width=5, max_width=12):
    """分别对两桶(reply+active 合并计数)估计,写 weekday_*/weekend_*;
    桶内数据不足 → 该桶保持当前值(不覆盖);sample_days 更新为有数据天数(跨桶去重日期)。"""
def bucket_window(self, bucket: str) -> tuple[int, int, float]:
    """返回该桶 (start, end, confidence)。"""
def set_active_bucket(self, bucket: str, start: int, end: int, confidence: float):
    """把某桶窗口同步到兼容字段 quiet_start/quiet_end/confidence(供 _sync_quiet_window 与门禁使用)。"""
```

**合并计数语义**:recompute 时某桶的 hour_counts = aggregate(reply_days, bucket) + aggregate(active_days, bucket)(逐小时相加);sample_days 为该桶"有 reply 或 active 的天数"(按日期去重)。

**测试断言清单**(写测试时逐条覆盖,固定种子 + 固定 CST):
1. bucket_for: 周六调休(fake is_makeup_workday=True)→ weekday;周中假期(fake is_holiday=True)→ weekend;周五 19:59 → weekday、20:00 → weekend;周六全天 → weekend;周日 19:59 → weekend、20:00 → weekday;普通周一 → weekday
2. track_reply_hour: 同日同桶合并、周五跨桶(20:00 前 weekday 条目 + 20:00 后 weekend 条目 → 两条独立条目)、修剪不变
3. aggregate/count 按桶过滤
4. recompute 双桶独立:14 天工作日零回复 0-5 + 周末数据 → weekday 窗口 0-5、weekend 窗口由周末数据决定;某桶不足 min_sample_days → 该桶不覆盖(保持默认 0-8)
5. record_active: 合并计数生效(如周末桶只有 1 天 reply,active 补到 7 天后可激活)
6. bucket_window/set_active_bucket 往返
7. 旧格式条目无 bucket → aggregate(bucket=...) 行为:无 bucket 条目算哪桶?→ **迁移在 Task 2 的 state 层做**;本 Task aggregate 对无 bucket 条目:过滤时视为不属于任何桶(丢弃)——由迁移保证加载后都有 bucket。测试确认。

**验证**:`cd /root/character_test && uv run python test_circadian.py` — 全部 PASS(新增 + 既有 18 个 v7 测试,若 v7 测试因签名变化需微调,保持断言语义不变)。

**关键注意**:不要破坏 v7 已有测试的断言语义;`record`/`track_reply_hour` 签名变化后,v7 测试调用处需同步(记录在报告中)。

---

### Task 2: 双作息 — state 集成

**Files:**
- Modify: `/root/character_test/chiguo_state.py`
- Modify: `/root/character_test/test_circadian.py`

**接口契约:**

1. `on_user_message`:记录时按当日分桶:
```python
bucket = bucket_for(now, self.holiday_parser.is_holiday,
                    self.holiday_parser.is_makeup_workday)
self.circadian.record(now, circ_cfg.get("history_days", 14), bucket)
```
(recompute 参数不变;recompute 内部按桶)

2. `_sync_quiet_window` 改为按当前时刻选桶:
```python
def _sync_quiet_window(self):
    """v8: 按当前时刻分桶选窗口;置信度达标 → 学习窗口,否则回退配置默认。"""
    cfg = self.config.get("circadian", {})
    bucket = bucket_for(datetime.now(CST), self.holiday_parser.is_holiday,
                        self.holiday_parser.is_makeup_workday)
    start, end, conf = self.circadian.bucket_window(bucket)
    self.circadian.set_active_bucket(bucket, start, end, conf)
    if conf >= cfg.get("min_confidence", 0.5):
        self.cooldown.set_quiet_window(start, end)
    else:
        self._apply_quiet_window()
```
注意:now 参数 —— `_sync_quiet_window` 目前无参(v7 是 `_sync_quiet_window(self)`),daemon 热重载/on_user_message 都调用它。**改为可选参数 `now: datetime | None = None`**(None → datetime.now(CST)),调用方不用改。

3. 迁移(`_apply_loaded_data` 加载 circadian 后):旧格式 reply_days 条目无 bucket → 按 `weekday() < 5 → "weekday" else "weekend"` 启发式补桶(解析条目 date 字符串;解析失败丢弃);旧字段 quiet_start/quiet_end/confidence 迁移到 weekday_*(若 weekday_* 为默认 0/8/0 且旧 confidence > 0);active_days 缺失 → 默认空。

4. `STATE_VERSION` 7 → 8。

**测试断言清单:**
1. on_user_message 在周六 → weekend 桶记录;周一 → weekday 桶
2. 迁移:构造 v7 格式 state(无 bucket 条目 + 旧 quiet_start/end/confidence)→ 加载后补桶 + weekday_* 继承旧窗口
3. 按桶选窗:注入两桶不同窗口 → 周六评估时 cooldown 窗口 = weekend 桶;周一 = weekday 桶(置信度均达标)
4. 周末桶置信度不足 → 周六回退默认 0-8;工作日桶达标不受影响
5. 跨桶一天(周五 20:00 前后各一次回复)→ 两条独立条目,各自计入对应桶
6. 既有 18+ 个 v7 测试回归全过

**验证**:`cd /root/character_test && uv run python test_circadian.py && uv run python test_trigger.py && uv run python test_escape_valve.py && uv run python test_integration.py`

---

### Task 3: netease_bridge.fetch_recent_play + 缓存

**Files:**
- Modify: `/root/character_test/netease_bridge.py`
- Create: `/root/character_test/test_netease_proof.py`(纯 bridge 部分;daemon 联动在 Task 4)

**接口契约:**

```python
RECENT_PLAY_CACHE_FILE = os.path.join(模块目录, "recent_play_cache.json")

def fetch_recent_play(limit: int = 20, ttl_minutes: int = 15,
                      now: datetime | None = None) -> list[dict] | None:
    """GET /user/record?type=1&limit=N。返回 [{playTime: epoch_ms, name, artist}...] 简化条目;
    缓存命中(ttl 内)直接返回缓存;失败/未登录 → None。
    缓存格式 {"fetched_at": iso, "plays": [...]};缓存文件损坏 → 视为未缓存。"""
```

- 复用 `_api_get` / `_load_cookie`;未登录检查沿用 `check_health` 的 `/login/status` 模式或直接尝试后降级
- 条目简化:playTime(ms int)+ name/artist 保留供日志;非法 playTime → 过滤该条(不是 None)
- 缓存写入复用 `_save_cache` 模式(原子写 tmp → replace,失败不阻塞)

**测试断言清单**(test_netease_proof.py,monkeypatch `netease_bridge._api_get`):
1. 成功:fake _api_get 返回 {code:200, data:{list:[...]}} → 返回简化条目,playTime 保留
2. 缓存命中:第一次拉取后第二次调用不触发 _api_get(计数断言)
3. 缓存过期:now 超过 ttl → 重新拉取
4. API 失败(_api_get → None)→ None;缓存文件损坏 → 重新拉取不崩
5. 非法 playTime 条目被过滤
6. code != 200 → None
7. 临时目录注入(缓存文件路径参数化,测试不写真实文件)

注意:桥接模块的路径锚定风格(cookie/cache 文件在模块目录);缓存路径要可注入(参数或模块变量),测试隔离用 monkeypatch。

**验证**:`cd /root/character_test && uv run python test_netease_proof.py`

---

### Task 4: daemon 双向联动 + toml [netease]

**Files:**
- Modify: `/root/character_test/chiguo_daemon.py`
- Modify: `/root/character_test/chiguo_proactive.toml`
- Modify: `/root/character_test/test_netease_proof.py`(daemon 联动测试)

**toml 新段(放在 [circadian] 之后):**
```toml
[netease]
# ── v8: 听歌状态双向联动(夜间活跃反证)──
play_cache_ttl_minutes = 15     # 播放记录缓存 TTL
play_proof_window_hours = 2.0   # 播放证据时间窗(距评估时点)
sleeping_confidence_factor = 0.5  # sleeping 置信度压制系数(有播放证据时)
```

**daemon 逻辑**(evaluate() 中 Bayesian 阻塞判定之前插入;仅睡眠窗口内):

```python
# ── v8: 听歌反证(夜间活跃)──
play_proof = False
qs, qe = self.state.cooldown.quiet_window()
if _in_quiet_window(now, qs, qe):
    try:
        ncfg = self.config.get("netease", {})
        plays = netease_bridge.fetch_recent_play(
            limit=20, ttl_minutes=ncfg.get("play_cache_ttl_minutes", 15))
        if plays:
            proof_win = ncfg.get("play_proof_window_hours", 2.0)
            recent = [p for p in plays if 0 <= (now.timestamp()*1000 - p["playTime"]) <= proof_win*3600*1000]
            if recent:
                play_proof = True
                # 反向校正:窗口内播放时间点记入 active(按当日分桶)
                bucket = bucket_for(now, self.state.holiday_parser.is_holiday,
                                    self.state.holiday_parser.is_makeup_workday)
                circ_cfg = self.config.get("circadian", {})
                for p in recent:
                    dt = datetime.fromtimestamp(p["playTime"]/1000, tz=CST)
                    if _in_quiet_window(dt, qs, qe):
                        self.state.circadian.record_active(dt, circ_cfg.get("history_days", 14), bucket)
    except Exception:
        pass  # 全链路降级:网络/解析/状态损坏均不阻塞
```

- `_in_quiet_window(dt, start, end)`:跨午夜语义(qe < qs)与 can_send 一致;建议放 chiguo_circadian.py 或 daemon 私有函数(实现时选其一,测试覆盖两分支)
- **sleeping 压制**:`play_proof=True` 时,把现有 `user_state.get("confidence", 0)` 的比较值改为 `confidence * factor`(factor 来自 toml;即:生效置信度 = 原始 × factor)。同时逃生阀 sleeping_guard 分支(`confidence >= escape_valve_sleep_block`)同样用压制后的置信度。
- 生效桶快照更新:record_active 后**不立即 recompute**(每 30 分钟评估频率 + 聚合成本低,可在 play_proof 时顺带 recompute + 记录,但为保证窗口稳定,只在反证发生时 recompute)——实现时选择:反证发生时 recompute + _sync_quiet_window,否则不主动重算。**倾向:反证时 recompute**(深夜听歌 → 窗口学习即时生效)。

**测试断言清单**(test_netease_proof.py 追加;monkeypatch daemon 引用的 fetch_recent_play 与 state):
1. 窗口内 + 有 2h 内播放 → play_proof → Bayesian sleeping 阻塞被解除(can_send 保持 True)
2. 窗口内 + 无播放 → 不压制
3. 窗口外 → fetch_recent_play 不被调用(mock 计数 0)
4. 播放时间在窗口外(如 20:00 前)→ 不算反证(不压制)
5. play_proof → record_active 被调用、recompute 被调用(活跃校正生效)
6. 逃生阀 sleeping_guard:play_proof 时置信度 ≥ 0.9 × 0.5 = 0.45 → 不再 ≥ 0.9 → 放行
7. fetch 异常/None → 不崩、不压制

**验证**:`cd /root/character_test && uv run python test_netease_proof.py && uv run python test_integration.py && uv run python test_followup.py && uv run python chiguo_daemon.py --status`

---

### Task 5: 文档同步 + 全量回归

**Files:**
- Modify: `/root/character_test/doc/SYSTEM.md`、`doc/IMPROVE.md`、`doc/README.md`
- Modify: `/root/character_test/MEMORY.md`、`CLAUDE.md`、`AGENTS.md`

内容要点:
- v8:双作息(bucket_for 分桶、周五 20:00-周日 20:00 周末窗、调休/假期修正、两套独立窗口、旧状态迁移)、听歌双向联动(fetch_recent_play、窗口内拉取、sleeping 置信度 × 0.5、record_active 反向校正)、STATE_VERSION 8、[netease] 段、新测试文件 test_netease_proof.py(测试文件数 16 → 17,测试数按实际)
- 全量 17 文件回归(原 16 + test_netease_proof)

**验证**:全量 runner 链(17 个),全部 PASS。

---

### Task 6: 全面审查(子代理并行审计)+ 修复

按项目铁律派 2 路并行审计子代理:
1. **A 路 — 双作息正确性**:bucket_for 边界(调休/假期/周五周日 20:00 整点)、跨桶一天、迁移完整性、稀疏回退、_sync_quiet_window 按桶选窗与 quiet_window() 门禁链路一致性、recompute 双桶独立性与 active 合并语义
2. **B 路 — 听歌联动正确性**:fetch_recent_play 缓存/降级、窗口内才拉取、sleeping 压制数学(与 bayesian_block_conf 阈值/逃生阀 interplay)、record_active 分桶、状态持久化往返、daemon 决策链路(play_proof → 压制 → save 顺序,recompute 是否持久化)

修复审计发现 → 重跑全量 → 文档同步(若有参数/行为变更)。

**验证**:全量 17 文件 PASS + 审计报告无未修复项。
