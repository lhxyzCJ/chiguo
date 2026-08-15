# Claude Code 开发规则 — Chiguo Proactive Message System

> 定位：纯 Claude Code 开发规则（构建、测试、约定、gotchas）。版本 v1.15。
> 架构（文件依赖树、evaluate() 决策流、5 维情绪引擎、触发系统、话题注入、消息 composer、Ship of Theseus 配置参数、决策输出 schema、运行时数据文件归属）详见 **doc/SYSTEM.md**。
> agent 后端集成（LLM Host、send/reply 侧、SUN2.md 人格宪法、skill 文件边界）详见 **doc/AGENT_INTEGRATION.md**。
> 测试无 pytest，独立 runner、纯 Python stdlib；测试清单唯一权威为 scripts/ci-test.sh（计数以它为准）。
> **Iron law**: decision/generation separation. Daemon outputs JSON. agent backend generates messages (Phase 4).

---

## 1. 构建与测试 (Build & Test)

```bash
# 全量测试：scripts/ci-test.sh 独立 runner（任一失败退出非零；清单唯一权威入口，计数以它为准）
bash scripts/ci-test.sh

# 单文件
python3 tests/test_monitor.py

# 决策引擎（单次 eval → JSON 到 stdout）
python3 chiguo_daemon.py

# 交互式 demo（无 LLM，仅模板）
python3 chiguo_demo.py

# 监控 / 健康
python3 chiguo_daemon.py --stats 7      # 7 日统计
python3 chiguo_daemon.py --alerts       # 异常检测
python3 chiguo_daemon.py --monitor      # 完整报告
python3 chiguo_daemon.py --stats        # 结构化统计
python3 chiguo_monitor.py --health      # 系统健康检查
python3 chiguo_monitor.py --alerts      # 告警列表
python3 chiguo_monitor.py --alerts-all  # 含已解决

# 维护
python3 update_holidays.py 2027 --solar --force  # 生成节假日数据

# 日志轮转（daemon 初始化时自动调用）
python3 chiguo_rotation.py --force
```

**No build step. No pip install.** Python 3.14-only（PEP 758 无括号 `except E1, E2:`、延迟注解——禁止加 `from __future__ import annotations`）。可选依赖：`openpyxl`（schedule）、`mem0ai` + `ollama`（记忆层）。`memory/mem0_backend.py`（`Mem0Backend`）在 `available` 探测内**惰性导入** `mem0`；缺失时 `available=False` 优雅降级，记忆查询软降级返回空，daemon 不会因缺 mem0 崩溃。

### daemon CLI（35 个参数）
`--ack --alerts --alerts-all --analysis --analysis-file --anniversary --attention --break --compact --consolidate --conversation --conversation-days --error --export --fallback --health --intensity --loop --memory-search --monitor --recv-id --record-send --rotate --schedule-change --schedule-recall --send-result --send-status --stats --status --text --trigger --tune --user-msg --user-msg-file --version`

各参数语义见 doc/SYSTEM.md CLI 章节。

---

## 2. 架构与集成（指向权威文档）

- 文件依赖树、evaluate() 决策流、5 维情绪引擎、触发系统、话题注入、消息 composer、配置参数（Ship of Theseus）、决策输出 schema → **doc/SYSTEM.md**
- LLM Host Integration（agent runner 抽象、send/reply 侧、SUN2.md 人格宪法、skill 文件安全边界）→ **doc/AGENT_INTEGRATION.md**

---

## 3. 关键模式与约定 (Key Patterns & Conventions)

### 状态持久化
- **原子写**：统一走共享 `chiguo_atomic.atomic_write`（`.tmp` 文件 → 校验 JSON → `os.replace()` 覆盖目标；0600 一步到位可选）
- **跨进程写锁**：统一走共享 `chiguo_locks`（fcntl 可重入锁；`chiguo_state`/`agent_health` 共用一份实现）
- **备份**：覆盖前 `.bak` 副本
- **校验和**：`_checksum` 字段 SHA256（校验但不因 mismatch 拒绝）
- **审计日志**：`chiguo_state_audit.jsonl` 记录损坏/恢复事件
- **tick_seq**：单调递增 tick 计数器（写盘前 CAS 保护，磁盘领先则内存跟随），用于检测遗漏 tick
- **PID 锁**：`chiguo_loop.pid` 防止 `--loop` 模式双启动

### 流式 JSONL 解析
- `_iter_decisions()` 逐行读取 — O(n) 时间、O(1) 内存
- 缺失文件/空文件/损坏行不崩溃
- `DecisionIndex` 构建字节偏移索引，按 trigger/date/action O(1) 查询

### 配置热重载（--loop 常驻态）
- `_maybe_reload_config()` 每次 `evaluate()` 前检查 TOML mtime（仅 `--loop` 模式有意义；cron 每次起新进程）
- mtime 变化且 TOML 合法时，替换 config 引用并重建 config 派生组件，重建集合（Q19 补全）：
  - `ChiguoState.reload_config()`：重建 personality 初始基线（`_personality_initial_baseline`）、holiday_parser（重读 `holidays.json`）、cooldown 静默窗口（`_sync_quiet_window`，置信度达标用学习窗口否则回退新 config 默认）
  - daemon 侧重建 `NeteaseService` / `TopicPicker` / `MessageComposer`（策略/配额/模板参数可能被改）
  - `_bayesian_estimator` 重置为 None（惰性重初始化）
  - runtime 持久化状态（情绪/人格演变/cooldown 字段/生物钟学习）不动
- TOML 语法错误/读取失败 → 保留旧配置，打 stderr 告警继续运行

### 测试隔离
- `tests/test_monitor.py` 用 `tempfile.TemporaryDirectory`；其余为纯函数测试，无共享状态
- 无测试框架：裸 `assert` + `if __name__ == "__main__"` runner
- `sys.path.insert(0, ...)` 找兄弟模块
- `random.seed(42)` 保证集成测试确定性
- 固定时间用显式 `datetime(..., tzinfo=CST)`

### 命名约定
- 测试函数 `test_` 前缀，成功打印 `"  OK test_name"`
- dataclass 字段有默认值；可变默认值用 `field(default_factory=...)`
- 私有方法 `_` 前缀
- CLI：argparse 模式，JSON 到 stdout、诊断到 stderr

---

## 4. 运行时文件（隐私数据——登录态/会话日志/个人数据永不入库，仅本机保留）

Note: privacy data (WeChat login state, conversation logs `chiguo_messages.jsonl`/`chiguo_decisions.jsonl`, `data/` personal files) is **never committed** — kept locally only, history rewritten (2026-08-02 security pass).

| File | Writer | Purpose |
|------|--------|---------|
| `chiguo_state.json` | chiguo_state.py | Persistent emotion state + last_tick + tick_seq + mono_anchor/wall_anchor (monotonic anchor pair) + checksum |
| `chiguo_state.json.bak` | chiguo_state.py | Pre-write backup |
| `chiguo_state.json.tmp` | chiguo_state.py | Atomic write staging |
| `chiguo_decisions.jsonl` | chiguo_daemon.py | Append-only decision log |
| `chiguo_messages.jsonl` | chiguo_daemon.py | Human-readable conversation log (v5) |
| `chiguo_state_audit.jsonl` | chiguo_state.py | Corruption/recovery audit trail (v5) |
| `chiguo_alerts.json` | chiguo_monitor.py | AlertManager persisted state |
| `chiguo_loop.pid` | chiguo_daemon.py | PID lock file |
| `schedule_cache.json` | schedule/parser.py | Parsed xskb.xlsx cache |
| `schedule_overrides.json` | schedule/override_store.py | Manual schedule overrides (atomic 0600) |
| `schedule_plan.json` | schedule/plan_store.py | Daily plan, replan-generated (atomic 0600) |
| `schedule_clarify.json` | wechat-bridge/bridge.mjs | Clarify record (writeClarify atomic 0600, 6h expiry) |
| `anniversaries.json` | schedule/anniversary.py | Anniversary records (countdown deprecated, migrated to reminder) |
| `break_state.json` | chiguo_daemon.py | Vacation override (written by --break CLI) |
| `holidays.json` | update_holidays.py | Override holiday data for future years |
| `solar_terms.json` | update_holidays.py | 某年节气快照（`--solar` 生成物；运行期不被读取，节气单一事实源见 §5） |
| `netease/netease_cache.json` | netease/bridge.py | Daily song recommendations cache |
| `netease/netease_cookie.txt` | netease/bridge.py | Netease API auth cookie (chmod 600) |
| `netease/recent_play_cache.json` | netease/bridge.py | Recent-play cache (v8, atomic write, 15-min TTL) |
| `netease/netease_health.json` | netease/service.py | Netease health/quota state (v9, atomic write) |

---

## 5. 已知设计决策（非 bug）

- **M3**: Config hot-reload resets Bayesian estimator → conservative, prevents stale config
- **M5**: Ritual trigger weights (2.5-3.0) dominate emotion weights (0.01-0.9) → by design; tune `ritual_weight_scale` to adjust
- **Hawkes dt==0 exclusion**: Events at `now` are the current event itself, not historical excitation — intentional
- **Lunar holiday estimation in update_holidays.py**: ~11 day/year drift for non-2027 years. Requires manual update of `KNOWN_HOLIDAYS` when State Council notice published
- **Solar terms single source (#259, Q5/Q17)**: 节气日期唯一权威入口 = `update_holidays.get_solar_terms_for(year)`。2026/2027 为天文权威校准精确表（lunar_python 北京时间计算 + HKO 交叉复核）；其余年份按 `~6h/year`（≈0.25 天/年）线性估算，跨年不用再每年代码级维护 `SOLAR_TERMS_2027`。`solar_terms.py` 为按年动态消费者。

---

## 6. 陷阱与 Gotchas

1. **File already read**: Edit tool requires Read first. When Read hook blocks (claude-mem down), use Bash/python for edits.
2. **Timezone**: Always use `CST = timezone(timedelta(hours=8))`. Naive datetimes get auto-completed via `_parse_tz()`.
3. **TOML types**: `rate_energy_min = 5.0` must be float. Comments must match values.
4. **mem0 惰性导入 + 60s 节流**: mem0 缺失时 `available=False` 优雅降级（记忆查询返回空；测试 mock/skip，`CHIGUO_MEM0_DISABLED=1` 确定性不可用）。故障驱动自愈：置不可用后至少间隔 `_RETRY_SECONDS=60` 才重新探测（memory/mem0_backend.py）。
5. **xskb.xlsx 缺失 / schedule 解析失败**: `refresh_schedule_cache` 返回 available=False → 按空闲处理（availability=1.0，tier=unavailable）；解析失败（`openpyxl` 缺失/文件损坏）保留旧缓存、不覆盖落盘（schedule/parser.py）。
6. **Test order**: Integration tests need `chiguo_proactive.toml` in CWD. Run from the repo root.
7. **Ritual weight scale**: `evaluate_triggers()` multiplies all ritual weights by `ritual_weight_scale`. Set to 0.3 to balance with emotion weights.
8. **character_rules ⑦ vs condensed**: Full rules (guidance variable) missing 喵/嘻嘻. Condensed version (context dict) has them. Both now fixed (2026-07-02).
9. **Bayesian normalization**: `update_from_label()` normalizes cached P(obs|state) to sum=1.0. Uncached values default to 0.05 but are not in the normalization group.
10. **time-bomb 测试不修**: 部分测试锚定真实日期/真实时钟（如 tests/test_topics.py 的 on_break 用 `datetime.now()` 与 semester_end 比较，测试固定 semester_end 为过去日期取确定分支）。这类"时间炸弹"在真实日期越过时可能失效——约定是不预先修补，等其失败时再更新锚定。

---

## 7. 安全边界 (Security Boundary)

**R/W**: `data/mem0/` mem0 store (embedded qdrant + history.db) — accessed via `memory/mem0_backend.py` (`Mem0Backend`, default `[memory].backend=mem0`). LLM key: `~/.pi/agent/auth.json` opencode-go (never committed).

---

## 8. 改动后检查清单 (Post-Change Checklist)

After ANY code change:
1. Run affected test files
2. Run full suite if touching math/state/daemon
3. Update `doc/SYSTEM.md` if architecture/CLI/config changed
