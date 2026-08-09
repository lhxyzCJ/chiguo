# agent 后端集成指南（Phase 4，v1.8→v1.11 契约冻结）

> 本文件为 agent 后端集成契约（原 agent 后端集成指南，#99 全库 pi→agent 重命名）。
> **版本不步进（用户确认）：VERSION 保持 1.10**，本文档重命名不改变行为语义。

## 命名契约（#99 冻结）

### 文件映射（git mv 保留历史）
| 旧名 | 新名 |
|---|---|
| `scripts/pi-run.mjs` | `scripts/agent-run.mjs`（唯一 agent 调用层） |
| `wechat-bridge/pi-rpc.mjs` | `wechat-bridge/agent-rpc.mjs` |
| `scripts/pi_health.py` | `scripts/agent_health.py` |
| `scripts/install_pi.sh` | `scripts/install_agent.sh` |
| `scripts/pi-auth.sh` | `scripts/agent-auth.sh` |
| `tests/test_agent_run.mjs` | `tests/test_agent_run.mjs` |
| `tests/test_pi_health.py` | `tests/test_agent_health.py` |
| `tests/test_bridge_askagent.mjs` | `tests/test_bridge_askagent.mjs` |
| `tests/test_install_pi.sh` | `tests/test_install_agent.sh` |
| `doc/PI_INTEGRATION.md` | `doc/AGENT_INTEGRATION.md`（本文件） |
| `pi_health.json(.lock)` | `agent_health.json(.lock)`（运行时） |
| `logs/pi-run.log` | `logs/agent-run.log`（运行时） |

### env 映射
`PIRUN_*`→`AGENTRUN_*`（AGENTRUN_RUNNER/PROVIDER/MODEL/THINKING/REPLY_THINKING/TIMEOUT/SESSION/NEW_SESSION/PERSONALITY/GUIDE/TOOLS/TELEMETRY/AGENT_COMMAND）、`PI_BIN`→`AGENT_BIN`、`PI_TIMEOUT`→`AGENT_TIMEOUT`、`PI_RUN_SCRIPT`→`AGENT_RUN_SCRIPT`、`WECHAT_BRIDGE_AGENT_RUN/RPC/HEALTH/HEALTH_PY`→`WECHAT_BRIDGE_AGENT_*`、`PI_FALLBACK_PROVIDER`→`AGENT_FALLBACK_PROVIDER`、`PI_KEY`→`AGENT_KEY`、`PI_MODE_FILE`→`AGENT_MODE_FILE`（测试用）。

### 标识符映射
`askAgent`→`askAgent`、`runPiRun`→`runAgentRun`、`PiRpc`→`AgentRpc`、`check_pi`→`check_agent`、`pi_bin`→`agent_bin`、`RUNNER==='pi'`→`'agent'`、`PI_RPC_ENABLED`→`AGENT_RPC_ENABLED`。

### CLI/配置映射
`--skip-pi`→`--skip-agent`（deploy.sh/envcheck 用户可见参数）、`[host].runner="pi"`→`"agent"`（默认值 = pi-agent 二进制，行为不变）。

### 豁免清单（产品名，grep 允许残留）
`~/.pi/`、`~/.pi-agent/`、pi-agent 二进制名 `pi`（`AGENT_BIN` 默认值 `'pi'`、`check_agent(agent_bin="pi")`）、`pi --provider/--model/--session-id` 子命令、`pi_health` 相关历史文档引用。
**误匹配排除**：`topics`/`api`/`_pi` 等普通子串（chiguo_topics.py、schedule/api.py、tests/test_topics.py 等）。
> 架构/能力描述一律用「agent 后端」表述（不点名具体 agent 实现）；仅操作性命令/路径保留具体名（如上豁免）。

> 寄主迁移后的当前架构：**消息生成与情绪分析全部走 agent 后端**（v1.8 起 runner 可替换：默认 runner=agent 走 agent 后端二进制，provider 可配，opencode-go 为默认示例；
> 定时触发走系统 crontab（chiguo-tick），微信收发走 wechat-bridge，记忆走 mem0（data/mem0/，qdrant 嵌入式 + ollama 本地 embedding）

## 架构总览

```
发送侧（系统 crontab 门控，零 idle 模型调用）：
crontab */15 * * * * scripts/chiguo-tick.sh
  → .venv/bin/python chiguo_daemon.py --compact（零 LLM 评估）
    ├─ action=idle → 静默退出（~90% 的评估不唤醒 LLM）
    └─ action=send → 发送侧 RPC 优先（v1.11 默认）：POST <bridge>/agent/prompt {text:决策 JSON, mode:send}
        → 常驻 agent RPC 生成消息；RPC 失败自动回退 node scripts/agent-run.mjs --prompt <决策 JSON>（AGENTRUN_SESSION=chiguo-send）
        → 回文本后 curl --noproxy '*' -X POST <toml [host].wechat_bridge_url> {"to","text"} → bridge → bot.send()
        → daemon --record-send <msg_id> --text <text> 回写（幂等）

回复侧（bridge 内联，agent 单次调用完成分析+回复）：
微信消息到达 → bridge 先确定性 daemon --user-msg <原文>（无分析）
  → detectSpecialCommand 检测纪念日/假期命令（命中 → 直接执行 daemon --anniversary/--break 并回复，不经 agent）
  → 否则 agent-run.mjs --prompt <原文> --analysis-mode → <<ANALYSIS>>{情绪 JSON}<<END>> + 回复文本
  → 有 analysis → daemon --user-msg <原文> --analysis '<JSON>'（recv_dedup 升级，不重复记账）
  → 回复文本发回微信

会话模型（并发隔离）：
  chiguo-main  = 回复侧（bridge 进程内 TurnQueue 串行）
  chiguo-send  = 主动发送（chiguo-tick.sh 经 AGENTRUN_SESSION 注入）
  两进程不同会话 → 无跨进程并发 turn；bridge 进程内 TurnQueue 兜底回复侧自身串行
```

### v1.11 RPC 常驻（可选项，默认 cron tick；互斥切换）

```
形态 A（默认，保持上节）：cron tick 每次冷启动 spawn agent-run → agent（每消息/每 tick 全量初始化）

形态 B（RPC 常驻，CHIGUO_DAEMON_LOOP=1 部署）：
  systemd 常驻 3 进程：
    chiguo-bridge.service    —— 常驻 bridge（HTTP /send + /agent/prompt 端点）
    chiguo-daemon.service    —— .venv/bin/python chiguo_daemon.py --loop 900 --compact
                                （决策引擎常驻 + 发送侧内聚 _loop_send：生成→发送→记账）
    pi --mode rpc（双会话）  —— 由 bridge 进程持有（agent-rpc.mjs 管理）
                                analysis 会话 chiguo-main / send 会话 chiguo-send
  回复链：bridge askAgent → AgentRpc.prompt(mode=analysis) → agent RPC（零 spawn）
  发送链：daemon --loop 内 _loop_send → POST /agent/prompt {mode:send} → bridge → AgentRpc
    → agent RPC → 回文本 → POST /send → bot.send() → record_send_text
  cron 仅剩 replan-tick（判脏轮询，几乎零成本）
  RPC 失败（任意环节）→ 自动回退 spawn（bridge askAgent 回退 agent-run；_loop_send 回退 spawn）
```

切换命令（防双发：cron tick 与 loop 常驻**必须互斥**，install_agent.sh 阶段 6 处理）：
```bash
export CHIGUO_DAEMON_LOOP=1   # install_agent.sh 将：移除旧 tick crontab + 安装 chiguo-daemon.service
bash scripts/install_agent.sh --yes
# 回退 cron 形态：CHIGUO_DAEMON_LOOP=0 重跑 + systemctl disable --now chiguo-daemon.service
```

环境变量（bridge 侧，wechat-bridge.sh write_env 生成；**回复链 RPC 默认启用**，无需 CHIGUO_DAEMON_LOOP）：
```
WECHAT_BRIDGE_AGENT_RUN=$PROJECT_DIR/scripts/agent-run.mjs   # spawn 回退路径
WECHAT_BRIDGE_AGENT_RPC=1                                     # 1=回复链 RPC 优先（失败自动回退 spawn）
WECHAT_BRIDGE_TOKEN=<随机 hex>                                # /send 与 /agent/prompt 共享 token（wechat-bridge.sh 生成，幂等保留）
```

daemon `[loop]` 段（--loop 发送侧内聚用）：
```
[loop]
bridge_url = "http://127.0.0.1:18790"   # bridge HTTP 地址
bridge_token = ""                       # 回退 token（env WECHAT_BRIDGE_TOKEN 优先，不进 git）
agent_timeout_ms = 125000               # /agent/prompt 超时
```

HTTP 契约（bridge，仅本地回环 + 共享 token）：
```
POST /send           {"to","text"}                        → {"ok":true}（bot.send）
POST /agent/prompt   {"text","mode":"analysis|send"}      → {"ok":true,"text","analysis"?}
                     失败 → 503 {"ok":false,"error"}（调用方回退 spawn）
```

## 一、安装（install_agent.sh）

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
| 0 探测 | `pi --version`（缺失 → 严重）；`OPENCODE_API_KEY` 可用性提示 |
| 4 ollama | `curl localhost:11434/api/tags` 有 `qwen3-embedding:0.6b`（缺 → 提示/`ollama pull`） |
| 5 auth.json | `[host].provider` 条目（key 从 `AGENT_API_KEY`/`OPENCODE_API_KEY` 环境变量读，不落盘明文，chmod 600） |
| 6 crontab | 注册 `*/15 * * * * scripts/chiguo-tick.sh >> logs/cron-tick.log 2>&1`（幂等，旧条目整行替换） |
| 7 冒烟 | `pi -p --provider <[host].provider> ...`（仅 --yes/ask） |

## 二、agent-run 契约（scripts/agent-run.mjs）

```bash
node scripts/agent-run.mjs --prompt <文本>            # 生成消息 → stdout JSON {"ok":true,"text":...}
node scripts/agent-run.mjs --prompt <文本> --analysis-mode  # 情绪分析 + 回复 → {"ok":true,"text","analysis"}
```

- **配置优先级**：`AGENTRUN_*` 环境变量 > toml `[host]` 段 > 默认值
  （AGENTRUN_PROVIDER/AGENTRUN_MODEL/AGENTRUN_THINKING/AGENTRUN_SESSION；AGENTRUN_PERSONALITY/AGENTRUN_GUIDE 仅测试/开发用，生产人格固定仓库内 personality/）
- **[host] 键**：`provider`（默认 opencode-go）、`model`（deepseek-v4-flash）、`thinking_level`（high）、
  `session_id`（chiguo-main，回复侧）、`send_session_id`（chiguo-send，主动发送）、
  `runner`（agent/command，默认 agent；v1.8 agent runner 抽象，见 §八）、`agent_command`（数组；runner=command 必填，AGENTRUN_AGENT_COMMAND 覆盖）、
  personality 固定仓库内 personality/（[host].personality_dir 已移除，见 DEPLOYMENT.md；注入 SUN2.md + 迟菓语言技巧指南.md）、
  `wechat_bridge_url`（`http://127.0.0.1:18790/send`）
- **agent 参数**（仅 runner=agent）：`-p` 非交互 + `--no-context-files`（隔离仓库开发上下文）+ `--mode json`（NDJSON 事件流）
  + `--append-system-prompt` ×2（SUN2 + 语言技巧指南）+ `--session-id <会话>` + `--thinking`；
  runner=command 时忽略这些参数，改走 §八 统一契约
- **输出解析**：NDJSON 取最后一条 `message_end` 的 text 拼接；analysis-mode 提取
  `<<ANALYSIS>>{...}<<END>>` 块
- **失败语义**：`{"ok":false,"error":"..."}`；非零退出但 stdout 含完整回复 → salvage 不丢回复
- 单测：`node tests/test_agent_run.mjs`（19 用例）

## 三、chiguo-tick（系统 crontab 入口）

- 入口 `scripts/chiguo-tick.sh`（+x）；注册/管理由 install_agent.sh 阶段 6 负责
- 流程见架构图；关键点：idle 静默退出；send 走 `AGENTRUN_SESSION=chiguo-send`（与会话分离）；
  curl 带 `--noproxy '*'`；发送失败仅记 stderr 并 `exit 0`（下个 tick 重试）；
  `--record-send` 回写发送状态（失败不阻塞）
- 日志：`logs/cron-tick.log`

## 四、bridge askAgent（回复侧）

`wechat-bridge/bridge.mjs`（v4）：

- 消息到达 → `recordUserMsg(text)`（daemon `--user-msg`，确定性，失败不阻塞）
- → `detectSpecialCommand(text)`（特殊命令，见 §五；命中 → 执行 daemon 并回复，**不经 agent**）
- → `askAgent(text)`（agent-run `--analysis-mode`，一次完成分析+回复；进程内 `TurnQueue` 串行 agent 调用）
- → `upgradeAnalysis(text, analysis)`（daemon `--user-msg --analysis`，recv_dedup 升级语义）
- → `bot.reply(msg, reply)`
- 环境变量：`WECHAT_BRIDGE_AGENT_RUN`（默认仓库内 agent-run.mjs）、`WECHAT_BRIDGE_DAEMON_PY`、
  `WECHAT_BRIDGE_DAEMON`、`WECHAT_BRIDGE_OWNER`、`WECHAT_BRIDGE_SEND_PORT`、`WECHAT_BRIDGE_STORAGE`
- 测试：`node tests/test_agent_rpc.mjs`（7 用例）、`node tests/test_bridge_agent_http.mjs`（5 用例）、`node tests/test_bridge_askagent_rpc.mjs`（2 用例）、`node tests/test_bridge_askagent.mjs`（17 用例）、`node tests/test_bridge_cmd.mjs`（43 用例）

## 五、特殊命令（纪念日/假期，方案 A：bridge 规则化）

纪念日/假期指令由 **bridge 确定性接管**（agent 纯文本调用无工具权限，
不依赖 agent 输出稳定性）。`wechat-bridge/command-detect.mjs` 在消息到达时正则检测：

| 哥哥说 | 执行 |
|--------|------|
| 记住X月X日(是)XX | `chiguo_daemon.py --anniversary "add anniversary MM-DD <名称>"` |
| YYYY年X月X日(是/为/要)XX | `--anniversary "add countdown YYYY-MM-DD <名称>"` |
| X月X日要XX（无年份） | `--anniversary "add countdown <推断年份>-MM-DD <名称>"`（已过 → 明年，CST） |
| 有哪些纪念日 / 纪念日列表 | `--anniversary list` |
| 放假了 / 放暑假了 / 我放假了 | `--break on` |
| 开学了 / 我开学了 | `--break off` |

**防误伤约束**（歧义交 agent 自然回复）：消息 ≤40 字；末尾带 吗/？/? 的问句不拦截；
`你/您` 开头的对 bot 提问不拦截；`今天放假了` 等一天性陈述不拦截。
执行后 bridge 回迟菓风确认文案（daemon JSON 驱动），失败回「处理失败：<原因>」。

> ⚠️ **裸「放假了」= 无限期假期**：`--break on` 置 `manual_override=True`（无限期，直到手动关闭，
> availability 恒 0.85，chiguo_monitor.py 会持续告警）。误触发后执行 `--break off` 或
> `--anniversary "remove <id>"` 式手动关闭：`uv run python chiguo_daemon.py --break off`。

## 六、provider key 配置

- agent 读 `~/.pi/agent/auth.json` 的 **`[host].provider` 名**条目（`{"type":"api_key","key":...}`，chmod 600；键名 = provider 名，opencode-go 为默认示例）
- 写入途径：`export AGENT_API_KEY=... && bash scripts/install_agent.sh --yes`（阶段 5；兼容回退 `OPENCODE_API_KEY`）
- key **不落盘明文到仓库**；`chiguo_envcheck.py` 的 `check_pi_auth` 校验该条目存在且有真值（自动跟随 toml provider）

## 七、接入任意模型 API（provider 可配）

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
   `uv run python chiguo_envcheck.py` 复核（pi_auth 检查自动跟随 provider）

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

- `chiguo-tick.sh` / `wechat-bridge.sh` 注入的 `OPENCODE_API_KEY`（memory 扩展 smart extraction 固定 env 名）**优先取
  auth.json 的 `opencode-go` 条目**（扩展 json5 llm 端点固定 opencode 网关），无该条目时回退 `[host].provider` 条目
  （best effort）——换对话 provider 无需改脚本；若不再有 opencode-go key，smart extraction 降级为正则（agent 启动日志可见）
- install_agent.sh 的 auth 写入与冒烟自动跟随 `[host].provider`（key 环境变量用通用名 `AGENT_API_KEY`，兼容回退 `OPENCODE_API_KEY`）
- 换 provider 后会话记忆（chiguo-main/chiguo-send）保留；模型能力差异（thinking 档位等）按 agent 侧 model 配置生效

## 八、接入自定义 agent（runner=command）

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

- `--prompt` 是**完整提示词**：agent-run.mjs 按模式模板构造（发送/分析沿用 SUN2.md 人格注入，
  安排链路 extract/verify/recall/replan 复用原 agent 提示词模板），agent 无需自行拼装
- `--mode` 语义与 agent 路径一一对应：`send` 生成消息、`analysis` 情绪分析+回复、
  `extract`/`verify`/`recall`/`replan` 安排澄清链路
- **stdout 契约**：单行 JSON `{"ok":true,"text":"...","analysis":{...},"parsed":{...},"raw":"..."}`
  （失败时 `{"ok":false,"error":"..."}`）；也兼容 agent 的 NDJSON 输出（`parseAgentOutput` 兜底解析）。
  非零退出但 stdout 含完整回复 → salvage 不丢回复（与 agent 路径一致）
- 任意语言/运行时皆可：只需读 `--prompt`/`--mode` 参数、向 stdout 输出 JSON

**限制与运维**

- bridge 的 **RPC 常驻模式仅 `runner=agent` 可用**（`WECHAT_BRIDGE_AGENT_RPC=1`）；command 模式下
  bridge askAgent 走进程内 `TurnQueue` 串行调用 agent-run.mjs（与 agent 路径一致）
- `chiguo_envcheck.py` 的 `check_pi` 支持 `runner`/`agent_command` 参数：runner=command 时检查
  agent_command 可执行性（不再要求 agent 二进制）
- 失败排查：askAgent 报「⚠️ 处理失败」时，除 bridge 日志（logs/wechat-bridge.log）外，手跑
  `<agent_command> --prompt '测试' --mode send` 直接看 agent 自身输出/日志

## 九、记忆后端抽象（[memory].backend）

v1.8 起记忆模块解耦为 `memory/` 包（`memory_bridge.py` 保留兼容门面：MemoryBridge=Mem0Backend 别名 + CLI）。
v1.9 起唯一内置后端为 mem0（旧 LanceDB/JSON 后端已删除）。`[memory].backend` 取值：

| 取值 | 行为 |
|------|------|
| `mem0`（默认） | mem0ai 记忆层：LLM 事实提取写入 + ollama 本地向量检索 + qdrant 嵌入式存储（`data/mem0/`） |
| `module.path.ClassName` | 自定义后端类（importlib 动态加载，须继承 `memory/base.py` 的 `MemoryBackend`） |

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
  `random_memory_with_forgetting` 由基类基于原语包装（R = e^(-t/(S×importance))，S=168h、min_weight=0.1），
  自定义后端零成本获得遗忘曲线
- 行契约（search/random_memory 返回 dict 字段）：id/text/category/scope/importance/timestamp/datetime/
  memory_category/l0_abstract/l2_content/tier/source；importance 必须清洗为非 NaN
- 自定义后端示例骨架：

```python
# my_memory.py — toml [memory].backend = "my_memory.MyBackend"
from memory.base import MemoryBackend

class MyBackend(MemoryBackend):
    def __init__(self, manual_path=None, **kwargs):   # kwargs = [memory] 段其余键
        ...

    @property
    def available(self) -> bool:
        return True

    def search(self, query, limit=10, category=None, min_importance=0.3):
        return [...]                                   # 统一行契约 dict 列表

    def random_memory(self, category=None, min_importance=0.5, prefer_categories=None):
        return {...} or None

    def stats(self) -> dict:
        return {...}
```

（自定义类放仓库任意模块路径即可；实例化 kwargs = [memory] 段其余键，按构造签名过滤。）

## 十一、故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `pi exited 1: ... No API key found` | auth.json 无 [host].provider 对应条目 | install_agent.sh 阶段 5（AGENT_API_KEY/OPENCODE_API_KEY） |
| `401 Unauthorized` | provider key 失效 | 换 key 重写 auth.json；`chiguo_envcheck.py` 复核 |
| `{"ok":false,"error":"empty reply"}` | agent 无 message_end 文本（空回复/坏 JSON） | 重试；检查 provider/model 是否可生成中文文本；`pi -p --provider <[host].provider> ... --mode json '测试'` 手动验证 |
| 超时（120s kill） | 网关慢/thinking 过高 | 调低 `[host].thinking_level`（off/minimal/low/medium/high/xhigh/max） |
| `[chiguo-tick] agent-run 未生成消息` | agent-run 失败（多数是 key/网络） | 看 logs/cron-tick.log；先手动跑一次 agent-run 复现 |
| bridge 回复「⚠️ 处理失败」 | askAgent 抛错（agent-run 非 JSON/失败） | bridge 日志（logs/wechat-bridge.log）看具体 error |
| 特殊命令回「处理失败」 | daemon CLI 报错（如日期格式错） | 命令 JSON 输出含 error；对照 §五 命令表手跑验证 |
| command runner 下 askAgent 失败/回「⚠️ 处理失败」 | agent 脚本自身报错（非 JSON/非零退出/脚本缺失） | 手跑 `<agent_command> --prompt '测试' --mode send` 看 agent stdout/日志；核对 `AGENTRUN_RUNNER`/`AGENTRUN_AGENT_COMMAND` 生效配置与 `[host].agent_command` |

## 十二、维护速查

```bash
# 手动决策 + 生成 + 发送链路（分步）
uv run python chiguo_daemon.py --compact          # 决策（idle 输出最小 JSON）
node scripts/agent-run.mjs --prompt '<决策 JSON>'    # 生成消息
bash scripts/chiguo-tick.sh                       # 全链路（idle 静默 0）

# 手动验证特殊命令（只读）
uv run python chiguo_daemon.py --anniversary list
uv run python chiguo_daemon.py --break status

# 会话/并发检查
#   chiguo-main：bridge 回复（TurnQueue 串行）
#   chiguo-send：tick 主动发送（AGENTRUN_SESSION 注入）
#   同会话并发 turn 在 agent 侧可能交错 → 两条链路永不共用会话

# 环境检查
uv run python chiguo_envcheck.py

# 测试
node tests/test_agent_run.mjs && node tests/test_agent_rpc.mjs && node tests/test_bridge_agent_http.mjs && node tests/test_bridge_askagent_rpc.mjs && node tests/test_bridge_askagent.mjs && node tests/test_bridge_cmd.mjs && \
bash tests/test_install_agent.sh --dry-run && \
bash tests/test_wechat_bridge.sh && uv run python tests/test_*.py   # 全量见 AGENTS.md
```
