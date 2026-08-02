# AGENTS.md

迟菓主动消息系统 (Chiguo proactive message system) — a zero-LLM math engine (`chiguo_daemon.py`) that decides when/what to send as JSON; pi-agent (Phase 4 起；OpenClaw 已停用) reads that JSON, generates the WeChat message, and sends it via wechat-bridge. Project root: the repo checkout directory (this repo is on GitHub; clone it anywhere, e.g. `/root/chiguo` on the dev machine). Always run commands from the repo root. Machine-specific paths (`~/.pi-agent/memory/lancedb-pro` 记忆库、`~/.pi/...`) live in `chiguo_proactive.toml`；`deploy.sh` bootstraps a fresh machine.

Existing instruction sources to read before editing: `CLAUDE.md` (setup + architecture), `CLAUDE_CODE_RULES.md` (detailed module map, decision schema, known design decisions), `doc/SYSTEM.md`, `doc/IMPROVE.md` (fix log), `MEMORY.md` (change history, newest first).

## Iron rules

- **Decision/generation separation**: the daemon outputs structured JSON, never messages. Do not merge LLM logic into the daemon.
- **Security boundary**: OpenClaw 已停用（`~/.openclaw` 可整体删除，勿恢复引用）。LanceDB 记忆库位于 `~/.pi-agent/memory/lancedb-pro`，只读访问仅经 `memory_bridge.py`。
- Before fixing: make a plan/todolist, dispatch parallel subagents, and self-audit with subagents after finishing. Subagents inherit the main model (do not override via `model` param); never use opus without explicit user authorization.

## Test & run

No pytest. Each `test_*.py` is a standalone runner with plain `assert`s; every runner exits non-zero on failure, so chain with `&&` or check `$?`.

```bash
node test_pi_run.mjs && node test_bridge_askpi.mjs && node test_bridge_cmd.mjs && node test_bridge_health.mjs && node test_trigger_script.js && bash test_install_integration.sh && bash test_install_pi.sh && bash test_wechat_bridge.sh && bash test_tick_health.sh && \
uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && \
uv run python test_integration.py && uv run python test_monitor.py && \
uv run python test_eventbus.py && uv run python test_personality.py && \
uv run python test_bayesian.py && uv run python test_composer.py && \
uv run python test_ebbinghaus.py && uv run python test_longing.py && \
uv run python test_escape_valve.py && uv run python test_feedback.py && \
uv run python test_trigger.py && uv run python test_topics.py && \
uv run python test_circadian.py && uv run python test_followup.py && \
uv run python test_netease_proof.py && uv run python test_netease_service.py && \
uv run python test_envcheck.py && uv run python test_composer_trade.py && \
uv run python test_personality_init.py && uv run python test_toml_binding.py && \
uv run python test_adapt_personality.py && uv run python test_pi_health.py   # full suite (24 py + 8 script tests)

uv run python test_monitor.py                    # single file
uv run python chiguo_daemon.py                   # single evaluation → JSON to stdout
uv run python chiguo_daemon.py --stats --alerts --monitor
uv run python chiguo_monitor.py --summary --health
uv run python chiguo_watchdog.py                 # standalone health, exit 0/1/2
uv run python chiguo_envcheck.py                 # env readiness, exit 0/1/2 (read-only)
uv run python chiguo_demo.py                     # interactive demo, templates only
```

- Python 3.14 via uv (`.venv` exists, Python 3.14.6). 3.14-only syntax is intentional: bracketless `except E1, E2:`, deferred annotations — do NOT add `from __future__ import annotations`.
- Integration tests require `chiguo_proactive.toml` in CWD → always run from project root.
- Tests isolate via `tempfile.TemporaryDirectory` and never touch real runtime files (`test_integration.py` injects `_base_dir` into a temp dir). `random.seed(42)` + fixed CST datetimes for determinism.

## Architecture (fast map)

- `chiguo_daemon.py` (DecisionEngine) → `chiguo_state.py` (5-dim emotion engine + 8-dim personality + schedule/holidays/memory + circadian/pending_topics), `chiguo_trigger.py` (13 sigmoid-weighted trigger types incl. v7 follow_up, no hard thresholds), `chiguo_topics.py` (8-source topic injection: schedule/memory/weather/general/anniversary/solar_terms/preference_followup + v9 netease via NeteaseService), `chiguo_composer.py` (Intent × Cue × Vibe), `chiguo_math.py` (pure functions), `chiguo_bayesian.py` (6 user states), `chiguo_circadian.py` (dual-bucket circadian sleep-window learning: weekday/weekend + play-activity merging), `netease_bridge.py` (fetch_recent_play: play proof inside quiet window, cached; v9: `_api_get` limited retry w/ backoff + daily-recommendation schema filter), `chiguo_netease.py` (v9 NeteaseService strategy layer: netease_health.json health/login-expiry detection/degradation chain/peek-consume two-phase quota/random source pick/music_topic material), `chiguo_eventbus.py` (pub/sub singleton), `chiguo_version.py` (project version single source: VERSION="1", +0.1 per round; decision JSON/--version/envcheck/monitor carry it).
- Everything tunable in `chiguo_proactive.toml` (277 lines); hot-reloads via mtime check in `--loop` mode only (cron spawns fresh processes).
- Output: `chiguo_decisions.jsonl` (append-only). State: `chiguo_state.json` (atomic `.tmp` → `os.replace`, `.bak` backup, SHA256 checksum, monotonic `tick_seq`). Runtime files are `.gitignore`d.
- CLI convention: JSON to stdout, diagnostics to stderr. Always use `CST = timezone(timedelta(hours=8))` — never naive datetimes.
- OpenClaw integration (v12) → **pi-agent integration (Phase 4, v1.4)**: 发送侧 `scripts/chiguo-tick.sh`（系统 crontab 每 15 分钟；`chiguo_daemon.py --compact` 零模型门控，send 才调 pi）+ 回复侧 bridge `askPi`（`scripts/pi-run.mjs --prompt <原文> --analysis-mode`，一次完成情绪分析 JSON + 回复，daemon recv_dedup 升级语义）+ 特殊命令（纪念日/假期）由 bridge 规则化确定性接管（`wechat-bridge/command-detect.mjs`，不经 pi）；`scripts/install_pi.sh` 引导 pi 环境（opencode-go key/memory-lancedb-pro/crontab）；详见 `doc/PI_INTEGRATION.md`。OpenClaw 链路已停用（`openclaw cron disable chiguo-check`），回退参考 `doc/OPENCLAW_INTEGRATION.md`。微信发送走 wechat-bridge (`wechat-bridge/bridge.mjs` 随仓库部署, HTTP POST 127.0.0.1:18790/send, 必须 --noproxy '*'; 管理脚本 `scripts/wechat-bridge.sh` install/start/stop/status/login, deploy.sh 第 5.5 步接入); 回复侧 bridge 确定性 `--user-msg` + pi 补分析 (daemon recv_dedup 升级语义, 见 CooldownState.recv_dedup). 登录态存仓库 `wechat-bridge/credentials/` git 跟踪 (private 仓库, 新设备 clone 即保留登录, 失效自动重登). 会话并发模型：回复=chiguo-main（bridge 内 TurnQueue 串行）、主动发送=chiguo-send（tick 经 PIRUN_SESSION 注入）——两进程零共享会话。

## Known gotchas

- `memory_bridge.py` lazy-imports `lancedb` inside `_ensure_table()` (CLAUDE_CODE_RULES.md §1 claims a hard top-level import — stale; CLAUDE.md is correct): daemon runs with `available=False` when lancedb is absent.
- `schedule_parser.py` requires `xskb.xlsx`; if missing, falls back to availability=0.85 (free time). `_parse()` returns bool and never overwrites a valid cache with a failed parse.
- `test_topics.py` anchors `_real_state` with `semester_end` in the past — don't "fix" it to real dates (time-bomb test).

## After any code change

1. Run affected test files; full 19-file suite if touching math/state/daemon.
2. Update affected sections of `doc/SYSTEM.md`, `doc/IMPROVE.md`, `doc/README.md`.
3. Log the change in `MEMORY.md` (date, files modified, description).
