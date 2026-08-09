# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
## 维护规则
## 注意，在你开始修复前一定记得做个计划或者todolist，然后尽量多开子代理，让事情做起来更高效，不要吝啬token消耗,做完事情后也要多开代理进行自我审计，这是铁律。
## 所有子代理（Agent/Workflow）开工前必须继承主模型，不得通过 model 参数覆盖（如 opus/haiku/fable），除非任务明确需要专用模型。
## 工程原则
1. 不保留向后兼容，过时路径直接删，别加兼容层
2. 选满足当前需求的最简实现，别搞多余抽象配置
3. 先跑通最小可用版本，再叠新功能，别换现有能跑的代码
4. 组件模块化，业务关注点分离
5. 优先用成熟维护的第三方库，没理由别重复造轮子
6. 先查现有项目依赖的能力，再考虑加包或自研
7. 架构决策看长期，别用临时凑合用的过渡方案
8. 参考成熟产品的验证方案，别从零发明
## Git 工作流（代码改动）
- 代码改动（refactor:/fix:/feat:）一律走分支 + PR：分支名 `simplify/<issue-N>-<slug>`；每分支对应一个 GitHub Issue（`gh issue create`），PR 正文 `Closes #N`；出口条件 = CI 全链绿 + 子代理自审 + 用户批准 → squash merge。
- docs:/chore: 小改动可直接 main（CI 仍自动验证）。
- 简化单元跟踪：从审查报告（~/chiguo-meta/audit/）拆出的每单元建 Issue，标题 `[simplify] <模块>: <改动>`，正文含文件清单、改动内容、删行预估、依赖单元。
每次修改代码后必须：
1. 更新相关文档（`doc/SYSTEM.md`、`doc/README.md` 中受影响的章节）
## Build & Test

```bash
# Run all tests (Python 3.14+ required) — 38 py + 10 script tests
bash scripts/ci-test.sh   # 本地与 GitHub Actions ci.yml 同一入口
```

**Python 3.14+ required** (via uv). Setup:

```bash
# One-time: create venv + install deps
uv venv
uv pip install openpyxl

# Run tests or scripts
uv run python tests/test_chiguo_math.py
uv run python chiguo_daemon.py
```

Key 3.14 features used:
- PEP 758: bracketless `except E1, E2:` (no parentheses for multi-exception without `as`)
- PEP 649/749: deferred annotations by default — `from __future__ import annotations` removed
- `X | None` union syntax instead of `Optional[X]` (3.10+)
- `random.choices()` instead of manual weighted random loops

No build step. Minimal dependencies beyond Python stdlib (plus `tomllib` for Python ≥3.11). mem0 (mem0ai) is a required dependency — the `memory/` backend degrades gracefully when the LLM key / ollama are unavailable (`available=False`, queries return empty).

## Architecture

**Decision/generation separation** — the core design principle. `chiguo_daemon.py` is a zero-LLM math engine that evaluates state and outputs structured JSON. Message generation happens externally via the **agent backend** (Phase 4 host; v1.8 抽象：默认 pi-agent，`[host].runner = command` 可替换为任意 CLI agent，见下), which reads that JSON, generates text per `personality/SUN2.md`, and sends via wechat-bridge.

**agent backend integration** (see `doc/AGENT_INTEGRATION.md`, Phase 4):
- **v1.8 agent 抽象；v1.9 记忆后端**：`[host].runner` — `agent`（默认，pi-agent 二进制）| `command`（任意 CLI agent，`[host].agent_command` 指定；统一契约 `--prompt <完整提示词> --mode <mode>`，stdout JSON 或 NDJSON；RPC 常驻仅 agent 模式）。**记忆后端抽象**：`[memory].backend` — `mem0`（默认，mem0ai 记忆层：LLM 事实提取写入 + ollama 本地向量检索 + qdrant 嵌入式存储）/ `module.path.ClassName` 自定义类（`memory/` 包：MemoryBackend 基类 + Mem0Backend + create_backend 工厂；`memory_bridge.py` 兼容门面）
- Send side: system crontab `*/15 * * * * scripts/chiguo-tick.sh` — runs `chiguo_daemon.py --compact` with zero model calls; idle → silent exit, send → `scripts/agent-run.mjs` (session `chiguo-send`) generates message per SUN2.md → curl `[host].wechat_bridge_url` → `--record-send` writes back；pi 生成失败 → `chiguo_composer.py` 模板池兜底直出文本（v1.10 A8：成功发送 + fallback 标记，composer 也失败才 fail）
- Reply side: `wechat-bridge/bridge.mjs` — deterministic `--user-msg` on arrival → special-command detection (`command-detect.mjs`: anniversary/break/schedule rules, no pi; schedule-center CLI 子命令 `--attention`/`--schedule-recall`/`--schedule-change` + `python -m schedule.replan --check`/`schedule.holiday`) → otherwise `askPi` (agent-run `--analysis-mode`: one call does emotion analysis JSON + reply, session `chiguo-main`, in-process TurnQueue serializes) → `--user-msg --analysis` upgrade (daemon recv_dedup, no double-count)
- Sessions: reply=`chiguo-main`, proactive send=`chiguo-send` — separate sessions eliminate cross-process concurrent turns
- Environment: `scripts/install_agent.sh` bootstraps pi (settings/json5, provider auth via toml [host].provider (default opencode-go), crontab)；记忆层 mem0 由 `uv sync` 安装（必需依赖；qdrant 嵌入式 data/mem0/ + ollama qwen3-embedding）
- Service management: `scripts/service.sh`（统一管理 ollama + wechat-bridge：`autostart`=systemd 开机自启（`/etc/systemd/system/chiguo-bridge.service` + enable --now）/ `temp`=临时启动不注册自启（nohup + pidfile）/ `status`/`stop`/`uninstall`；两模式互斥接管防 18790 端口冲突；`--dry-run` 支持；deploy.sh 第 5 步接 autostart）

**v4 (2026-06-27)** adds: Bayesian user state inference, multi-dimensional personality (Big Five + character-specific), Ebbinghaus forgetting curves, message composition system (Intent × Cue × Vibe), probability accumulation with anxiety blocking, personality adaptation, and dynamic sleep scheduling.

**v7 (2026-07-31)** adds: circadian sleep-window learning (生物钟学习 — learns the user's quiet hours from reply times, applies only when confidence ≥ 0.5, falls back to config default 0-8) and the follow_up trigger (接话茬 — continues unfinished topics from `--analysis` topic ingestion, bell-shaped age weighting, single attempt, memory fallback). STATE_VERSION 6→7.

**v8 (2026-07-31)** adds: dual-schedule circadian learning (双作息 — weekday/weekend buckets with separate learned windows, `bucket_for()` handles 调休上班日→weekday / 节假日→weekend / Fri 20:00+ & Sat & Sun before 20:00→weekend; v7 state auto-migrates by backfilling buckets) and NetEase play proof (听歌双向联动 — `netease.bridge` 的 `NeteaseBridge.fetch_recent_play()` fetches recent plays only inside the active quiet window, play within the 2h proof window suppresses Bayesian sleeping confidence ×0.5 and records active times back into the circadian tracker). STATE_VERSION 7→8.

**v1.10 (2026-08-09)** adds: 外部对比优化 9 项（STATE_VERSION 不变仍为 10）— A1 弹性衰减（`elastic_recover`：effective_hl = half_life/(1+|gap|/baseline)，偏离越远回弹越快，`[emotion].elastic_baseline`）+ A2 情绪交互矩阵（tick 后跨维度联动，`[emotion].interaction_*` 默认 1.0=关闭）+ A3 日程乘数×抖动（情绪类，仪式类豁免）+ A4 三段激活（<min_activation 沉默 / ≥must_send_activation 必选 must_send）+ A5 未回复退场状态机（backing_off 禁情绪类 / silent 全禁发）+ A6 repeat 阻尼泛化（全类型 ×0.6^min(n,3)）+ A8 生成失败确定性回退（composer 兜底 CLI）+ A9 内容级防复读（3-gram Jaccard 弃用候选）+ A10 回复饱和阻尼（30 分钟窗口 ×0.5^min(n,3)）。

```
chiguo_daemon.py (DecisionEngine)
  ├─ chiguo_state.py     → 5-dimension emotion engine + 8-dim personality + Bayesian inference + schedule + holidays + memory
  ├─ chiguo_trigger.py   → sigmoid-weighted random trigger selection (13 trigger types incl. reflect, longing, follow_up)
  ├─ chiguo_topics.py    → 8-source topic injection with Ebbinghaus-weighted memory + personality modulation
  ├─ chiguo_composer.py  → Intent × Cue × Vibe three-layer message composition (v4)
  ├─ chiguo_math.py      → pure functions: sigmoid, half-life decay, Hawkes, longing accumulation (v4)
  ├─ chiguo_personality.py → Big Five + character traits (8 dimensions) (v4)
  ├─ chiguo_bayesian.py  → Bayesian user state estimation (6 states, online learning) (v4)
  ├─ chiguo_circadian.py → circadian sleep-window learning (dual-bucket: weekday/weekend independent windows + active-time merging) (v7, v8 dual-bucket)
  ├─ netease/bridge.py  → NetEase API bridge 数据面 (NeteaseBridge 实例化: fetch_recent_play/fetch_daily_songs/QR 登录;运行时文件锚定 netease/ 子目录) (v8→重构)
  ├─ netease/service.py → NetEase strategy layer 策略层 (v9, DI: health probe/degradation chain/peek-consume quota/music topic/fetch_play_proof 单入口)
  ├─ schedule/ 包   → 课表数据面 parser.py / 纯解析 parsing.py / 策略 query.py + 节假日 holiday.py + 纪念日 anniversary.py + 覆盖/计划/澄清 override_store.py/plan_store.py/api.py + 检索与安排 sources.py/day_plan.py/resolve_when.py/attention.py/recall.py + 确认 confirm.py + 复盘 replan.py
  ├─ solar_terms.py      → 24 solar terms
  ├─ memory/ 包        → 记忆后端抽象：MemoryBackend 基类 + Mem0Backend + create_backend 工厂（Ebbinghaus 包装在基类；memory_bridge.py 兼容门面）
  ├─ chiguo_watchdog.py  → daemon health checks (disk, memory, tick freshness)
  ├─ chiguo_rotation.py  → monthly log rotation → archive/
  ├─ chiguo_envcheck.py  → read-only env readiness check (exit 0/1/2)
  ├─ chiguo_version.py   → project version single source (VERSION="1.10", MINOR +1 per round: 1.9→1.10→1.11, not decimal addition)
  └─ chiguo_monitor.py   → streaming JSONL analytics (stats/alerts/health)

  Output: chiguo_decisions.jsonl (append-only structured log)
  State:  chiguo_state.json (atomic write: .tmp → os.replace)
```

**Config**: `chiguo_proactive.toml` — all parameters (368 lines). Legacy host section from Task 14 (superseded by `[host]`; only `wechat_recipient` still read). `DecisionEngine._maybe_reload_config()` detects mtime changes and hot-reloads in `--loop` mode without restart.

**Version**: `chiguo_version.py` is the single source (`VERSION="1.10"`); +0.1 per completed round. Decision JSON/`--version`/envcheck/monitor carry the version; state file `_version` is the schema number (STATE_VERSION=10), unrelated to the project version.

**5 emotion dimensions** with half-life decay toward equilibrium: loneliness (→100, 40h), affection (→0, 500h), anxiety (→100, 30h), energy (→100, 8h), tsundere_index (10-95, computed). User replies apply half-life decay drops (loneliness 0.35h, anxiety 0.5h). v1.10: 弹性衰减（`elastic_recover`：effective_hl = half_life/(1+|target-current|/baseline)，偏离越远回弹越快；loneliness/anxiety/affection/energy 四处推进改调）+ 情绪交互矩阵（tick 后 `apply_interaction_matrix` 一次：affection>60→anxiety 恢复加速 / energy<30→loneliness 恢复加速 / anxiety>70→energy 恢复减速，`[emotion].interaction_*` 默认 1.0=关闭恒等）+ 回复饱和阻尼（30 分钟窗口同向回复计数，加成 ×0.5^min(n,3)，`[cooldown].drop_damp_*`）。

**Trigger selection**: sigmoid-weighted, not hard-threshold. `chiguo_trigger.py` computes weights for 13 trigger types (incl. v7 follow_up), then weighted random choice. No priority-based cascading — every eligible trigger has non-zero probability. v1.10 三段：A3 日程乘数×抖动（上课 0.3/空闲 free_multiplier 1.2/半忙 0.6 × uniform(0.8,1.2)，仪式类豁免）→ A6 repeat 阻尼（全类型 ×0.6^min(n,3)）→ A4 三段激活（情绪类权重和 < min_activation 0.08 沉默 / ≥ must_send_activation 0.5 必选 must_send）；A5 未回复退场状态机（backoff_start=3 backing_off 禁情绪类、backoff_silent=5 silent 全禁发，escape_valve 豁免）。

**Topic injection**: When `lonely_low` or `lonely_mid` triggers fire, 70% chance to inject a conversation topic from 8 weighted sources (schedule, memory, weather, anniversary, solar terms, preference followup, general, netease). 3 consecutive lonely triggers → forced topic injection.

**CLI convention**: all entry points follow argparse patterns. Output is JSON to stdout, diagnostics to stderr.

## Runtime Files

All auto-generated at first run, all in `.gitignore`:

| File | Purpose |
|------|---------|
| `chiguo_state.json` | Persistent emotion state + `last_tick` timestamp |
| `chiguo_decisions.jsonl` | Append-only decision log (one JSON object per line) |
| `schedule_cache.json` | Parsed class schedule cache (cache_version=2) |
| `holidays.json` | Chinese holiday data (regenerated by `update_holidays.py`, State Council schedule) |
| `anniversaries.json` | Anniversary records |
| `schedule_overrides.json` | Manual schedule overrides (schedule/override_store.py, atomic 0600) |
| `schedule_plan.json` | Daily plan (schedule/plan_store.py, atomic 0600, replan-generated) |
| `schedule_clarify.json` | Clarify record (wechat-bridge/bridge.mjs writeClarify, atomic 0600, 6h expiry) |
| `break_state.json` | Vacation override state (written by `--break` CLI) |
| `chiguo_state_audit.jsonl` | State corruption/recovery/checksum audit log |
| `chiguo_watchdog_state.json` | Watchdog persisted state (`stall_since` tick freshness) |
| `recent_play_cache.json` | NetEase recent-play cache (v8, atomic write, 15-min TTL) |

## Key Patterns

- **State persistence**: atomic `tmp → os.replace` for corruption resistance. On load, falls back to `.tmp` if main file missing; deletes and recreates if JSON is corrupted.
- **Streaming parser**: `chiguo_monitor.py._iter_decisions()` reads JSONL line-by-line (per-line O(1) memory, missing/empty/corrupted files handled without crashing); aggregation sequences (`emotion_series`/`reply_events`/`daily_counts`) grow linearly with the number of entries in the window.
- **Emotion trends**: first-half vs second-half mean comparison; no heavy regression needed.
- **Reply rate estimation**: inferred from `messages_without_reply` deltas between consecutive sends (no explicit reply tracking in logs).
- **Config hot-reload**: `_maybe_reload_config()` checks toml mtime before each `evaluate()` call. Only matters for `--loop` mode; cron spawns fresh processes.
- **Test isolation**: all tests use `tempfile.TemporaryDirectory` for state/log/config files. No shared state between tests. `tests/test_integration.py` injects `_base_dir` into a temp dir so it never touches real runtime files (state/break/log). Note: all 36 py test runners (+ 10 script tests) exit non-zero on assertion failure (use `$?` or `&&` chaining to detect regressions).

## 安全边界

**安全边界**：mem0 记忆库位于 `data/mem0/`（qdrant 嵌入式 + history.db，gitignore），访问经 `memory/` 包（默认 `Mem0Backend`；`memory_bridge.py` 为兼容门面）；LLM key 读 `~/.pi/agent/auth.json` 的 opencode-go 条目，不进 git。


