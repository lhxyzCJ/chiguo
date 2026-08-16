# agent 后端集成指南（v1.19，agent/LLM 集成唯一文档）

> 本文档是 agent 后端集成契约的唯一权威文档。前身 `doc/PI_INTEGRATION.md` 在 #99 全库 pi→agent 重命名时
> git mv 至此（保留历史；当时决议"重命名不改变行为语义、版本不步进"）。版本随 `chiguo_version.py` 正常步进，当前 1.19。
> 架构/能力描述一律用「agent 后端」表述（不点名具体 agent 实现）；仅操作性命令/路径保留具体名（豁免清单见 §0.5）。

## 〇、命名契约（#99 冻结）

### 0.1 文件映射（git mv 保留历史）

| 旧名 | 新名 |
|---|---|
| `scripts/pi-run.mjs` | `scripts/agent-run.mjs`（唯一 agent 调用层） |
| `wechat-bridge/pi-rpc.mjs` | `wechat-bridge/agent-rpc.mjs` |
| `scripts/pi_health.py` | `scripts/agent_health.py` |
| `scripts/install_pi.sh` | `scripts/install_agent.sh` |
| `scripts/pi-auth.sh` | `scripts/agent-auth.sh` |
| `tests/test_agent_run.mjs` | `tests/test_agent_run.mjs`（#99 前已 agent 命名，未改名） |
| `tests/test_pi_health.py` | `tests/test_agent_health.py` |
| `tests/test_bridge_askagent.mjs` | `tests/test_bridge_askagent.mjs`（#99 前已命名，未改名） |
| `tests/test_install_pi.sh` | `tests/test_install_agent.sh` |
| `doc/PI_INTEGRATION.md` | `doc/AGENT_INTEGRATION.md`（本文件） |
| `pi_health.json(.lock)` | `agent_health.json(.lock)`（运行时） |
| `logs/pi-run.log` | `logs/agent-run.log`（运行时） |

### 0.2 env 映射

`PIRUN_*`→`AGENTRUN_*`（AGENTRUN_RUNNER/PROVIDER/MODEL/THINKING/REPLY_THINKING/TIMEOUT/SESSION/NEW_SESSION/PERSONALITY/GUIDE/TOOLS/TELEMETRY/AGENT_COMMAND）、
`PI_BIN`→`AGENT_BIN`、`PI_TIMEOUT`→`AGENT_TIMEOUT`、`PI_RUN_SCRIPT`→`AGENT_RUN_SCRIPT`、
`WECHAT_BRIDGE_AGENT_RUN/RPC/HEALTH/HEALTH_PY`→`WECHAT_BRIDGE_AGENT_*`、
`PI_FALLBACK_PROVIDER`→`AGENT_FALLBACK_PROVIDER`（wechat-bridge.sh 解析 auth.json 回退条目）、
`PI_KEY`→`AGENT_KEY`（wechat-bridge.sh 注入 OPENCODE_API_KEY 的来源值）。
`PI_MODE_FILE` 已随测试夹具重构移除，无对应新名。

### 0.3 标识符映射

`askAgent`→`askAgent`（bridge 统一入口）、`runPiRun`→`runAgentRun`、`PiRpc`→`AgentRpc`、`check_pi`→`check_agent`、
`check_pi_auth`→`check_agent_auth`、`pi_bin`→`agent_bin`、`RUNNER==='pi'`→`'agent'`、`PI_RPC_ENABLED`→`AGENT_RPC_ENABLED`。

### 0.4 CLI/配置映射

`--skip-pi`→`--skip-agent`（deploy.sh/envcheck 用户可见参数）、`[host].runner="pi"`→`"agent"`（默认值 = pi-agent 二进制，行为不变）。

### 0.5 豁免清单（产品名，grep 允许残留）

`~/.pi/`、`~/.pi-agent/`、pi-agent 二进制名 `pi`（`AGENT_BIN` 默认值 `'pi'`、`check_agent(agent_bin="pi")`）、
`pi --provider/--model/--session-id` 子命令、`pi_health` 相关历史文档引用、`pi --mode rpc`（RPC 常驻模式）。
**误匹配排除**：`topics`/`api`/`_pi` 等普通子串（chiguo_topics.py、schedule/api.py、tests/test_topics.py 等）。

## 一、架构总览

```
发送侧（系统 crontab 门控，零 idle 模型调用）：
crontab */15 * * * * scripts/chiguo-tick.sh
  → source scripts/agent-auth.sh（解析 auth.json 的 opencode-go/[host].provider key → OPENCODE_API_KEY）
  → .venv/bin/python chiguo_daemon.py --compact（零 LLM 评估）
    ├─ action=idle → 静默退出（~90% 的评估不唤醒 LLM）
    └─ action=send → 发送侧 RPC 优先（v1.11 B1 默认）：POST <bridge>/agent/prompt {text: 决策JSON, mode: send}
        → 常驻 agent RPC 生成消息（chiguo-send 会话）
        → RPC 失败自动回退 node scripts/agent-run.mjs --prompt <决策JSON> --send-mode
          （AGENTRUN_SESSION=<toml [host].send_session_id>）
        → agent 失败 → sleep 5s 整链重试一次（U2：抖动缓冲，重试成功不计故障）；仍失败中止（无 composer 兜底）
        → 回文本后 curl --noproxy '*' -X POST <toml [host].wechat_bridge_url> {"to","text"} → bridge → bot.send()
        → scripts/agent_health.py record（成败记账，状态 transition 时经 /send 告警/恢复）
        → daemon --record-send <msg_id> --text <text> --trigger <trigger> 回写（幂等）

回复侧（bridge 内联路由，handleMessage 单一链路）：
微信消息到达 → OWNER_ID 门（非本人 → 仅 askAgent 回复，不进命令/回忆/追问路径）
  → recordUserMsg（daemon --user-msg，确定性；命令消息无分析，dedup 450s）
  → 澄清检查（有待澄清记录且非退出词 → 路由回安排提取链路；词表命中 = 新命令）
  → detectSlashCommand（斜杠命令白名单，确定性执行，不经 agent/daemon）
  → detectSpecialCommand（纪念日/假期，确定性直写 daemon --anniversary/--break/--schedule-change）
  → detectScheduleIntent（安排意图 → 提取→校验→写链路，agent 独立会话 chiguo-extract/chiguo-verify）
  → 聊天链：getAttention（daemon --attention）+ getMemories（daemon --memory-search）注入
    → agent-run --analysis-mode 一次完成分析+回复 → 有 analysis → upgradeAnalysis（--user-msg --analysis）
    → 回复文本发回微信

会话模型（并发隔离）：
  chiguo-main       = 回复侧（bridge 进程内 TurnQueue 串行）
  chiguo-send       = 主动发送（chiguo-tick 经 AGENTRUN_SESSION 注入 / RPC send 会话）
  chiguo-extract/verify/recall/replan = 安排意图链路（独立会话，与聊天上下文零共享）
  两进程不同会话 → 无跨进程并发 turn；bridge 进程内 TurnQueue 兜底回复侧自身串行
  /agent/prompt 发送侧端点（R20）与 askAgent 共用同一 TurnQueue —— 原实现直接调 __agentRpc.prompt()
  绕过队列，并发 HTTP turn 会交错同一会话 RPC 调用，现由 startSendServer(bot, queue) 透传统一约束
```

### 1.1 RPC 常驻（可选项，默认 cron tick；互斥切换）

```
形态 A（默认，保持上节）：cron tick 每次冷启动 spawn agent-run → agent（每消息/每 tick 全量初始化）

形态 B（RPC 常驻，CHIGUO_DAEMON_LOOP=1 部署）：
  systemd 常驻 3 进程：
    chiguo-bridge.service    —— 常驻 bridge（HTTP /send + /agent/prompt 端点）
    chiguo-daemon.service    —— .venv/bin/python chiguo_daemon.py --loop 900 --compact
                                （决策引擎常驻 + 发送侧内聚 _loop_send：生成→发送→记账）
    agent --mode rpc（双会话）—— 由 bridge 进程持有（agent-rpc.mjs 管理）
                                analysis 会话 chiguo-main / send 会话 chiguo-send
  回复链：bridge askAgent → AgentRpc.prompt(mode=analysis) → agent RPC（零 spawn）
  发送链：daemon --loop 内 _loop_send → POST /agent/prompt {mode:send} → bridge → AgentRpc
    → agent RPC → 回文本 → POST /send → bot.send() → record_send_text
  cron 仅剩 replan-tick（判脏轮询，几乎零成本）
  RPC 失败（任意环节）→ 自动回退 spawn（bridge askAgent 回退 agent-run；_loop_send 回退 spawn）
```

切换命令（防双发：cron tick 与 loop 常驻**必须互斥**，install_agent.sh 阶段 6c 处理）：
```bash
export CHIGUO_DAEMON_LOOP=1   # install_agent.sh 将：移除旧 tick crontab + 安装 chiguo-daemon.service
bash scripts/install_agent.sh --yes
# 回退 cron 形态：CHIGUO_DAEMON_LOOP=0 重跑 + systemctl disable --now chiguo-daemon.service
```

环境变量（bridge 侧，wechat-bridge.sh write_env 生成；**回复链 RPC 默认启用**，无需 CHIGUO_DAEMON_LOOP）：
```
WECHAT_BRIDGE_AGENT_RUN=$PROJECT_DIR/scripts/agent-run.mjs   # spawn 回退路径（未设置时默认仓库内 scripts/agent-run.mjs；启动时校验存在，缺失/被误删 → 明确报错退出）
WECHAT_BRIDGE_AGENT_RPC=1                                     # 1=回复链 RPC 优先（仅 RUNNER=agent 可用，失败自动回退 spawn）
WECHAT_BRIDGE_TOKEN=<随机 hex>                                # /send 与 /agent/prompt 共享 token（wechat-bridge.sh 生成，幂等保留）
# loop 形态下 daemon _loop_send 的 /send 鉴权（R17）：install_agent.sh 生成 chiguo-daemon.service
# 时注入 EnvironmentFile=-wechat-bridge/.env，systemd 常驻进程直接读同一份 WECHAT_BRIDGE_TOKEN
```

daemon `[loop]` 段（--loop 发送侧内聚用）：
```
[loop]
bridge_url = "http://127.0.0.1:18790"   # bridge HTTP 地址（含 /send 与 /agent/prompt）
bridge_token = ""                       # 回退 token（env WECHAT_BRIDGE_TOKEN 优先，不进 git）
agent_timeout_ms = 125000               # /agent/prompt 超时
```

HTTP 契约（bridge，仅本地回环 + 共享 token）：
```
POST /send           {"to","text"}                        → {"ok":true}（bot.send）
POST /agent/prompt   {"text","mode":"analysis|send"}      → {"ok":true,"text","analysis"?}
                     失败 → 503 {"ok":false,"error"}（调用方回退 spawn）
# 鉴权：#84 仅本地回环来源（Host/Origin）+ 可选 X-Bridge-Token；
#       WECHAT_BRIDGE_TOKEN 未配置时启动即 exit 1（强制鉴权；wechat-bridge.sh 自动生成随机 token）
```

### 1.2 daemon 通信契约（Python↔Node 无 socket 常驻）

Node/bridge 与 daemon 之间**无 socket 常驻进程**：全部经 CLI 子进程调用，stdout 输出 JSON 契约：

| 调用 | 用途 | 备注 |
|---|---|---|
| `--user-msg <原文>` | 记录消息（确定性，命令消息无分析） | recv_dedup 450s |
| `--user-msg <原文> --analysis '<JSON>'` | 分析升级记账（upgradeAnalysis） | 不重复记账 |
| `--user-msg <原文> --recv-id <uuid>` | 补报升级记账：bridge 本地生成的每条消息 uuid，同 id 只记一次(recv_dedup 精确去重)，不进 agent prompt | 与首次记账共用同一 id |
| `--attention` | 回复侧注意力注入（T1/T2/T3 + 情感快照） | 零写副作用，毫秒级 |
| `--memory-search <q>` | 回复侧记忆检索（mem0） | 失败降级跳过注入 |
| `--schedule-recall <q>` | 回忆检索 | 失败 ok:false + exit 1 |
| `--schedule-change <json>` | 写安排（reminder/cancel/move/add/exam_week/remove） | 畸形 JSON 不写入 |
| `--anniversary` | 纪念日增删查（add/list/remove） | buildReply 驱动 |
| `--break` | 假期开关（on/off/status） | manual_override 语义 |

daemon CLI 共 36 个参数（`--version --loop --user-msg --analysis --recv-id --user-msg-file --analysis-file --status
--compact --anniversary --break --health --attention --schedule-recall --schedule-change --memory-search --tune
--stats --alerts --monitor --consolidate --conversation --conversation-days --export --record-send --fallback
--text --trigger --intensity --send-result --send-status --error --alerts-all --ack --alerts-push --rotate`，
详见 doc/SYSTEM.md 七、CLI 参考）。**RPC 常驻仅存在于 node bridge ↔ agent 二进制之间**（agent-rpc.mjs），
与 Python daemon 无关。

## 二、人格注入（双层）

- **角色权威（archive 层）**：`personality/archive/SUN2.md` —— 迟菓唯一权威人格设定，与原著《日光雨》逐条对齐
  （L 行号可核实），是运行时人格规范的最终依据。原详版 `personality/archive/迟菓人格-详版.md` 仅作素材参考。
- **运行时注入（runtime 层）**：每次 agent 调用注入三段 `--append-system-prompt`：
  `personality/迟菓人格-精简版.md`（运行手册，唯一运行规范，含思考引导）+ `personality/记忆用法.md`
  （长期记忆使用规范）+ `personality/工具用法.md`（可用工具与调用时机）。
  runner=agent 时由 `buildBaseAgentArgs` 拼接（print 模式与 RPC 常驻共用）；runner=command 时
  agent-run.mjs 把三段拼进 `--prompt` 前缀，**保证换后端不丢人格**。
- `AGENTRUN_PERSONALITY/AGENTRUN_GUIDE/AGENTRUN_TOOLS` 可覆盖三段文件路径，仅测试/开发用；生产人格固定仓库内 `personality/`。

## 三、安装（install_agent.sh）

任意机器 pull 仓库后，agent 环境由 `scripts/install_agent.sh` 一键引导（幂等，deploy.sh 第 5.5 步接入）：

```bash
bash scripts/install_agent.sh --dry-run   # 只扫描报告（只读，非 TTY 默认也是它）
bash scripts/install_agent.sh --yes       # 自动完成全部（每次修改前 .bak 备份）
bash scripts/install_agent.sh             # 交互 ask：逐项确认
bash deploy.sh                         # 或随部署一起（传 --skip-agent 跳过）
```

模式与退出码约定：`0`=完成，`1`=有待办/警告/残留，`2`=严重问题。

| 阶段 | 内容 |
|------|------|
| 0 探测 | `pi --version`（缺失 → 严重）；`AGENT_API_KEY`/`OPENCODE_API_KEY` 可用性提示 |
| 0b 清理 | 移除已废弃 memory-lancedb-pro 扩展残留（v1.15 mem0 唯一后端；settings.json 条目 + 文件/目录，幂等） |
| 4 ollama | `curl localhost:11434/api/tags` 有 `qwen3-embedding:0.6b`（缺 → 提示/`ollama pull`） |
| 5 auth.json | `[host].provider` 条目（key 从 `AGENT_API_KEY`/`OPENCODE_API_KEY` 环境变量读，不落盘明文，chmod 600） |
| 6 crontab | 注册 `*/15 * * * * scripts/chiguo-tick.sh >> logs/cron-tick.log 2>&1`（幂等，活动旧条目整行替换；被注释禁用的手动停用条目原样保留，醒目提示 + ask 确认，绝不静默删除/恢复） |
| 6b crontab | 注册 replan-tick（判脏轮询，幂等） |
| 6c systemd | `CHIGUO_DAEMON_LOOP=1` 时安装 `chiguo-daemon.service`（--loop 900 --compact；与 cron tick 互斥） |
| 7 冒烟 | `pi -p --provider <[host].provider> --model <model> ...`（仅 --yes/ask） |

## 四、agent-run 契约（scripts/agent-run.mjs）

```bash
node scripts/agent-run.mjs --prompt <文本>            # 生成消息 → stdout JSON {"ok":true,"text":...}
node scripts/agent-run.mjs --prompt <文本> --analysis-mode  # 情绪分析 + 回复 → {"ok":true,"text","analysis"}
node scripts/agent-run.mjs --prompt <决策JSON> --send-mode  # 主动发送（决策 JSON 自足）
# 安排链路（§6.2 B2）：--schedule-extract/--schedule-verify/--schedule-recall/--schedule-replan
```

- **配置优先级**：`AGENTRUN_*` 环境变量 > toml `[host]` 段 > 默认值
  （AGENTRUN_PROVIDER/AGENTRUN_MODEL/AGENTRUN_THINKING/AGENTRUN_REPLY_THINKING/AGENTRUN_TIMEOUT/AGENTRUN_SESSION）
- **[host] 键**：`provider`（默认 opencode-go）、`model`（deepseek-v4-flash）、`thinking_level`（high）、
  `reply_thinking_level`（回复侧独立档位，缺省回退 thinking_level；交互路径要快，与主动发送/重分析互不拖累）、
  `session_id`（chiguo-main，回复侧）、`send_session_id`（chiguo-send，主动发送）、
  `runner`（agent/command，默认 agent）、`agent_command`（数组；runner=command 必填，AGENTRUN_AGENT_COMMAND 覆盖）、
  `wechat_bridge_url`（`http://127.0.0.1:18790/send`）
- **agent 参数**（仅 runner=agent）：`-p` 非交互 + `--provider/--model/--session-id` + `--no-context-files`
  （隔离仓库开发上下文）+ `--no-skills`（基础 analysis/send 路径与安排链路 extract/verify/recall/replan
  四会话统一禁用技能 —— R5 降权 #311：安排链路为知识边界纯文本契约，无工具调用场景，降权防注入→工具调用 F-A19-004×F-SEC-04）
  + `--mode json`（NDJSON 事件流）+ `--append-system-prompt` ×3（§二 三段）+ `--thinking`（analysis 用 reply_thinking_level）；
  runner=command 时忽略这些参数，改走 §九 统一契约
- **输出解析**：NDJSON 取最后一条 `message_end` 的 text 拼接；analysis-mode 提取 `<<ANALYSIS>>{...}<<END>>` 块
  （平衡括号解析，嵌套 JSON 不被首 `}` 截断）
- **失败语义**：`{"ok":false,"error":"..."}`；非零退出但 stdout 含完整回复 → salvage 不丢回复
- **stdout 字节上限（R19）**：Node `spawn` 忽略 `maxBuffer`，agent-run 手动按 `opts.maxBuffer ?? 16MB` 计数，
  超出即 SIGKILL 子进程并 reject（防无界输出累积拖垮 tick/bridge）；stderr 截断保留末 256KB
- **遥测**：一行一轮追加写 `logs/agent-run.log`（gitignore；/status 与验收依赖；`AGENTRUN_TELEMETRY=0` 跳过）
- 单测：`node tests/test_agent_run.mjs`（50 用例）

## 五、chiguo-tick（系统 crontab 入口）

- 入口 `scripts/chiguo-tick.sh`（+x）；注册/管理由 install_agent.sh 阶段 6 负责
- 流程见 §一；关键点：
  - `source scripts/agent-auth.sh` 解析 auth.json key → `OPENCODE_API_KEY`（opencode-go 优先 → [host].provider 回退）
  - idle 静默退出；send 走 `AGENTRUN_SESSION=<toml send_session_id>`（与会话分离）
  - 发送侧 RPC 优先（v1.11 B1）：`POST <bridge>/agent/prompt {text, mode:send}`，失败回退 spawn
  - agent 失败 → 5s 整链重试（U2）；仍失败中止发送（无 composer 兜底）+ agent_health 告警 / 手动恢复
  - 发送成败经 `scripts/agent_health.py record` 记账（transition 时告警/恢复）
  - curl 带 `--noproxy '*'`；发送失败仅记 stderr 并 `exit 0`（下个 tick 重试）；
    `--record-send` 回写发送状态（带 `--trigger`，失败不阻塞）
- 日志：`logs/cron-tick.log`

## 六、bridge 集成（回复侧路由 + 两套命令体系）

`wechat-bridge/bridge.mjs` 的 `handleMessage` 是回复侧唯一入口（OWNER_ID 门 + TurnQueue 串行），
路由顺序：**OWNER_ID 门 → recordUserMsg → 澄清检查 → 斜杠命令 → 特殊命令 → 安排意图 → 聊天链**。

- 非本人（非 OWNER_ID）：仅 askAgent 回复，不进写/回忆/追问/命令路径，不取 --attention，失败回通用文案（安全补钉）
- `recordUserMsg(text)`：确定性 `--user-msg` 回传 daemon（命令消息无分析，dedup 450s），失败不阻塞
- 澄清检查：有待澄清记录且非退出词 → 路由回安排提取链路（词表命中 = 新命令；否则合并"原意+回答"）
- 进程内 `TurnQueue` 串行 agent 调用（同一会话 chiguo-main 不允许并发 turn）
- 环境变量：`WECHAT_BRIDGE_AGENT_RUN`（默认仓库内 agent-run.mjs）、`WECHAT_BRIDGE_DAEMON_PY`、
  `WECHAT_BRIDGE_DAEMON`、`WECHAT_BRIDGE_OWNER`、`WECHAT_BRIDGE_SEND_PORT`、`WECHAT_BRIDGE_STORAGE`、
  `WECHAT_BRIDGE_MEMORY_PY`/`WECHAT_BRIDGE_MEMORY_CLI`（斜杠命令记忆 CLI：解释器/argv；默认 `.venv/bin/python -m memory`）、
  `WECHAT_BRIDGE_ACTIVITY_FILE`/`CHIGUO_ACTIVITY_FILE`（轮换活动时间戳覆盖，测试隔离用）
- 会话轮换配置在 toml `[host].session_rotate_*`：`enabled`（默认 true）、`check_minutes`（默认 60）、
  `idle_minutes`（默认 60）；send 每轮全新由 bridge `/agent/prompt` + `AGENTRUN_ROTATE_SESSION=1` 实现（§6.1 下方）
- 测试：`tests/test_agent_rpc.mjs`、`tests/test_bridge_agent_http.mjs`、`tests/test_bridge_askagent_rpc.mjs`、
  `tests/test_bridge_askagent.mjs`、`tests/test_bridge_cmd.mjs`、`tests/test_bridge_health.mjs`、`tests/test_bridge_rotate.mjs`、
  `tests/test_bridge_schedule.mjs`

### 6.1 命令体系 A：斜杠命令（ai 会话命令，detectSlashCommand）

全部 `/` 开头消息由 bridge 确定性接管（白名单制），**纯 node 侧执行（文件操作 + 记忆 CLI），不经 agent 也不经 daemon**：

| 命令 | 作用 | 执行 |
|---|---|---|
| `/new` | 清空当前对话上下文（记忆保留） | 备份最近一个 chiguo-main 会话文件到 `~/.chiguo/session-backups/<ts>-chiguo-main.jsonl`；RPC 模式下同时重启常驻会话 |
| `/status` | 上下文/缓存/记忆状态（纯技术） | 读 `logs/agent-run.log` 最后一行遥测 + 会话文件大小 + `python -m memory --stats` |
| `/记忆` | 记忆库统计 | `python -m memory --stats`（mem0） |
| `/记得什么 <词>` | 搜索记忆 | `python -m memory --search <词>` |
| `/help` | 命令列表 | 本表 |

其余任何 `/` 开头消息 → "这是什么咒语啦？我可不会~"，不进 LLM。用户侧说明见 [doc/微信命令.md](微信命令.md)。

### 6.2 命令体系 B：规则化自然语言命令（detectSpecialCommand / detectScheduleIntent）

**B1 纪念日/假期（detectSpecialCommand，确定性直写 daemon，零 LLM）**：

| 哥哥说 | 执行 |
|--------|------|
| 记住X月X日(是)XX | `--anniversary "add anniversary MM-DD <名称>"` |
| YYYY年X月X日(是/为/要)XX | `--schedule-change {kind: reminder, when:{date:YYYY-MM-DD}, label}`（显式日期直转写） |
| X月X日要XX（无年份） | `--schedule-change {kind: reminder}`（inferYear：今年已过 → 明年，CST） |
| 有哪些纪念日 / 纪念日列表 | `--anniversary list` |
| 放假了 / 放暑假了 | `--break on` |
| 开学了 | `--break off` |

**防误伤约束**（歧义交 agent 自然回复）：消息 ≤40 字；末尾带 吗/？/? 的问句不拦截；
`你/您` 开头的对 bot 提问不拦截；语义反转守卫（残余文本含征求/疑问/否定词 → 放行 agent）。
执行后 bridge 回迟菓风确认文案（daemon JSON 驱动），失败回「处理失败：<原因>」。

> ⚠️ **裸「放假了」= 无限期假期**：`--break on` 置 `manual_override=True`（无限期，直到手动关闭，
> availability 恒 0.85，chiguo_monitor.py 会持续告警）。误触发后执行 `--break off` 或
> `--anniversary "remove <id>"` 式手动关闭：`uv run python chiguo_daemon.py --break off`。

**B2 安排意图（detectScheduleIntent，词表命中 → 提取→校验→写链路）**：

| 词表（子串命中） | intent | 链路 |
|---|---|---|
| 停课/不上课 | cancel | 提取→校验→`--schedule-change` |
| 调课/改到/调到 | move | 同上 |
| 加课/补课 | add | 同上 |
| 取消/撤销 | remove | 同上 |
| 考试周 | exam_week | 同上 |
| 记住/记得/提醒、X月X日[日号] | reminder/extract | 同上 |

链路（handleScheduleCommand，均经 TurnQueue，180s 超时不阻塞队列）：
`extract`（agent-run `--schedule-extract`，独立会话 chiguo-extract，180s，带 --attention）→
`verify`（agent-run `--schedule-verify`，独立会话 chiguo-verify，180s）→
`daemon --schedule-change <item>`（30s 写入）。
信息不足 → 追问文案写入澄清记录（`schedule_clarify.json`，6h 过期），下条消息补充回答路由回链路；
`not_command` 误命中 → 释放回聊天链（消息仍获回复，不静默丢弃）。

### 6.3 两套命令体系边界

- **共同点**：均由 bridge 确定性接管，命中即不进聊天链、不污染 chiguo-main 会话；全部仅 OWNER_ID 可达；
  检测保守（短消息 + 非问句 + 锚定词），歧义消息放行 agent 自然回复。
- **斜杠命令 = 会话级确定性命令**：0 次 LLM + 0 次 daemon，纯 node 执行（文件 + 记忆 CLI）；
  白名单制，未知 `/` 开头一律拒绝。治理 ai 会话状态（清空/查看/记忆）。
- **规则化自然语言命令 = 领域级确定性命令**：纪念日/假期 **0 次 LLM + 1 次 daemon 调用**（直写）；
  安排意图 **2 次 agent 独立会话（chiguo-extract/verify）+ 1 次 daemon 写入**，且用独立会话
  与聊天上下文零共享。治理领域状态（纪念日/假期/课表/提醒）。
- **分级总览**：斜杠命令（0 LLM）→ 特殊命令（0 LLM + daemon）→ 安排意图（agent 独立会话 + daemon）→ 聊天链
  （1 次 agent，chiguo-main）。

## 七、provider key 配置

- agent 读 `~/.pi/agent/auth.json` 的 **`[host].provider` 名**条目（`{"type":"api_key","key":...}`，chmod 600；键名 = provider 名，opencode-go 为默认示例）
- 写入途径：`export AGENT_API_KEY=... && bash scripts/install_agent.sh --yes`（阶段 5；兼容回退 `OPENCODE_API_KEY`）
- key **不落盘明文到仓库**；`chiguo_envcheck.py` 的 `check_agent_auth` 校验该条目存在且有真值（自动跟随 toml provider）

## 八、接入任意模型 API（provider 可配）

chiguo 对后端模型不做绑定：**消息生成/情绪分析全部走 agent 后端，provider 由 `[host].provider` 单一来源决定**
（= `pi --provider` 名与 auth.json 键名）。opencode-go 只是默认示例，可换成 agent 支持的任何接入方式：

**方式 A：内置 provider（零代码）**

1. 配 key（chiguo 工具链以 auth.json 为唯一校验源）：
   - `pi` 交互式 `/login <provider>` 存入 `~/.pi/agent/auth.json`（键名 = provider 名；agent 官方方式）
   - 或 `export AGENT_API_KEY=<provider 的 key> && bash scripts/install_agent.sh --yes`（阶段 5 写入；兼容回退 `OPENCODE_API_KEY`）
   - 注：`DEEPSEEK_API_KEY`/`OPENAI_API_KEY` 等 provider 专用环境变量对 agent 运行时有效，但 install/envcheck 只认 auth.json——两者都配才全绿
2. 改 toml：
   ```toml
   [host]
   provider = "openai"          # 或 deepseek / anthropic / google / openrouter …
   model = "gpt-5"              # 或 provider/id 前缀
   ```
3. 重启相关链路（bridge `bash scripts/wechat-bridge.sh restart`；tick 随 crontab 每 15 分钟新进程生效）；
   `uv run python chiguo_envcheck.py` 复核（agent_auth 检查自动跟随 provider）

**方式 B：自定义 OpenAI 兼容端点（自建网关/私有部署，agent 官方 models.json 机制）**

写 `~/.pi/agent/models.json`（示例：本地 ollama/vLLM/LM Studio 或任意 OpenAI 兼容网关）：

```json
{
  "providers": {
    "my-gateway": {
      "baseUrl": "http://192.168.1.10:8000/v1",
      "api": "openai-completions",
      "apiKey": "$MY_GATEWAY_KEY",
      "models": [{ "id": "qwen2.5-coder:7b" }]
    }
  }
}
```

然后 toml `[host] provider = "my-gateway"` + `model = "qwen2.5-coder:7b"`。更复杂场景（代理/特殊鉴权/OAuth）
用 agent 扩展 `pi.registerProvider()`，见 agent 官方 [custom-provider.md](https://github.com/earendil-works/pi-mono/blob/main/docs/custom-provider.md)。

**注意事项**

- `chiguo-tick.sh` / `wechat-bridge.sh` 注入的 `OPENCODE_API_KEY`（mem0 事实提取固定用 opencode-go key）**优先取
  auth.json 的 `opencode-go` 条目**（mem0 事实提取端点固定 opencode 网关），无该条目时回退 `[host].provider` 条目
  （best effort）——换对话 provider 无需改脚本；若不再有 opencode-go key，mem0 事实提取不可用（envcheck 记忆层报 warn/critical）
- install_agent.sh 的 auth 写入与冒烟自动跟随 `[host].provider`（key 环境变量用通用名 `AGENT_API_KEY`，兼容回退 `OPENCODE_API_KEY`）
- 换 provider 后会话记忆（chiguo-main/chiguo-send）保留；模型能力差异（thinking 档位等）按 agent 侧 model 配置生效

## 九、接入自定义 agent（runner=command）

v1.8 起 agent 后端可任意替换：`scripts/agent-run.mjs` 抽象 agent runner，`[host].runner` 决定实现——
`agent`（默认，agent 后端二进制）或 `command`（任意 CLI agent，如自定义 node/python 脚本或本地推理进程）。

**配置**

```toml
[host]
runner = "command"                            # 切到任意 CLI agent
agent_command = ["node", "/path/to/agent.mjs"]  # 必填：可执行命令 + 固定参数（数组）
```

环境变量覆盖：`AGENTRUN_RUNNER`（agent/command）与 `AGENTRUN_AGENT_COMMAND`（JSON 数组字符串）优先于 toml。

**统一契约**（runner=command 时，agent-run.mjs 对每次调用执行）：

```
<agent_command> --prompt <完整提示词> --mode <analysis|send|extract|verify|recall|replan>
```

- `--prompt` 是**完整提示词**：agent-run.mjs 按模式模板构造（发送/分析沿用 §二 三段人格前缀 +
  对应模板；安排链路 extract/verify/recall/replan 复用 agent 提示词模板），agent 无需自行拼装
- `--mode` 语义与 agent 路径一一对应：`send` 生成消息、`analysis` 情绪分析+回复、
  `extract`/`verify`/`recall`/`replan` 安排澄清链路
- **stdout 契约**：单行 JSON `{"ok":true,"text":"...","analysis":{...},"parsed":{...},"raw":"..."}`
  （失败时 `{"ok":false,"error":"..."}`；也兼容 agent 的 NDJSON 输出，`parseAgentOutput` 兜底解析）。
  非零退出但 stdout 含完整回复 → salvage 不丢回复（与 agent 路径一致）
- 任意语言/运行时皆可：只需读 `--prompt`/`--mode` 参数、向 stdout 输出 JSON

**限制与运维**

- bridge 的 **RPC 常驻模式仅 `runner=agent` 可用**（`WECHAT_BRIDGE_AGENT_RPC=1`）；command 模式下
  bridge askAgent 走进程内 `TurnQueue` 串行调用 agent-run.mjs（与 agent 路径一致）
- `chiguo_envcheck.py` 的 `check_agent` 支持 `runner`/`agent_command` 参数：runner=command 时检查
  agent_command 可执行性（不再要求 agent 二进制）
- 失败排查：askAgent 报「⚠️ 处理失败」时，除 bridge 日志（logs/wechat-bridge.log）外，手跑
  `<agent_command> --prompt '测试' --mode send` 直接看 agent 自身输出/日志

## 十、记忆后端（[memory].backend）

v1.8 起记忆模块解耦为 `memory/` 包（v1.8 的根目录兼容门面 `memory_bridge.py` 已删除；CLI 下沉为 `python -m memory`）。
**mem0 是唯一记忆后端**（v1.15 已移除 memory-lancedb-pro 扩展，install_agent.sh 阶段 0b 幂等清理残留）——
`[memory].backend` 仅 `mem0` / `auto`（遗留同义）合法，其他值抛 ValueError；
`MemoryBackend` 抽象基类保留作内部测试桩/复用层：

**MemoryBackend 四原语**（子类实现；不可用 → 查询返回空，不抛）：

```python
class MyBackend(MemoryBackend):
    @property
    def available(self) -> bool: ...                       # 后端是否可用
    def search(self, query, limit=10, category=None,
               min_importance=0.3) -> list[dict]: ...      # 关键词检索 → 统一行契约 dict
    def random_memory(self, category=None, min_importance=0.5,
                      prefer_categories=None) -> dict | None: ...  # 加权随机
    def stats(self) -> dict: ...                           # 统计（total/user_relevant/available…）
```

- **Ebbinghaus 在基类**：`ebbinghaus_weight`/`search_with_forgetting`/`user_relevant_with_forgetting`/
  `random_memory_with_forgetting` 由基类基于原语包装（R = e^(-t/(S×importance))，S=168h、min_weight=0.1）
- 行契约（search/random_memory 返回 dict 字段）：id/text/category/scope/importance/timestamp/datetime/
  memory_category/l0_abstract/l2_content/tier/source；importance 必须清洗为非 NaN
- mem0 配置见 `chiguo_proactive.toml` `[memory]` 段（qdrant 嵌入式 `data/mem0/` + ollama 本地 embedding
  `qwen3-embedding:0.6b`；事实提取 LLM 默认读 auth.json 的 opencode-go key）

## 十一、故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `pi exited 1: ... No API key found` | auth.json 无 [host].provider 对应条目 | install_agent.sh 阶段 5（AGENT_API_KEY/OPENCODE_API_KEY） |
| `401 Unauthorized` | provider key 失效 | 换 key 重写 auth.json；`chiguo_envcheck.py` 复核 |
| `{"ok":false,"error":"empty reply"}` | agent 无 message_end 文本（空回复/坏 JSON） | 重试；检查 provider/model 是否可生成中文文本；`pi -p --provider <[host].provider> ... --mode json '测试'` 手动验证 |
| 超时（120s kill） | 网关慢/thinking 过高 | 调低 `[host].thinking_level`/`reply_thinking_level`（off/minimal/low/medium/high/xhigh/max） |
| `[chiguo-tick] agent-run 未生成消息` | agent-run 失败（多数是 key/网络） | 看 logs/cron-tick.log；先手动跑一次 agent-run 复现 |
| bridge 回复「⚠️ 处理失败」 | askAgent 抛错（agent-run 非 JSON/失败） | bridge 日志（logs/wechat-bridge.log）看具体 error |
| 特殊命令回「处理失败」 | daemon CLI 报错（如日期格式错） | 命令 JSON 输出含 error；对照 §6.2 B1 命令表手跑验证 |
| 安排链路回「处理失败，再试一次？」 | extract/verify 超时（180s）或块解析失败 | 手跑 `node scripts/agent-run.mjs --prompt '<原文>' --schedule-extract --attention '{}'` 看 agent 输出；澄清记录见 `schedule_clarify.json` |
| command runner 下 askAgent 失败/回「⚠️ 处理失败」 | agent 脚本自身报错（非 JSON/非零退出/脚本缺失） | 手跑 `<agent_command> --prompt '测试' --mode send` 看 agent stdout/日志；核对 `AGENTRUN_RUNNER`/`AGENTRUN_AGENT_COMMAND` 生效配置与 `[host].agent_command` |
| envcheck 记忆层 warn/critical | 无 opencode-go key → mem0 事实提取不可用 | 补配 auth.json 的 opencode-go 条目（见 §八 注意事项） |

## 十二、维护速查

```bash
# 手动决策 + 生成 + 发送链路（分步）
uv run python chiguo_daemon.py --compact          # 决策（idle 输出最小 JSON）
node scripts/agent-run.mjs --prompt '<决策 JSON>' --send-mode   # 生成消息
bash scripts/chiguo-tick.sh                       # 全链路（idle 静默 0）

# 手动验证回复侧注入（只读）
uv run python chiguo_daemon.py --attention        # 注意力注入块
uv run python chiguo_daemon.py --memory-search '火锅'   # 记忆检索
uv run python chiguo_daemon.py --anniversary list # 纪念日列表
uv run python chiguo_daemon.py --break status     # 假期状态

# 会话/并发检查
#   chiguo-main：bridge 回复（TurnQueue 串行）
#   chiguo-send：tick 主动发送（AGENTRUN_SESSION 注入）
#   chiguo-extract/verify/recall/replan：安排意图链路（与聊天上下文零共享）
#   同会话并发 turn 在 agent 侧可能交错 → 两条链路永不共用会话

# 环境检查
uv run python chiguo_envcheck.py

# 测试
node tests/test_agent_run.mjs && node tests/test_agent_rpc.mjs && node tests/test_bridge_agent_http.mjs && node tests/test_bridge_askagent_rpc.mjs && node tests/test_bridge_askagent.mjs && node tests/test_bridge_cmd.mjs && node tests/test_bridge_health.mjs && node tests/test_bridge_schedule.mjs && \
bash tests/test_install_agent.sh --dry-run && \
bash tests/test_wechat_bridge.sh && uv run python tests/test_*.py   # 全量见 AGENTS.md
```
