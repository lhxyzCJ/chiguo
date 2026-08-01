# 2026-08-01 OpenClaw 集成改造设计（v11 集成升级）

> 状态：已批准（用户 2026-08-01 确认）
> 背景：发送侧 cron `--system-event` 每 30 分钟空转 agent（~90% idle 白烧模型调用）；回复侧 `.claude/settings.json` 的 `UserPromptSubmit` hook 是 Claude-Code 机制而非 OpenClaw 原生。本次按官方文档（docs.openclaw.ai，2026-08 抓取）升级为原生机制。

## 1. 目标

1. **发送侧**：OpenClaw automations + trigger-script 条件门控 —— daemon 决策无模型执行，仅 `action=send` 才唤醒 agent 生成消息，消灭 idle 空转模型调用。
2. **回复侧**：用官方 standing order（agents/main/AGENTS.md）+ 既有 SKILL.md 指令替代 Claude-Code `UserPromptSubmit` hook；LLM 情绪分析保留在 agent 回复流程中，单记录原则。
3. **可移植性**：项目被任意机器 pull 到本地后，`deploy.sh`（或单独 `install_integration.sh`）自动完成安装+严格校验+旧方案残留迁移；不依赖对特定机器的远程操作。
4. **严格遵从官方文档**：所有 CLI 命令、配置开关、校验方法均出自 docs.openclaw.ai；`<command> --help` 为权威功能探测手段（官方文档原话："run `<command> --help` for the authoritative, current list"）。

## 2. 架构（数据流）

```
发送侧（新）：
OpenClaw automations --every 15m --trigger-script scripts/chiguo-watch.js --session main
  → 触发器无模型执行：tools.call('exec') 跑 uv run python chiguo_daemon.py --compact
    ├─ action=idle → {fire:false}（零模型调用）
    └─ action=send  → {fire:true, message:<决策JSON>}（含 state/context）
  → main session agent 收到 system-event = 生成指令 + 决策 JSON
    → SUN2.md 生成 1-3 句 → openclaw-weixin 发送
    → daemon --record-send / --send-result 回写（保留现状，幂等）

回复侧（新）：
微信消息到达 → agent 正常回复；standing order 强制流程：
  LLM 情绪分析（warmth/effort/attention + 话题摄入）→ daemon --user-msg --analysis → SUN2.md 回复
  （删除 .claude/settings.json UserPromptSubmit hook；单记录，无双重记录）
```

## 3. 关键设计决策

| 决策 | 理由 |
|---|---|
| trigger-script 而非 webhook / heartbeat | 官方文档把 trigger-script 定位为「watch actionable state」一等公民；webhook 需 daemon 发 HTTP 混入网络职责；heartbeat 是近似调度且混入主会话检查。OpenClaw 调度器独占时序 |
| 15 分钟间隔 | idle 评估不再消耗模型，决策延迟 30min→15min；daemon 内部 pacing（poisson/min_interval/quiet hours）不受影响；每小时 4 次 python 评估开销可忽略 |
| 单记录原则 | 只有 agent 调 `--user-msg` 记录回复，避免 hook+agent 双重记录破坏回复延迟建模（`on_user_message` 有状态副作用） |
| standing order 放 agents/main/ | 安全边界内（`~/.openclaw/workspace/agents/main/`），不动 workspace 根 AGENTS.md |
| 决策/生成分离铁律不变 | daemon 只输出 JSON；trigger script 是纯搬运工（exec + 解析 + fire），不生成消息 |
| 功能探测用 `--help` 而非版本号 | 官方文档明确 `<command> --help` 是权威命令清单 |

## 4. 组件

### 4.1 `scripts/chiguo-watch.js`（新）

官方 trigger-script 契约（JS，`tools.call('exec', ...)`）：

- 执行：`exec` 跑 `<repo>/.venv/bin/python <repo>/chiguo_daemon.py --compact`（路径由安装器注入或环境变量 `CHIGUO_REPO`；相对路径锚定规则与 `_anchored` 一致）
- 解析：stdout 单行 JSON；`action === 'send'` → `{fire:true, message: <完整决策JSON>}`；`idle` → `{fire:false}`
- 容错：daemon 崩溃/非零退出/坏 JSON/超时 → `{fire:false}` 并持久 `state.last_error`（下次评估可带出）
- 契约返回：`{fire, message?, state?}`，state ≤16KB（官方上限）

### 4.2 `test_trigger_script.js`（新）

node 单测，mock `tools.call` 四路径：idle / send / 坏 JSON / daemon 非零退出。`node test_trigger_script.js`，退出码非零即失败（与仓库 test_*.py 约定一致）。

### 4.3 `scripts/install_integration.sh`（新）—— 自包含安装器

三模式：`--dry-run`（只扫描报告，默认提示先跑）/ `--yes`（自动解决全部）/ 默认交互（逐项确认）。每次修改前备份。退出码 0=完成 / 1=有残留未处理或警告 / 2=严重问题。

**阶段 0 环境探测（全只读）**：

```
1. openclaw -V                                版本可执行（无 openclaw → 跳过集成安装，退出 0 + 文档提示）
2. openclaw automations add --help | grep --trigger-script
     功能探测（官方：--help 为权威清单）。缺失 → warn + 降级路径（保留旧 cron），退出 1
3. openclaw status / openclaw gateway status   Gateway 在跑（trigger 由调度器执行）
4. openclaw config get cron.triggers.enabled   当前开关状态
```

**阶段 0b 旧方案残留扫描**（发现即报告，一个不留）：

```
a. openclaw automations list --all | grep -i chiguo        （--all 含禁用，官方文档）
b. .claude/settings.json UserPromptSubmit 含 chiguo 条目   （项目根/workspace）
c. ~/.openclaw/workspace/skills/chiguo/scripts/on-user-msg.sh（老 hook 脚本）
d. openclaw hooks list | grep -i chiguo                    （若曾装原生 hook）
e. agents/main/AGENTS.md 中 chiguo 集成段落残留
f. openclaw config get hooks.internal.handlers              （legacy 数组格式，官方文档承认兼容但建议迁移）
```

**阶段 1 配置修改（官方入口）**：

```
openclaw config set cron.triggers.enabled true   （官方命令，自动 schema 校验；先备份 config file）
openclaw config validate                         配置合法
```

**阶段 2 注册（幂等）**：

```
openclaw automations rm chiguo-check（容忍不存在）—— 若有多个 chiguo 旧作业，逐个列出并处置
openclaw automations add --name chiguo-check --every 15m \
  --trigger-script <repo>/scripts/chiguo-watch.js --session main \
  --system-event "<生成指令：按 SUN2.md 生成消息并发送，发后 daemon --record-send/--send-result>"
openclaw automations get chiguo-check → 确认落库
```

**阶段 3 回复侧安装**：

```
agents/main/AGENTS.md 写入 standing order 段落（幂等去重 + 备份）：
  "每次回复主人的微信消息前：1) LLM 分析情绪(warmth/effort/attention/topic)
   2) python3 <repo>/chiguo_daemon.py --user-msg <原文> --analysis <JSON>
   3) 按 SUN2.md 人格回复"
删除 .claude/settings.json 中 chiguo 的 UserPromptSubmit 条目（备份后 JSON 编辑，保留其他 hook）
老 on-user-msg.sh → 备份为同目录 on-user-msg.sh.bak 后从 scripts/ 移除（不再被引用）
```

**阶段 4 收尾验证**：

```
openclaw automations list | grep chiguo-check         作业在册
openclaw config validate                              配置仍合法
openclaw security audit --deep                        危险自动化开关后官方审计（--yes 模式执行并报告）
openclaw automations run chiguo-check --wait --wait-timeout 10m   端到端冒烟（输出 idle 即链路通）
```

**残留处置矩阵**：

| 残留类型 | 自动解决（官方命令） | 用户解决（文档指引） |
|---|---|---|
| 旧 automations 作业 | `openclaw automations rm <id>` 后注册新作业 | 手工 `openclaw automations disable <id>` 暂停 |
| `.claude/settings.json` chiguo hook 条目 | 备份后移除条目（保留其他 hook） | 手工删除 hook 块 |
| 老 on-user-msg.sh | 备份为 .bak 后移除 | 手工删除 |
| OpenClaw 原生 hook 残留 | `openclaw hooks disable <name>` | 手工 disable |
| legacy `hooks.internal.handlers` | `openclaw doctor --fix`（官方迁移工具） | 手工改配置 + `openclaw config validate` |
| AGENTS.md 旧段落 | 幂等替换为新版 standing order | 手工编辑 |

### 4.4 `doc/OPENCLAW_INTEGRATION.md`（重写）

新架构全流程：trigger-script 作业注册、standing order 内容、install 脚本用法、手工回退/降级路径、版本兼容矩阵、官方文档出处索引。

### 4.5 既有文件更新

- `deploy.sh`：末尾调用 `install_integration.sh`（支持 `--skip-integration`）；自检数组不变
- `AGENTS.md`：测试链加 node 测试（test_trigger_script.js）、集成方式一句话
- `README.md` / `doc/README.md`：集成方式、scripts/ 目录说明
- `MEMORY.md` / `doc/IMPROVE.md`：变更记录

## 5. 错误处理与安全

- 任何机器可移植：脚本路径相对仓库根解析；机器相关路径单一事实来源为 `chiguo_proactive.toml`
- install 脚本全幂等：重复运行不产生重复作业/段落/残留
- 版本不支持时不失败部署：warn + 保留旧 cron system-event 方式（文档写明降级路径）
- `cron.triggers.enabled` 是官方「无人值守代码执行」开关——脚本仅执行 daemon 一条命令，风险面可控；修改带备份，文档明示
- 安全边界：standing order 只写 `agents/main/`；不动 workspace 根 AGENTS.md；不触碰 `~/.openclaw/memory/` 等

## 6. 测试与验证策略

1. 本机：node 单测（4 路径）+ 19 文件 py 全量回归 + install 脚本 `--dry-run` 自测（本机无 openclaw → 阶段 0 探测应优雅降级）
2. 官方文档核对：安装器每条命令对照 docs.openclaw.ai 对应页（automations/cron-jobs、hooks、cli、config、doctor）
3. 任意机器（用户 pull 后）：`bash deploy.sh` → 自动安装+自校验；`openclaw automations run chiguo-check --wait` 冒烟
4. 完成后 code-review 子代理全面审查（仓库铁律）

## 7. 范围外（YAGNI）

- 不引入 webhook/HTTP 推送机制（保持 daemon 零网络职责）
- 不写 OpenClaw 原生 TS plugin hook（internal hook + standing order 已覆盖需求）
- 不改 daemon 决策/日志核心逻辑（本轮纯集成层改造）
- 不迁移历史 decisions.jsonl（archive/ 已按月轮转）

## 8. 官方文档出处索引

- Automations / trigger-script / event triggers：`/automation`、`/automation/cron-jobs`
- 危险自动化开关 `cron.triggers.enabled`：`/automation/cron-jobs`（Warning 段）
- Internal hooks / 事件表 / legacy handlers / doctor --fix：`/automation/hooks`
- Standing orders：`/automation`（Quick decision guide）
- CLI 参考（config get/set/validate、automations、hooks、security audit、doctor）：`/cli`
- `<command> --help` 权威性：`/cli`（Command tree 段）
- `openclaw cron` = `openclaw automations` 别名：`/automation/cron-jobs`
