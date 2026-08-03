# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
## 维护规则
## 注意，在你开始修复前一定记得做个计划或者todolist，然后尽量多开子代理，让事情做起来更高效，不要吝啬token消耗,做完事情后也要多开代理进行自我审计，这是铁律。
## 所有子代理（Agent/Workflow）开工前必须继承主模型，不得通过 model 参数覆盖（如 opus/haiku/fable），除非任务明确需要专用模型。
## 修改代码或开 agents 时，如需要调用 opus 模型，必须立即中断并提示用户授权，不得擅自使用。
每次修改代码后必须：
1. 更新相关文档（`doc/SYSTEM.md`、`doc/README.md` 中受影响的章节）
## Build & Test

```bash
# Run all tests (Python 3.14+ required) — 35 py + 9 script tests
node tests/test_pi_run.mjs && node tests/test_bridge_askpi.mjs && node tests/test_bridge_cmd.mjs && node tests/test_bridge_health.mjs && node tests/test_bridge_schedule.mjs && \
bash tests/test_install_pi.sh && bash tests/test_wechat_bridge.sh && bash tests/test_netease_api.sh && bash tests/test_tick_health.sh && \
uv run python tests/test_chiguo_math.py && uv run python tests/test_holiday_parser.py && \
uv run python tests/test_schedule_parser.py && \
uv run python tests/test_integration.py && uv run python tests/test_monitor.py && \
uv run python tests/test_eventbus.py && uv run python tests/test_personality.py && \
uv run python tests/test_bayesian.py && uv run python tests/test_composer.py && \
uv run python tests/test_ebbinghaus.py && uv run python tests/test_longing.py && \
uv run python tests/test_escape_valve.py && uv run python tests/test_feedback.py && \
uv run python tests/test_trigger.py && uv run python tests/test_topics.py && \
uv run python tests/test_circadian.py && uv run python tests/test_followup.py && \
uv run python tests/test_netease_proof.py && uv run python tests/test_netease_service.py && \
uv run python tests/test_envcheck.py && uv run python tests/test_composer_trade.py && \
uv run python tests/test_personality_init.py && uv run python tests/test_toml_binding.py && \
uv run python tests/test_adapt_personality.py && uv run python tests/test_pi_health.py && \
uv run python tests/test_anniversary.py && uv run python tests/test_schedule_override.py && \
uv run python tests/test_day_plan.py && uv run python tests/test_recall.py && \
uv run python tests/test_attention_tiers.py && uv run python tests/test_availability.py && \
uv run python tests/test_trigger_scale.py && uv run python tests/test_isolation.py && \
uv run python tests/test_schedule_plan.py && uv run python tests/test_schedule_cli.py   # full suite (35 py + 9 script tests)

# Or individually
uv run python tests/test_monitor.py

# Decision engine (single evaluation, prints JSON to stdout)
uv run python chiguo_daemon.py

# Monitor
uv run python chiguo_daemon.py --stats        # 7-day stats
uv run python chiguo_daemon.py --alerts       # anomaly detection
uv run python chiguo_daemon.py --monitor      # full report
uv run python chiguo_monitor.py --summary     # human-readable summary
```

**Python 3.14+ required** (via uv). Setup:

```bash
# One-time: create venv + install deps
uv venv
uv pip install openpyxl lancedb

# Run tests or scripts
uv run python tests/test_chiguo_math.py
uv run python chiguo_daemon.py
```

Key 3.14 features used:
- PEP 758: bracketless `except E1, E2:` (no parentheses for multi-exception without `as`)
- PEP 649/749: deferred annotations by default — `from __future__ import annotations` removed
- `X | None` union syntax instead of `Optional[X]` (3.10+)
- `random.choices()` instead of manual weighted random loops

No build step. No dependencies beyond Python stdlib (plus `tomllib` for Python ≥3.11). LanceDB integration is optional — `memory_bridge.py` gracefully degrades when LanceDB is unavailable.

## Architecture

**Decision/generation separation** — the core design principle. `chiguo_daemon.py` is a zero-LLM math engine that evaluates state and outputs structured JSON. Message generation happens externally via **pi-agent** (Phase 4 host), which reads that JSON, generates text per `personality/SUN2.md`, and sends via wechat-bridge.

**pi-agent integration** (see `doc/PI_INTEGRATION.md`, Phase 4):
- Send side: system crontab `*/15 * * * * scripts/chiguo-tick.sh` — runs `chiguo_daemon.py --compact` with zero model calls; idle → silent exit, send → `scripts/pi-run.mjs` (session `chiguo-send`) generates message per SUN2.md → curl `[host].wechat_bridge_url` → `--record-send` writes back
- Reply side: `wechat-bridge/bridge.mjs` — deterministic `--user-msg` on arrival → special-command detection (`command-detect.mjs`: anniversary/break/schedule rules, no pi; schedule-center CLI 子命令 `--attention`/`--schedule-recall`/`--schedule-change` + `python -m schedule.replan --check`/`schedule.holiday`) → otherwise `askPi` (pi-run `--analysis-mode`: one call does emotion analysis JSON + reply, session `chiguo-main`, in-process TurnQueue serializes) → `--user-msg --analysis` upgrade (daemon recv_dedup, no double-count)
- Sessions: reply=`chiguo-main`, proactive send=`chiguo-send` — separate sessions eliminate cross-process concurrent turns
- Environment: `scripts/install_pi.sh` bootstraps pi (memory-lancedb-pro extension, settings/json5, ollama embedding, provider auth via toml [host].provider (default opencode-go), crontab)
- Service management: `scripts/service.sh`（统一管理 ollama + wechat-bridge：`autostart`=systemd 开机自启（`/etc/systemd/system/chiguo-bridge.service` + enable --now）/ `temp`=临时启动不注册自启（nohup + pidfile）/ `status`/`stop`/`uninstall`；两模式互斥接管防 18790 端口冲突；`--dry-run` 支持；deploy.sh 第 5 步接 autostart）

**v4 (2026-06-27)** adds: Bayesian user state inference, multi-dimensional personality (Big Five + character-specific), Ebbinghaus forgetting curves, message composition system (Intent × Cue × Vibe), probability accumulation with anxiety blocking, EventBus decoupling, personality adaptation, and dynamic sleep scheduling.

**v7 (2026-07-31)** adds: circadian sleep-window learning (生物钟学习 — learns the user's quiet hours from reply times, applies only when confidence ≥ 0.5, falls back to config default 0-8) and the follow_up trigger (接话茬 — continues unfinished topics from `--analysis` topic ingestion, bell-shaped age weighting, single attempt, memory fallback). STATE_VERSION 6→7.

**v8 (2026-07-31)** adds: dual-schedule circadian learning (双作息 — weekday/weekend buckets with separate learned windows, `bucket_for()` handles 调休上班日→weekday / 节假日→weekend / Fri 20:00+ & Sat & Sun before 20:00→weekend; v7 state auto-migrates by backfilling buckets) and NetEase play proof (听歌双向联动 — `netease.bridge` 的 `NeteaseBridge.fetch_recent_play()` fetches recent plays only inside the active quiet window, play within the 2h proof window suppresses Bayesian sleeping confidence ×0.5 and records active times back into the circadian tracker). STATE_VERSION 7→8.

```
chiguo_daemon.py (DecisionEngine)
  ├─ chiguo_state.py     → 5-dimension emotion engine + 8-dim personality + Bayesian inference + schedule + holidays + memory
  ├─ chiguo_trigger.py   → sigmoid-weighted random trigger selection (13 trigger types incl. reflect, longing, follow_up)
  ├─ chiguo_topics.py    → 8-source topic injection with Ebbinghaus-weighted memory + personality modulation
  ├─ chiguo_composer.py  → Intent × Cue × Vibe three-layer message composition (v4)
  ├─ chiguo_math.py      → pure functions: sigmoid, half-life decay, Hawkes, longing accumulation (v4)
  ├─ chiguo_personality.py → Big Five + character traits (8 dimensions) (v4)
  ├─ chiguo_bayesian.py  → Bayesian user state estimation (6 states, online learning) (v4)
  ├─ chiguo_eventbus.py  → lightweight pub/sub event bus (v4)
  ├─ chiguo_circadian.py → circadian sleep-window learning (dual-bucket: weekday/weekend independent windows + active-time merging) (v7, v8 dual-bucket)
  ├─ netease/bridge.py  → NetEase API bridge 数据面 (NeteaseBridge 实例化: fetch_recent_play/fetch_daily_songs/QR 登录;运行时文件锚定 netease/ 子目录) (v8→重构)
  ├─ netease/service.py → NetEase strategy layer 策略层 (v9, DI: health probe/degradation chain/peek-consume quota/music topic/fetch_play_proof 单入口)
  ├─ schedule/ 包   → 课表数据面 parser.py / 纯解析 parsing.py / 策略 query.py + 节假日 holiday.py + 纪念日 anniversary.py + 覆盖/计划/澄清 override_store.py/plan_store.py/api.py + 检索与安排 sources.py/day_plan.py/resolve_when.py/attention.py/recall.py + 确认 confirm.py + 复盘 replan.py
  ├─ solar_terms.py      → 24 solar terms
  ├─ memory_bridge.py    → LanceDB read-only bridge + Ebbinghaus forgetting (v4)
  ├─ chiguo_watchdog.py  → daemon health checks (disk, memory, tick freshness)
  ├─ chiguo_rotation.py  → monthly log rotation → archive/
  ├─ chiguo_envcheck.py  → read-only env readiness check (exit 0/1/2)
  ├─ chiguo_version.py   → project version single source (VERSION="1.4", +0.1 per round)
  └─ chiguo_monitor.py   → streaming JSONL analytics (stats/alerts/health)

  Output: chiguo_decisions.jsonl (append-only structured log)
  State:  chiguo_state.json (atomic write: .tmp → os.replace)
```

**Config**: `chiguo_proactive.toml` — all parameters (309 lines). Legacy host section from Task 14 (superseded by `[host]`; only `wechat_recipient` still read). `DecisionEngine._maybe_reload_config()` detects mtime changes and hot-reloads in `--loop` mode without restart.

**Version**: `chiguo_version.py` is the single source (`VERSION="1.4"`); +0.1 per completed round. Decision JSON/`--version`/envcheck/monitor carry the version; state file `_version` is the schema number (STATE_VERSION=10), unrelated to the project version.

**5 emotion dimensions** with half-life decay toward equilibrium: loneliness (→100, 40h), affection (→0, 500h), anxiety (→100, 30h), energy (→100, 8h), tsundere_index (10-95, computed). User replies apply half-life decay drops (loneliness 0.35h, anxiety 0.5h).

**Trigger selection**: sigmoid-weighted, not hard-threshold. `chiguo_trigger.py` computes weights for 13 trigger types (incl. v7 follow_up), then weighted random choice. No priority-based cascading — every eligible trigger has non-zero probability.

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
- **Test isolation**: all tests use `tempfile.TemporaryDirectory` for state/log/config files. No shared state between tests. `tests/test_integration.py` injects `_base_dir` into a temp dir so it never touches real runtime files (state/break/log). Note: all 35 py test runners (+ 9 script tests) exit non-zero on assertion failure (use `$?` or `&&` chaining to detect regressions).

## 安全边界

**安全边界**：记忆库位于 `~/.pi-agent/memory/lancedb-pro`，本项目只通过 `memory_bridge.py` 进行只读查询。


