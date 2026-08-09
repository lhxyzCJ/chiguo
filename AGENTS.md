# AGENTS.md

迟菓主动消息系统 (Chiguo proactive message system) — a zero-LLM math engine (`chiguo_daemon.py`) that decides when/what to send as JSON; the agent backend (Phase 4 起；默认 pi-agent，v1.8 起 `[host].runner = command` 可换任意 CLI agent) reads that JSON, generates the WeChat message, and sends it via wechat-bridge. Project root: the repo checkout directory (this repo is on GitHub; clone it anywhere, e.g. `/root/chiguo` on the dev machine). Always run commands from the repo root. Machine-specific paths (`data/mem0/` 记忆库、`~/.pi/...`) live in `chiguo_proactive.toml`；`deploy.sh` bootstraps a fresh machine.

Existing instruction sources to read before editing: `CLAUDE.md` (setup + architecture), `CLAUDE_CODE_RULES.md` (detailed module map, decision schema, known design decisions), `doc/SYSTEM.md`.

**Spec/plan 归档约定**：设计文档与实施计划**一律写到项目外 `~/chiguo-meta/`**（`specs/` 与 `plans/` 子目录，不进 git）；`doc/` 只放正式系统文档，仓库内不再出现 `docs/superpowers/`。

**双 README 铁律**：README 改动必须**双文件同步**（`README.md` 中文默认 + `README_EN.md` 英文）；`README.md` 顶部声明英文版可能滞后、以中文版为准。

## Iron rules

- **Decision/generation separation**: the daemon outputs structured JSON, never messages. Do not merge LLM logic into the daemon.
- **Security boundary**: mem0 记忆库位于 `data/mem0/`（qdrant 嵌入式 + history.db，gitignore），访问经 `memory/` 包（默认 `Mem0Backend`；`memory_bridge.py` 为兼容门面）；LLM key 读 `~/.pi/agent/auth.json` 的 opencode-go 条目，不进 git。
- Before fixing: make a plan/todolist, dispatch parallel subagents, and self-audit with subagents after finishing. Subagents inherit the main model (do not override via `model` param); never use opus without explicit user authorization.

## 工程原则

1. 不保留向后兼容，过时路径直接删，别加兼容层
2. 选满足当前需求的最简实现，别搞多余抽象配置
3. 先跑通最小可用版本，再叠新功能，别换现有能跑的代码
4. 组件模块化，业务关注点分离
5. 优先用成熟维护的第三方库，没理由别重复造轮子
6. 先查现有项目依赖的能力，再考虑加包或自研
7. 架构决策看长期，别用临时凑合用的过渡方案
8. 参考成熟产品的验证方案，别从零发明

## Test & run

No pytest. Each `test_*.py` is a standalone runner with plain `assert`s; every runner exits non-zero on failure, so chain with `&&` or check `$?`.

```bash
bash scripts/ci-test.sh   # full suite (38 py + 10 script) — 本地与 GitHub Actions ci.yml 同一入口；任一失败退出非零
```

- Python 3.14 via uv (`.venv` exists, Python 3.14.6). 3.14-only syntax is intentional: bracketless `except E1, E2:`, deferred annotations — do NOT add `from __future__ import annotations`.
- Integration tests require `chiguo_proactive.toml` in CWD → always run from project root.
- Tests isolate via `tempfile.TemporaryDirectory` and never touch real runtime files (`tests/test_integration.py` injects `_base_dir` into a temp dir). `random.seed(42)` + fixed CST datetimes for determinism.

## Architecture (fast map)

- `chiguo_daemon.py` (DecisionEngine) → `chiguo_state.py` (5-dim emotion engine + 8-dim personality + schedule/holidays/memory + circadian/pending_topics), `chiguo_trigger.py` (13 sigmoid-weighted trigger types incl. v7 follow_up, no hard thresholds), `chiguo_topics.py` (8-source topic injection: schedule/memory/weather/general/anniversary/solar_terms/preference_followup + v9 netease via NeteaseService), `chiguo_composer.py` (Intent × Cue × Vibe), `chiguo_math.py` (pure functions), `chiguo_bayesian.py` (6 user states), `chiguo_circadian.py` (dual-bucket circadian sleep-window learning: weekday/weekend + play-activity merging), `netease/` 包 (数据面 `bridge.py` NeteaseBridge 实例 + 策略层 `service.py` NeteaseService DI：健康/登录失效检测/降级链/peek-consume 配额/随机选源/播放反证单入口 `fetch_play_proof`;运行时文件锚定 `<base_dir>/netease/`), `chiguo_version.py` (project version single source: VERSION="1.9", +0.1 per round; decision JSON/--version/envcheck/monitor carry it).
- Everything tunable in `chiguo_proactive.toml` (314 lines); hot-reloads via mtime check in `--loop` mode only (cron spawns fresh processes).
- Output: `chiguo_decisions.jsonl` (append-only). State: `chiguo_state.json` (atomic `.tmp` → `os.replace`, `.bak` backup, SHA256 checksum, monotonic `tick_seq`). Privacy runtime files (state/decisions/messages/login state) are `.gitignore`d (local only, history rewritten); monitoring/health JSONL stays tracked for analysis.
- CLI convention: JSON to stdout, diagnostics to stderr. Always use `CST = timezone(timedelta(hours=8))` — never naive datetimes.
- **pi-agent integration (Phase 4, v1.4；v1.8 agent 抽象：`[host].runner` = pi（默认，pi-agent 二进制）| command（任意 CLI agent，`[host].agent_command` 指定，统一契约 `--prompt <完整提示词> --mode <mode>`，stdout JSON/NDJSON；RPC 常驻仅 pi 模式））**: 发送侧 `scripts/chiguo-tick.sh`（系统 crontab 每 15 分钟；`chiguo_daemon.py --compact` 零模型门控，send 才调 pi）+ 回复侧 bridge `askPi`（`scripts/pi-run.mjs --prompt <原文> --analysis-mode`，一次完成情绪分析 JSON + 回复，daemon recv_dedup 升级语义）+ 特殊命令（纪念日/假期）由 bridge 规则化确定性接管（`wechat-bridge/command-detect.mjs`，不经 pi）；`scripts/install_pi.sh` 引导 pi 环境（provider key 随 toml [host].provider，缺省 opencode-go/crontab）；记忆层 mem0 由 `uv sync` 安装（必需依赖；qdrant 嵌入式 data/mem0/ + ollama qwen3-embedding）；详见 `doc/PI_INTEGRATION.md`。微信发送走 wechat-bridge (`wechat-bridge/bridge.mjs` 随仓库部署, HTTP POST 127.0.0.1:18790/send, 必须 --noproxy '*'; 管理脚本 `scripts/wechat-bridge.sh` install/start/stop/status/login, deploy.sh 第 5 步接入); 回复侧 bridge 确定性 `--user-msg` + pi 补分析 (daemon recv_dedup 升级语义, 见 CooldownState.recv_dedup). 登录态存 `wechat-bridge/credentials/`（gitignore 本地保留；新设备需 `bash scripts/wechat-bridge.sh login` 重新扫码，失效自动重登）. 会话并发模型：回复=chiguo-main（bridge 内 TurnQueue 串行）、主动发送=chiguo-send（tick 经 PIRUN_SESSION 注入）——两进程零共享会话。

## Known gotchas

- `memory/mem0_backend.py` (`Mem0Backend`) lazy-imports `mem0` inside `available` probing: daemon runs with `available=False` when mem0 is absent (60s throttle retry self-heals; tests set `CHIGUO_MEM0_DISABLED=1` for deterministic unavailable).
- `schedule/parser.py` requires `xskb.xlsx`; if missing, falls back to availability=0.85 (free time). `_parse()` returns bool and never overwrites a valid cache with a failed parse.
- `tests/test_topics.py` anchors `_real_state` with `semester_end` in the past — don't "fix" it to real dates (time-bomb test).

## Git 工作流（代码改动）

- 代码改动（refactor:/fix:/feat:）一律走分支 + PR：
  - 分支名 `simplify/<issue-N>-<slug>`（如 `simplify/12-composer-cue`）
  - 每分支对应一个 GitHub Issue（`gh issue create`），PR 正文 `Closes #N`
  - 出口条件：CI 全链绿 + 子代理自审 + 用户批准 → squash merge
- docs:/chore: 小改动可直接 main（CI 仍自动验证）。
- 简化单元跟踪：从审查报告（~/chiguo-meta/audit/）拆出的每单元建 Issue，
  标题 `[simplify] <模块>: <改动>`，正文含文件清单、改动内容、删行预估、依赖单元。

## After any code change

1. Run affected test files; full chain (38 py + 10 script) if touching math/state/daemon.
2. Update affected sections of `doc/SYSTEM.md`, `doc/README.md`；`doc/DEPLOYMENT.md` 在 deploy.sh/scripts 改动时同步。
