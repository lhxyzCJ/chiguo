# AGENTS.md

迟菓主动消息系统 (Chiguo proactive message system) — a zero-LLM math engine (`chiguo_daemon.py`) that decides when/what to send as JSON; the agent backend (Phase 4 起；默认 agent 后端，v1.8 起 `[host].runner = command` 可换任意 CLI agent) reads that JSON, generates the WeChat message, and sends it via wechat-bridge. Project root: the repo checkout directory (this repo is on GitHub; clone it anywhere, e.g. `/root/chiguo` on the dev machine). Always run commands from the repo root. Machine-specific paths (`data/mem0/` 记忆库、`~/.pi/...`) live in `chiguo_proactive.toml`；`deploy.sh` bootstraps a fresh machine.

Existing instruction sources to read before editing: `CLAUDE.md` (setup + architecture), `CLAUDE_CODE_RULES.md` (detailed module map, decision schema, known design decisions), `doc/SYSTEM.md`.

**Spec/plan 归档约定**：设计文档与实施计划**一律写到项目外 `~/chiguo-meta/`**（`specs/` 与 `plans/` 子目录，不进 git）；`doc/` 只放正式系统文档，仓库内不再出现 `docs/superpowers/`。

**双 README 铁律**：README 改动必须**双文件同步**（`README.md` 中文默认 + `README_EN.md` 英文）；`README.md` 顶部声明英文版可能滞后、以中文版为准。

## Iron rules

- **Decision/generation separation**: the daemon outputs structured JSON, never messages. Do not merge LLM logic into the daemon.
- **Security boundary**: mem0 记忆库位于 `data/mem0/`（qdrant 嵌入式 + history.db，gitignore），访问经 `memory/` 包（默认 `Mem0Backend`，CLI 见 `python -m memory`）；LLM key 读 `~/.pi/agent/auth.json` 的 opencode-go 条目，不进 git。
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
9. **铁律：完成任务并报告之前，必须解决 todo list 与宿主的报错**——逐项 `complete_step` 签收任务（不留 incomplete 项）；报告前处理全部宿主报错（final-answer readiness 门禁：无未签收 todo、本轮有可观测工作、mutation 后运行验证并提交 reviewed_paths、prefer 能力调用或 `use_capability` 显式 decline），带着报错收尾视为未完成
10. 禁止写无用的测试/备份/免责声明/保持项目整体整洁有序，遵守代码规范，写完任何代码后必须校验语法是否正确
11. 做好随时有break change的准备，不要留没有意义的降级/备选，除非用户指定

## 称谓约定（哥哥 / 用户 / 主人）

- **哥哥**：仅人设语音——最终会进 LLM 上下文或用户微信的文字（prompt 预设、`hint` 字段、澄清反问、直发文案）+ `personality/` 目录。判断标准：这段文字是迟菓在说话。
- **用户**：一切开发侧——注释、docstring、CLI help、日志诊断、运维输出、配置注释（引用人设原文除外）、测试名/注释/非人设 fixture、系统文档。
- **主人**：禁用，全仓零出现（含文档；历史记忆数据不追溯）。输入解析仅兼容哥哥前缀。

## 宿主收尾门禁（final-answer readiness）

Reasonix 宿主在 final-answer 时检查四类验收项，报错模式固定，对策如下：

1. **capability 门禁**：capability-route 中标 `require`/`prefer` 的 skill 必须**显式调用**（`read_skill`/`run_skill`/`use_capability`）或显式 decline（`use_capability` action=decline + reason）；「内容已常驻系统提示」不算调用痕迹
2. **verification 门禁**：mutation 后必须跑**宿主白名单**验证命令，并在 `complete_step` evidence 的 `command` 字段引用：JS 改动 → `node --check <file>`；文档/纯文本改动 → `git diff --check`。标准测试命令（`pytest`/`go test ./...`/`npm test`）**已随测试套件清空而不可用**。**不算数**：`git status/log`、`gh run ...`、`node -e`/`python -c` 内联解释器
3. **review 门禁**：mutation 后跑 `review`/`security_review` 覆盖最新变更；按建议改码后必须重跑
4. **todo 门禁**：逐项 `complete_step` 签收，不留 incomplete 项

机械自检（无自动化测试）：Python `uv run python -m compileall -q <改动文件>`；JS `node --check <file>`；Shell `bash -n <file>`；端到端冒烟 `uv run python chiguo_daemon.py --compact`（零模型门控，stdout JSON）。

## Run & self-check

项目当前**无测试套件**：`tests/`、`scripts/ci-test.sh`、`.github/workflows/ci.yml` 与 pytest 依赖已在大规模重构前整体删除，仓库不再有自动化测试链（`wechat-bridge/vendor/wechatbot/tests/` 是第三方 SDK 自带测试，不属本项目）。改动后只做静态自检 + 冒烟运行，回归保障以人工审阅为准。

```bash
uv run python -m compileall -q <改动文件>   # Python 语法
node --check <file>                        # JS 语法
bash -n <file>                             # Shell 语法
uv run python chiguo_daemon.py --compact    # 端到端冒烟（零模型门控，stdout JSON）
```

- Python 3.14 via uv (`.venv` exists, Python 3.14.7). 3.14-only syntax is intentional: bracketless `except E1, E2:`, deferred annotations — do NOT add `from __future__ import annotations`.
- 运行期依赖 CWD 下的 `chiguo_proactive.toml` → 所有命令从项目根运行。

## Architecture (fast map)

- `chiguo_daemon.py` (DecisionEngine) → `chiguo_state.py` (5-dim emotion engine + 8-dim personality + schedule/holidays/memory + circadian/pending_topics; v1.10 弹性衰减 elastic_recover + 情绪交互矩阵 apply_interaction_matrix + 回复饱和阻尼 drop_damp; v1.11 惯性阻尼 impact_inertia + 用户情绪感知 user_mood/comfort + OU 噪声 + 基线漂移 baseline_*; v1.12 B1 事件类型化情绪 delta EVENT_DELTA + B2 情绪-记忆耦合 emotion_tag + A2 回复率统计 reply_stats + A3 信息增益门控消费), `chiguo_trigger.py` (14 sigmoid-weighted trigger types (incl. v7 follow_up + v1.11 comfort), no hard thresholds; v1.10 A3 日程乘数×抖动/A4 三段激活（min_activation 沉默、must_send_activation 必选）/A5 未回复退场状态机（backing_off/silent）/A6 repeat 阻尼泛化; v1.12 A2 分类型回复率反馈闭环 reply_feedback_*), `chiguo_topics.py` (8-source topic injection: schedule/memory/weather/general/anniversary/solar_terms/preference_followup + v9 netease via NeteaseService; v1.10 A9 内容级防复读 jaccard_3gram; v1.12 C3 死 metadata 清理 text 优先), `chiguo_composer.py` (Intent × Cue × Vibe + v1.10 兜底 CLI：decision JSON/--trigger 模板池直出 + _FALLBACK_LINES), `chiguo_math.py` (pure functions), `chiguo_bayesian.py` (6 user states; v1.12 A1 转移矩阵+前向滤波 TRANSITIONS/prev_posterior + A3 后验熵产出), `chiguo_circadian.py` (dual-bucket circadian sleep-window learning: weekday/weekend + play-activity merging), `memory/` 包 (v1.12 C1 确定性巩固 consolidate_plan/consolidate + C2 复习强化 note_recalled/recall_count + C3 死 metadata 清理 + C4 写全轮次 write_full_turns + B2 emotion_tag 写读), `chiguo_monitor.py` (v1.12 D1 主动消息评估 proactive_stats), `netease/` 包 (数据面 `bridge.py` NeteaseBridge 实例 + 策略层 `service.py` NeteaseService DI：健康/登录失效检测/降级链/peek-consume 配额/随机选源/播放反证单入口 `fetch_play_proof`;运行时文件锚定 `<base_dir>/netease/`), `chiguo_version.py` (project version single source: VERSION="1.24", MINOR +1 per round (1.9→1.10→1.11→1.12→1.13→1.14→1.15→1.16→1.17→1.18→1.19→1.20→1.21→1.22→1.23→1.24, not decimal addition); decision JSON/--version/envcheck/monitor carry it).
- Everything tunable in `chiguo_proactive.toml` (22 sections; line count via `wc -l chiguo_proactive.toml`); hot-reloads via mtime check in `--loop` mode only (cron spawns fresh processes).
- Output: `chiguo_decisions.jsonl` (append-only). State: `chiguo_state.json` (atomic `.tmp` → `os.replace`, `.bak` backup, SHA256 checksum, monotonic `tick_seq`; `mono_anchor`/`wall_anchor` monotonic anchor pair persisted at top level, #206 — caps emotion elapsed against NTP wall-clock forward jumps in cron mode). Privacy runtime files (state/decisions/messages/login state) and monitoring/health JSONL (audit/health) are `.gitignore`d (local only, history rewritten).
- CLI convention: JSON to stdout, diagnostics to stderr. Always use `CST = timezone(timedelta(hours=8))` — never naive datetimes.
- **agent backend integration (Phase 4, v1.4；v1.8 agent 抽象：`[host].runner` = agent（默认，agent 后端二进制）| command（任意 CLI agent，`[host].agent_command` 指定，统一契约 `--prompt <完整提示词> --mode <mode>`，stdout JSON/NDJSON；RPC 常驻仅 agent 模式））**: 发送侧 `scripts/chiguo-tick.sh`（系统 crontab 每 15 分钟；`chiguo_daemon.py --compact` 零模型门控，send 才调 agent；agent 失败 → 5s 抖动重试一次（不计 fail_streak），仍失败 → 中止发送 + `agent_health.py` 记账（连续 3 次 → 微信告警 + 暂停探测，重启 loop/恢复后继续；v1.16 去 A8 composer 兜底））+ 回复侧 bridge `askAgent`（`scripts/agent-run.mjs --prompt <原文> --analysis-mode`，一次完成情绪分析 JSON + 回复，daemon recv_dedup 升级语义）+ 特殊命令（纪念日/假期）由 bridge 规则化确定性接管（`wechat-bridge/command-detect.mjs`，不经 agent）；`scripts/install_agent.sh` 引导 agent 环境（provider key 随 toml [host].provider，缺省 opencode-go/crontab）；记忆层 mem0 由 `uv sync` 安装（必需依赖；qdrant 嵌入式 data/mem0/ + ollama qwen3-embedding）；详见 `doc/AGENT_INTEGRATION.md`。微信发送走 wechat-bridge (`wechat-bridge/bridge.mjs` 随仓库部署, HTTP POST 127.0.0.1:18790/send, 必须 --noproxy '*'; 管理脚本 `scripts/wechat-bridge.sh` install/start/stop/status/login, deploy.sh 第 4 步接入); 回复侧 bridge 确定性 `--user-msg --recv-id <uuid>` + agent 补分析 (bridge 每条消息本地生成 uuid, daemon recv_dedup 按 id 精确去重升级语义, 见 CooldownState.recv_dedup). 登录态存 `wechat-bridge/credentials/`（gitignore 本地保留；新设备需 `bash scripts/wechat-bridge.sh login` 重新扫码，失效自动重登）. 会话并发模型：回复=chiguo-main（bridge 内 TurnQueue 串行）、主动发送=chiguo-send（tick 经 AGENTRUN_SESSION 注入）——两进程零共享会话。

## Known gotchas

- `memory/mem0_backend.py` (`Mem0Backend`) lazy-imports `mem0` inside `available` probing: daemon runs with `available=False` when mem0 is absent (60s throttle retry self-heals).
- `schedule/parser.py` requires `xskb.xlsx`; if missing, `schedule_valid=False` → availability base 1.0 (tier `unavailable`, treated as fully available). `_parse()` returns bool and never overwrites a valid cache with a failed parse.

## Git 工作流（代码改动）

- 代码改动（refactor:/fix:/feat:）一律走分支 + PR：
  - 分支名 依据修改内容自定
  - 每分支对应一个 GitHub Issue（`gh issue create`），PR 正文 `Closes #N`
  - 出口条件：语法自检 + 子代理自审 + 用户批准 → squash merge
- docs:/chore: 小改动可直接 main（已无 CI 自动验证）。
- 简化单元跟踪：从审查报告（~/chiguo-meta/audit/）拆出的每单元建 Issue，标题与正文含文件清单、改动内容、删行预估、依赖单元。

## After any code change

1. 语法自检 + 冒烟运行（见 §Run & self-check）；触碰 math/state/daemon 时人工审阅确认行为不漂移。
2. Update affected sections of `doc/SYSTEM.md`, `doc/README.md`；`doc/DEPLOYMENT.md` 在 deploy.sh/scripts 改动时同步。
