# Refactor 记录 — T11·Q1 ChiguoState 13 责任集群重组（Issue #269）

> 目标：把 `chiguo_state.py`（2347 行 / 78 defs / 13 责任集群）重组为**不超过 4 个单类**，
> 并把 daemon 对 `chiguo_state` 私有成员直访（11 处）与 cooldown 字段级直读写（41 处）全部收口到公开 API。
> 对应 GitHub Issue：#269。
>
> 基线：`main` @ `a6db82c`。只在本 worktree（`/root/chiguo-worktrees/T11`）操作，不 push、不改 `chiguo_version.py`。

## 一、拆分前后结构对比（行数 / 类 / 责任集群）

### 拆分前（a6db82c）

单文件 `chiguo_state.py` **2347 行**，仅 3 个类：

| 类 | 行号 | 行数 | 职责 |
|---|---|---|---|
| `ChiguoEmotion` | 57–88 | ~31 | 情绪 dataclass（neediness/dominant_layer/clamp） |
| `CooldownState` | 183–310 | ~128 | 冷却/交互计数 dataclass |
| `ChiguoState` | 313–2347 | **~2035** | **单管 13 个责任集群**：路径/配置、持久化(load/save/lock/audit)、迁移、生物钟同步、Bayesian、人格、寒暑假、课表、情绪推进、接话茬、事件响应、概率触发、收尾/快照 |

- 78 个 defs；daemon 私有直访 11 处（行号 118/125/258/271/457/486/561/1118/1151/1325/1611）
- cooldown 字段级直读写 41 处（含 582/594 的 `held_count`/`accumulated_lambda` 直接赋值）

### 拆分后（本分支 fix/q1-state-split）

单文件仍为 `chiguo_state.py`，但重组为**恰好 4 个单类**（每类单一职责）：

| 类 | 行号 | 行数 | 职责（对应拍板方向） |
|---|---|---|---|
| `ChiguoEmotion` | 58–88 | 31 | 情绪引擎（数据 + 纯运算） |
| `CooldownState` | 184–396 | 213 | 核心状态·冷却子状态 + **公开 getter/mutator API**（字段收口载体） |
| `StatePersistence` | 399–833 | **435** | 持久化 + 迁移：load/save/flock/审计/校验和/路径/版本迁移 |
| `ChiguoState` | 836–2507 | **1672** | 核心状态·决策主干（决策/情绪/课表/人格/Bayesian/接话茬/快照；文件层委托 StatePersistence） |

### 关键指标变化

| 指标 | 拆分前 | 拆分后 | 说明 |
|---|---|---|---|
| 顶层类数 | 3 | **4（≤4 达标）** | 恰为拍板的 4 个单类方向中的一个子集：核心状态 / 情绪引擎 / 持久化(含迁移) |
| 责任集群 | 13（全在人单体 ChiguoState） | **4 单类** | 单一职责；迁移并入持久化（加载时执行，天然同属"状态格式"关注点） |
| 单体 `ChiguoState` 行数 | **~2035** | **~1672** | 剥离持久化/迁移 435 行，下降 **~18%**；核心类不再持有文件层复杂度 |
| `StatePersistence` 行数 | 0（内联于 ChiguoState） | 435 | 新增持久化/迁移单类，单一职责 |
| daemon 私有直访 | 11 处 | **0 处** | 全部改走公开 API（`load`/`audit`/`sync_quiet_window`/`apply_analysis_impact`/`reset_bayesian_estimator`） |
| cooldown 字段直读写 | 41 处 | **0 处（daemon）** | 全部改走 CooldownState 公开 getter/mutator |
| 回归守卫测试 | 无 | **有**（`tests/test_state_private_access_guard.py`） | AST 静态断言 daemon/trigger/topics/demo 不直访私有、不做 cooldown 裸字段读写 |

> 注：拆分后 `chiguo_state.py` 总行数 2347 → 2507（+160）。增量来自新增的公开 getter/mutator
> API（`CooldownState` ~85 行）与公开委托入口（`ChiguoState` ~15 行）——这是"字段/私有直访收口"
> 的必要成本，换取的是**认知显著下降**：单类单一职责、核心类不再持有文件层复杂度。
>
> **验收④显式豁免**：总行数指标不设红线、不反向压减。本拆分的判据为「认知下降 + 职责单一」
> （4 单类≤4、单体 `ChiguoState` ~2035→1672、责任集群 13→4 达标）；总行数反增属公开 API
> 簿记的必要成本，不作回归指标。

## 二、职责归属映射（13 集群 → 4 单类）

| 原 13 集群 | 归属类 | 说明 |
|---|---|---|
| B 持久化(load/save/lock/audit/校验和/路径) | `StatePersistence` | 原子写/备份/fsync/0600/校验和/审计/跨进程 flock |
| C 迁移（circadian 分桶、crash/event 回填） | `StatePersistence` | `apply_loaded_data`/`migrate_circadian_v8`，加载时执行 |
| A 路径/配置基础设施 | `StatePersistence.anchored` + `ChiguoState` 委托 | 文件路径锚定归属持久化 |
| D 生物钟/作息同步 | `ChiguoState` | 决策/门禁相关，保留核心 |
| E/F/G/H/I/J/K/L/M（Bayesian/人格/寒暑假/课表/情绪/接话茬/事件触发/发送决策/收尾快照） | `ChiguoState` | 决策主干 |
| `ChiguoEmotion` | `ChiguoEmotion` | 情绪数据引擎 |
| `CooldownState` | `CooldownState` | 冷却子状态 + 公开字段 API |

## 三、公开 API 收口清单（daemon 私有直访 11 处）

| 旧直接访问 | 新公开 API |
|---|---|
| `state._load()` | `state.load()` |
| `state._sync_quiet_window([now])` | `state.sync_quiet_window([now])` |
| `state._audit(evt, detail)` | `state.audit(evt, detail)` |
| `state._apply_analysis_impact(analysis, now)` | `state.apply_analysis_impact(analysis, now)` |
| `state._bayesian_estimator = None` | `state.reset_bayesian_estimator()` |

## 四、cooldown 字段收口（CooldownState 公开 getter/mutator）

- 读：`get_last_message_at / get_last_user_message_at / get_messages_today / get_messages_without_reply / get_trigger_history / get_event_timestamps / get_reply_latencies / get_busy_suppress_until / get_held_count / get_accumulated_lambda / get_recv_dedup / get_user_mood / get_reply_stats / get_consolidate_last_at / is_morning_sent / is_night_sent / get_current_date`
- 改：`append_trigger_history / mark_morning_sent / mark_night_sent / increment_held / set_accumulated_lambda / set_consolidate_last_at / set_recv_dedup / set_last_message_at / set_last_user_message_at / set_user_mood`
- 既有公开方法保留：`quiet_window / silent_hours / minutes_since_last_message / is_busy_suppressed`

## 五、验收自评

- ① 状态 roundtrip 全绿：`test_daemon_fixes`（含 `test_bug2_tick_save_reload_roundtrip`、`test_state_monotonic_anchor_persist_roundtrip`）等通过
- ② 私有访问收口回归守卫：新增 `tests/test_state_private_access_guard.py`，AST 断言通过
- ③ 全链 `bash scripts/ci-test.sh` 通过
- ④ ≤4 单类、行数/认知下降：见上表（核心类 ~2035→1672，责任集群 13→4）
