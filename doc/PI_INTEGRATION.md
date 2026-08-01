# pi-agent 集成指南（Phase 4，v1.4）

> 寄主迁移后的当前架构：**消息生成与情绪分析全部走 pi-agent**（opencode-go provider），
> 定时触发走系统 crontab（chiguo-tick），微信收发走 wechat-bridge，记忆走 memory-lancedb-pro
> （pi 版扩展 + ollama embedding，复用历史 LanceDB 库）。OpenClaw 链路已停用，
> 回退参考见 [OPENCLAW_INTEGRATION.md](OPENCLAW_INTEGRATION.md)（已废弃头注）。

## 架构总览

```
发送侧（系统 crontab 门控，零 idle 模型调用）：
crontab */15 * * * * scripts/chiguo-tick.sh
  → .venv/bin/python chiguo_daemon.py --compact（零 LLM 评估）
    ├─ action=idle → 静默退出（~90% 的评估不唤醒 LLM）
    └─ action=send → node scripts/pi-run.mjs --prompt <决策 JSON>（PIRUN_SESSION=chiguo-send）
        → pi 按 SUN2.md 生成 1-3 句
        → curl --noproxy '*' -X POST <toml [host].wechat_bridge_url> {"to","text"} → bridge → bot.send()
        → daemon --record-send <msg_id> --text <text> 回写（幂等）

回复侧（bridge 内联，pi 单次调用完成分析+回复）：
微信消息到达 → bridge 先确定性 daemon --user-msg <原文>（无分析）
  → detectSpecialCommand 检测纪念日/假期命令（命中 → 直接执行 daemon --anniversary/--break 并回复，不经 pi）
  → 否则 pi-run.mjs --prompt <原文> --analysis-mode → <<ANALYSIS>>{情绪 JSON}<<END>> + 回复文本
  → 有 analysis → daemon --user-msg <原文> --analysis '<JSON>'（recv_dedup 升级，不重复记账）
  → 回复文本发回微信

会话模型（并发隔离）：
  chiguo-main  = 回复侧（bridge 进程内 TurnQueue 串行）
  chiguo-send  = 主动发送（chiguo-tick.sh 经 PIRUN_SESSION 注入）
  两进程不同会话 → 无跨进程并发 turn；bridge 进程内 TurnQueue 兜底回复侧自身串行
```

## 一、安装（install_pi.sh）

任意机器 pull 仓库后，pi 环境由 `scripts/install_pi.sh` 一键引导（幂等，deploy.sh 第 5.6 步接入）：

```bash
bash scripts/install_pi.sh --dry-run   # 只扫描报告（只读，非 TTY 默认也是它）
bash scripts/install_pi.sh --yes       # 自动完成全部（每次修改前 .bak 备份）
bash scripts/install_pi.sh             # 交互 ask：逐项确认
bash deploy.sh                         # 或随部署一起（传 --skip-pi 跳过）
```

模式与退出码同 install_integration.sh 约定：`0`=完成，`1`=有待办/警告/残留，`2`=严重问题。

| 阶段 | 内容 |
|------|------|
| 0 探测 | `pi --version`（缺失 → 严重）；`OPENCODE_API_KEY` 可用性提示 |
| 1 memory-lancedb-pro | clone `github.com/lhxyzCJ/TestForPi-memory-lancedb-pro` → `~/.pi-agent/`，缺 dist 才 `npm install && npm run build` |
| 2 settings.json | `extensions` 写 `~/.pi-agent/TestForPi-memory-lancedb-pro/dist/pi-adapter/index.js`（修正 Windows 残留路径） |
| 3 json5 配置 | 写 `~/.pi/agent/memory-lancedb-pro.json5`（dbPath=~/.openclaw/memory/lancedb-pro + ollama embedding + deepseek llm + autoCapture/autoRecall/smartExtraction） |
| 4 ollama | `curl localhost:11434/api/tags` 有 `qwen3-embedding:0.6b`（缺 → 提示/`ollama pull`） |
| 5 auth.json | `opencode-go` 条目（key 从 `OPENCODE_API_KEY` 环境变量读，不落盘明文，chmod 600） |
| 6 crontab | 注册 `*/15 * * * * scripts/chiguo-tick.sh >> logs/cron-tick.log 2>&1`（幂等，旧条目整行替换） |
| 7 冒烟 | `memory-pro stats` + `pi -p --provider opencode-go ...`（仅 --yes/ask） |

## 二、pi-run 契约（scripts/pi-run.mjs）

```bash
node scripts/pi-run.mjs --prompt <文本>            # 生成消息 → stdout JSON {"ok":true,"text":...}
node scripts/pi-run.mjs --prompt <文本> --analysis-mode  # 情绪分析 + 回复 → {"ok":true,"text","analysis"}
```

- **配置优先级**：`PIRUN_*` 环境变量 > toml `[host]` 段 > 默认值
  （PIRUN_PROVIDER/PIRUN_MODEL/PIRUN_THINKING/PIRUN_SESSION/PIRUN_PERSONALITY/PIRUN_GUIDE）
- **[host] 键**：`provider`（默认 opencode-go）、`model`（deepseek-v4-flash）、`thinking_level`（high）、
  `session_id`（chiguo-main，回复侧）、`send_session_id`（chiguo-send，主动发送）、
  `personality_dir`（仓库内 `personality/`，注入 SUN2.md + 迟菓语言技巧指南.md）、
  `wechat_bridge_url`（`http://127.0.0.1:18790/send`）
- **pi 参数**：`-p` 非交互 + `--no-context-files`（隔离仓库开发上下文）+ `--mode json`（NDJSON 事件流）
  + `--append-system-prompt` ×2（SUN2 + 语言技巧指南）+ `--session-id <会话>` + `--thinking`
- **输出解析**：NDJSON 取最后一条 `message_end` 的 text 拼接；analysis-mode 提取
  `<<ANALYSIS>>{...}<<END>>` 块
- **失败语义**：`{"ok":false,"error":"..."}`；非零退出但 stdout 含完整回复 → salvage 不丢回复
- 单测：`node test_pi_run.mjs`（19 用例）

## 三、chiguo-tick（系统 crontab 入口）

- 入口 `scripts/chiguo-tick.sh`（+x）；注册/管理由 install_pi.sh 阶段 6 负责
- 流程见架构图；关键点：idle 静默退出；send 走 `PIRUN_SESSION=chiguo-send`（与会话分离）；
  curl 带 `--noproxy '*'`；发送失败仅记 stderr 并 `exit 0`（下个 tick 重试）；
  `--record-send` 回写发送状态（失败不阻塞）
- 日志：`logs/cron-tick.log`

## 四、bridge askPi（回复侧）

`wechat-bridge/bridge.mjs`（v4）：

- 消息到达 → `recordUserMsg(text)`（daemon `--user-msg`，确定性，失败不阻塞）
- → `detectSpecialCommand(text)`（特殊命令，见 §五；命中 → 执行 daemon 并回复，**不经 pi**）
- → `askPi(text)`（pi-run `--analysis-mode`，一次完成分析+回复；进程内 `TurnQueue` 串行 pi 调用）
- → `upgradeAnalysis(text, analysis)`（daemon `--user-msg --analysis`，recv_dedup 升级语义）
- → `bot.reply(msg, reply)`
- 环境变量：`WECHAT_BRIDGE_PI_RUN`（默认仓库内 pi-run.mjs）、`WECHAT_BRIDGE_DAEMON_PY`、
  `WECHAT_BRIDGE_DAEMON`、`WECHAT_BRIDGE_OWNER`、`WECHAT_BRIDGE_SEND_PORT`、`WECHAT_BRIDGE_STORAGE`
- 测试：`node test_bridge_askpi.mjs`（10 用例）、`node test_bridge_cmd.mjs`（31 用例）

## 五、特殊命令（纪念日/假期，方案 A：bridge 规则化）

openclaw standing order 停用后，纪念日/假期指令由 **bridge 确定性接管**（pi 纯文本调用无工具权限，
不依赖 pi 输出稳定性）。`wechat-bridge/command-detect.mjs` 在消息到达时正则检测：

| 哥哥说 | 执行 |
|--------|------|
| 记住X月X日(是)XX | `chiguo_daemon.py --anniversary "add anniversary MM-DD <名称>"` |
| YYYY年X月X日(是/为/要)XX | `--anniversary "add countdown YYYY-MM-DD <名称>"` |
| X月X日要XX（无年份） | `--anniversary "add countdown <推断年份>-MM-DD <名称>"`（已过 → 明年，CST） |
| 有哪些纪念日 / 纪念日列表 | `--anniversary list` |
| 放假了 / 放暑假了 / 我放假了 | `--break on` |
| 开学了 / 我开学了 | `--break off` |

**防误伤约束**（歧义交 pi 自然回复）：消息 ≤40 字；末尾带 吗/？/? 的问句不拦截；
`你/您` 开头的对 bot 提问不拦截；`今天放假了` 等一天性陈述不拦截。
执行后 bridge 回迟菓风确认文案（daemon JSON 驱动），失败回「处理失败：<原因>」。

> ⚠️ **裸「放假了」= 无限期假期**：`--break on` 置 `manual_override=True`（无限期，直到手动关闭，
> availability 恒 0.85，chiguo_monitor.py 会持续告警）。误触发后执行 `--break off` 或
> `--anniversary "remove <id>"` 式手动关闭：`uv run python chiguo_daemon.py --break off`。

## 六、opencode-go key 配置

- pi 读 `~/.pi/agent/auth.json` 的 `opencode-go` 条目（`{"type":"api_key","key":...}`，chmod 600）
- 写入途径：`export OPENCODE_API_KEY=... && bash scripts/install_pi.sh --yes`（阶段 5）
- key **不落盘明文到仓库**；`chiguo_envcheck.py` 的 `check_pi_auth` 校验该条目存在且有真值

## 七、memory-lancedb-pro 配置（记忆）

- 扩展：`~/.pi-agent/TestForPi-memory-lancedb-pro/dist/pi-adapter/index.js`
  （settings.json `extensions` 注册；安装器修正 Windows 残留路径）
- 配置：`~/.pi/agent/memory-lancedb-pro.json5` —— `dbPath=~/.openclaw/memory/lancedb-pro`
  （**复用历史 LanceDB 库**，只读语义不变）、embedding=ollama `qwen3-embedding:0.6b`（1024 维，localhost:11434）、
  llm=deepseek、autoCapture/autoRecall/smartExtraction 开、sessionMemory 关
- CLI 冒烟：`~/.pi-agent/.../node_modules/.bin/memory-pro stats`
- 降级：ollama 不可达 → 记忆 embedding 降级（自动捕获/召回不可用，不影响 daemon 主链路）；
  daemon 侧 LanceDB 仍经 `memory_bridge.py` 只读（缺 lancedb → `available=False` JSON 兜底）

## 八、故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `pi exited 1: ... No API key found` | auth.json 无 opencode-go 条目 | install_pi.sh 阶段 5（OPENCODE_API_KEY） |
| `401 Unauthorized` | opencode-go key 失效 | 换 key 重写 auth.json；`chiguo_envcheck.py` 复核 |
| `{"ok":false,"error":"empty reply"}` | pi 无 message_end 文本（空回复/坏 JSON） | 重试；检查 provider/model 是否可生成中文文本；`pi -p --provider opencode-go ... --mode json '测试'` 手动验证 |
| 超时（120s kill） | 网关慢/thinking 过高 | 调低 `[host].thinking_level`（off/minimal/low/medium/high/xhigh/max） |
| `[chiguo-tick] pi-run 未生成消息` | pi-run 失败（多数是 key/网络） | 看 logs/cron-tick.log；先手动跑一次 pi-run 复现 |
| bridge 回复「⚠️ 处理失败」 | askPi 抛错（pi-run 非 JSON/失败） | bridge 日志（logs/wechat-bridge.log）看具体 error |
| 特殊命令回「处理失败」 | daemon CLI 报错（如日期格式错） | 命令 JSON 输出含 error；对照 §五 命令表手跑验证 |
| memory-pro stats 失败 | 扩展未 build/ollama 停 | install_pi.sh 阶段 1/4；`ollama serve` 后重跑 |

## 九、维护速查

```bash
# 手动决策 + 生成 + 发送链路（分步）
uv run python chiguo_daemon.py --compact          # 决策（idle 输出最小 JSON）
node scripts/pi-run.mjs --prompt '<决策 JSON>'    # 生成消息
bash scripts/chiguo-tick.sh                       # 全链路（idle 静默 0）

# 手动验证特殊命令（只读）
uv run python chiguo_daemon.py --anniversary list
uv run python chiguo_daemon.py --break status

# 会话/并发检查
#   chiguo-main：bridge 回复（TurnQueue 串行）
#   chiguo-send：tick 主动发送（PIRUN_SESSION 注入）
#   同会话并发 turn 在 pi 侧可能交错 → 两条链路永不共用会话

# 环境检查
uv run python chiguo_envcheck.py

# 测试
node test_pi_run.mjs && node test_bridge_askpi.mjs && node test_bridge_cmd.mjs && \
node test_trigger_script.js && bash test_install_integration.sh && bash test_install_pi.sh --dry-run && \
bash test_wechat_bridge.sh && uv run python test_*.py   # 全量见 AGENTS.md
```
