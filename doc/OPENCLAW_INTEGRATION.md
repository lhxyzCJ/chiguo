# OpenClaw 集成指南（v11）

> 官方文档出处: docs.openclaw.ai（automation/cron-jobs、hooks、cli、doctor；功能探测以
> `<command> --help` 为权威清单）。旧版（v4）cron system-event + Claude-Code hook 方案见文末「降级路径」。

## 架构

```
发送侧（trigger-script 门控，零 idle 模型调用）：
OpenClaw cron add chiguo-check --every 15m --trigger-script scripts/chiguo-watch.js --session main
  → trigger 脚本无模型执行：exec 跑 <仓库目录>/.venv/bin/python chiguo_daemon.py --compact
    ├─ action=idle → {fire:false}（零模型调用，~90% 的评估不唤醒 agent）
    └─ action=send → {fire:true, message:<决策 JSON>}
  → main session agent 收到 system-event（生成指令 + 决策 JSON）
    → 按 SUN2.md 生成 1-3 句 → openclaw-weixin 发送
    → daemon --record-send / --send-result 回写发送状态（幂等）

回复侧（standing order 单记录，替代 v4 UserPromptSubmit hook）：
微信消息到达 → agent 正常回复；standing order（agents/main/AGENTS.md）强制流程：
  LLM 情绪分析（warmth/effort/attention/topic）→ daemon --user-msg --analysis → SUN2.md 回复
  （无 hook 双重记录；回复延迟建模唯一入口是 agent 调 --user-msg 这一次）
```

---

## 一、安装（推荐：任意机器 pull 后自动引导）

```bash
bash scripts/install_integration.sh --dry-run   # 先扫描（只读）
bash scripts/install_integration.sh --yes       # 自动安装+迁移
bash deploy.sh                                  # 或随部署一起（传 --skip-integration 可跳过）
```

安装器模式（官方命令见 §二）：

| 模式 | 行为 |
|------|------|
| `--dry-run` | 只扫描报告，不写任何东西；发现待办/残留则退出 1（非 TTY 默认也是它） |
| `--yes` | 自动完成全部安装+迁移（每次修改前备份） |
| 默认（交互 ask） | 逐项确认：y 执行，n 跳过并视为残留（退出 1） |
| `--skip-integration` | 静默跳过（deploy.sh 传参用） |

退出码约定：`0`=完成，`1`=有待办/警告/残留未处理，`2`=严重问题。全部幂等，重复运行安全。

---

## 二、安装器做了什么（逐阶段对应官方命令）

| 阶段 | 内容 | 官方命令 |
|------|------|----------|
| 0 探测 | openclaw 存在性 / 功能探测 / Gateway 状态 | `openclaw -V`；`openclaw cron add --help \| grep --trigger-script`（官方：`<command> --help` 为权威清单）；`openclaw gateway status`。无 openclaw → 跳过安装退出 0；不支持 `--trigger-script` → 提示降级路径退出 1 |
| 0b 残留扫描 | 旧方案残留发现即报告 | `openclaw cron list --all \| grep -i chiguo`（--all 含禁用）；`.claude/settings.json` 中 chiguo 的 UserPromptSubmit 条目；`~/.openclaw/workspace/skills/chiguo/scripts/on-user-msg.sh`；`openclaw hooks list \| grep -i chiguo`；`openclaw config get hooks.internal.handlers`（legacy 格式，官方建议迁移） |
| 1 开关 | 开启官方危险自动化开关（脚本以 agent 权限无头执行，安装器仅注册 chiguo-watch.js 一条命令） | `openclaw config set cron.triggers.enabled true`；修改后 `openclaw config validate` |
| 2 注册 | 先移除旧作业，再注册 trigger-script 作业（已存在则跳过，幂等） | `openclaw cron rm <旧作业>`；`openclaw cron add chiguo-check --every 15m --trigger-script '<仓库目录>/scripts/chiguo-watch.js' --session main --wake now --timeout-seconds 120 --system-event '<指令>'` |
| 3 standing order | 回复侧流程写入 `~/.openclaw/workspace/agents/main/AGENTS.md`（`# CHIGUO-STANDING-ORDER-START/END` 标记段，幂等：已存在跳过；写入前备份 .bak） | 见 §四全文 |
| 3b 清理 | 清除 v4 残留：`.claude/settings.json` hook 条目（备份后 JSON 编辑，保留其他 hook）；on-user-msg.sh → `.bak` 后移除；OpenClaw 原生 chiguo hook 禁用；legacy handlers 检测并告警（官方建议迁移到 discovery 系统；doctor --fix 迁移清单不含该键 → 不自动处理，残留计入待办 PENDING） | `openclaw hooks disable <name>`；`openclaw config get hooks.internal.handlers`（检测） |
| 4 验证 | 配置合法 / 作业在册 / 开关与 standing order 复验（config set / AGENTS.md 写入失败兜底 → PENDING）/ 官方审计（--yes 模式）/ 端到端冒烟 | `openclaw config validate`；`openclaw cron list \| grep chiguo-check`；`openclaw config get cron.triggers.enabled`（必须 true）；`grep CHIGUO-STANDING-ORDER-START ~/.openclaw/workspace/agents/main/AGENTS.md`；`openclaw security audit --deep`；`openclaw cron run chiguo-check --expect-final`（观察 run --expect-final 退出码与决策日志 chiguo_decisions.jsonl 新条目判断链路） |

阶段 2 注册时写入的 `--system-event` 指令全文（与安装器 INSTRUCTION 一致）：

```
收到迟菓决策结果。按 SUN2.md 人格生成 1-3 句微信消息发给主人（当前微信会话上下文）。遵守 context.layer_guidance 语气指引与 context.instruction 格式约束；layer_guidance 含【安全阀】标记时语气务必温和克制。发送后运行 <仓库目录>/.venv/bin/python <仓库目录>/chiguo_daemon.py --record-send <msg_id> --text <消息原文> --trigger <trigger> --intensity <intensity>；发送失败则运行 --send-result <msg_id> --send-status failed。
```

15 分钟间隔：idle 评估不再消耗模型，决策延迟从 30min 降到 15min；daemon 内部 pacing（poisson / min_interval / quiet hours）不受影响。

---

## 三、trigger 脚本契约（scripts/chiguo-watch.js）

官方契约（docs.openclaw.ai/automation/cron-jobs → "Event triggers"）：脚本返回
`{fire: bool, message?: string, state?: object}`，state ≤ 16KB（官方上限）。

| 情形 | 返回 |
|------|------|
| `action=send` | `{fire: true, message: <完整决策 JSON>}`（清空 last_error） |
| `action=idle` | `{fire: false}`（零模型调用，清空 last_error） |
| daemon 崩溃（exec 抛错）/ stdout 无决策 JSON / 坏 JSON / 超时 / 未知 action | `{fire: false, state: {last_error: ...}}`，持久留到下次评估带出 |

> 注：脚本只读 exec 返回的 stdout 内容（以及 exec 调用本身抛错），不读取 daemon 退出码——"非零退出"只要 stdout 仍是合法 JSON 就按 JSON 处理；反之退出码为 0 但 stdout 无 JSON 也判为失败。

实现要点：

- 执行：`tools.call('exec', {command})` 跑 `<仓库目录>/.venv/bin/python <仓库目录>/chiguo_daemon.py --compact`（仓库路径来自环境变量 `CHIGUO_REPO`，缺省为脚本所在目录的上级）。
- 解析：优先全文 JSON，失败逐行回退（stdout 杂音泄漏防护）；`--compact` 时 idle 输出最小单行 `{"action":"idle","time":...}`（chiguo_watch 视为 fire:false；另见 §七 调试）。
- 纯搬运工：脚本不生成消息、不决策，只做 exec + 解析 + fire（决策/生成分离铁律）。
- 单测：`node test_trigger_script.js`（mock 四路径：idle / send / 坏 JSON / 无输出或 exec 抛错）。

### 决策输出参考（fire:true 时 message 的完整内容）

#### send 示例

```json
{
  "action": "send",
  "trigger": "lonely_mid",
  "intensity": "medium",
  "context": {
    "situation": "主人已经14小时没发消息了...",
    "layer": "middle",
    "layer_guidance": "嘴硬心软，表面强硬但话里有话...",
    "emotion": {"loneliness":62,"affection":55,"anxiety":48,"energy":72,"tsundere_index":68},
    "silent_hours": 14.2,
    "personality_profile": "tsundere_heavy|sensitive|playful",
    "schedule_hint": "主人今天没课。",
    "instruction": "请以迟菓（SUN2.md 设定）的身份，用上述语气发一条微信消息给主人。1-3句话。自然。",
    "combo": "a_b",
    "trigger_type": "lonely_mid"
  }
}
```

#### 触发类型速查

special / morning / night / meal / memory / follow_up / lonely_low / lonely_mid / lonely_high / anxiety / playful / reflect / longing（13 种，v7 新增 follow_up）

#### 安全阀标记

若 `layer_guidance` 含以下标记，必须遵守：

- **【安全阀】48h内多次崩溃** → 语气温和克制，用关心代替不安，不质问不崩溃
- **【安全阀】距上次崩溃不足24h** → 语气放软，不再次崩溃

#### idle 示例

```json
{"action":"idle","reason":"no_trigger","state":{...}}
```

`reason`: no_trigger / quiet_hours / daily_limit / low_energy / min_interval / user_sleeping / user_busy / busy_suppressed / sleeping_guard

---

## 四、回复侧流程（standing order 内容全文）

安装器在 `~/.openclaw/workspace/agents/main/AGENTS.md` 写入的段落（标记段幂等，重复运行不重复；写入前备份 `.bak`）：

```

# CHIGUO-STANDING-ORDER-START
## 迟菓消息流程（standing order，每会话注入）
每次收到主人的微信消息并准备回复时：
1. 用 LLM 分析主人消息情绪，输出 JSON：{"warmth": -1~1, "effort": 0~1, "attention": 0~1, "topic": "可选", "suppress_hours": 可选}
2. 运行 <仓库目录>/.venv/bin/python <仓库目录>/chiguo_daemon.py --user-msg <消息原文> --analysis '<JSON>'
3. 按 ~/.openclaw/workspace/skills/chiguo/SUN2.md 人格回复
4. 纪念日/假期指令：运行 chiguo_daemon.py --anniversary / --break 对应命令
# CHIGUO-STANDING-ORDER-END
```

### 情绪分析 prompt

第 1 步 LLM 分析的完整 JSON 规范（与 standing order 的 JSON 一致，字段注释供 agent 校准）：

```
分析主人发给迟菓的微信消息。只输出 JSON，不输出解释。

{
  "warmth": <float -1.0~1.0>,
  "effort": <float 0.0~1.0>,
  "attention": <float 0.0~1.0>,
  "topic": "<字符串 可选，消息涉及的话题/事件，用于话题跟随>",
  "suppress_hours": <float 0.0~24.0 可选>
}

warmth: -1.0=敌意/烦躁("别烦我","滚"), -0.5=冷淡("嗯","..."), 0=中性("好的"), 0.5=温暖("菓菓辛苦了"), 1.0=亲密("菓菓最好了！想你了")
effort: 0=敷衍("嗯","ok","."), 0.5=一般("好的菓菓"), 1.0=用心长消息
attention: 0=无视迟菓(说别的事), 0.5=部分回应, 1.0=直接回应她的话题
suppress_hours: 检测到忙碌/结束对话时设置("在开会"→4,"晚安睡了"→8)，不确定时省略此字段
```

更新 daemon：

```bash
<仓库目录>/.venv/bin/python <仓库目录>/chiguo_daemon.py \
  --user-msg "<消息原文>" \
  --analysis '<LLM输出的JSON>'
```

---

## 五、特殊命令（--anniversary / --break）

| 主人说 | 执行 CLI |
|--------|----------|
| "记住X月X日是XX" | `<仓库目录>/.venv/bin/python chiguo_daemon.py --anniversary "add anniversary MM-DD <名称>"` |
| "X月X日要XX" | `<仓库目录>/.venv/bin/python chiguo_daemon.py --anniversary "add countdown YYYY-MM-DD <名称>"` |
| "有哪些纪念日" | `<仓库目录>/.venv/bin/python chiguo_daemon.py --anniversary list` |
| "放暑假了"/"放假了" | `<仓库目录>/.venv/bin/python chiguo_daemon.py --break on` |
| "开学了" | `<仓库目录>/.venv/bin/python chiguo_daemon.py --break off` |

---

## 六、管理命令

```bash
openclaw cron list                              # 列出所有作业
openclaw cron get chiguo-check                  # 查看详情（确认 --trigger-script 落库）
openclaw cron edit chiguo-check ...             # 修改参数
openclaw cron disable chiguo-check              # 暂停（不删除，调度器不再触发）
openclaw cron enable chiguo-check               # 恢复
openclaw cron run chiguo-check --expect-final   # 手动触发并等待结果（端到端冒烟）
openclaw cron rm chiguo-check                   # 删除（官方叙述文档写 remove、命令树为 rm，等效；cron 为官方命令（旧版 automations 别名已移除）
```

---

## 七、调试

```bash
# 手动决策（JSON 到 stdout；--compact 时 idle 输出最小单行 JSON）
uv run python chiguo_daemon.py
uv run python chiguo_daemon.py --compact

# 模拟主人消息（回复侧链路）
uv run python chiguo_daemon.py --user-msg "我回来了" --analysis '{"warmth":0.5,"effort":0.4,"attention":0.7,"topic":"今天"}'

# 状态 + 健康 + 统计 + 告警 + 监控
uv run python chiguo_daemon.py --status
uv run python chiguo_daemon.py --health
uv run python chiguo_daemon.py --stats 7
uv run python chiguo_daemon.py --alerts
uv run python chiguo_daemon.py --monitor

# trigger 脚本四路径单测
node test_trigger_script.js

# 安装器只读扫描（改完安装器后先跑这个）
bash scripts/install_integration.sh --dry-run

# 端到端冒烟（观察 run --expect-final 退出码与决策日志 chiguo_decisions.jsonl 新条目判断链路）
openclaw cron run chiguo-check --expect-final

# 配置校验 + 官方审计
openclaw config validate
openclaw security audit --deep
openclaw doctor
```

---

## 八、降级路径（版本不支持 trigger-script 时）

安装器阶段 0 探测发现当前版本不支持 `cron --trigger-script` 时（官方：`<command> --help` 为权威清单），保留 v4 的 cron system-event 方式：

```bash
openclaw cron add chiguo-check --cron "*/30 * * * *" --tz Asia/Shanghai \
  --session main --wake now --timeout-seconds 120 \
  --system-event "运行 python3 <repo>/chiguo_daemon.py。解析 stdout JSON。idle→NO_REPLY；send→SUN2.md 生成并发送"
```

注意：

- 此方式是 v4 旧方案，每次触发都会唤醒 agent（~90% idle 空转模型调用），仅作降级兜底；新版本请用主方案并重跑安装器。
- 回复侧：保留 standing order（同主方案，不依赖 hook）；v4 的 `.claude/settings.json` UserPromptSubmit hook 已由安装器阶段 3b 清除，不需要也不应恢复。

---

## 九、官方出处索引

- Automations / trigger-script / event triggers：`/automation`、`/automation/cron-jobs`
- 危险自动化开关 `cron.triggers.enabled`：`/automation/cron-jobs`（Warning 段）
- Internal hooks / 事件表 / legacy handlers（兼容性说明与迁移到 discovery 系统的建议）：`/automation/hooks`
- Standing orders：`/automation`（Quick decision guide）
- CLI 参考（config get/set/validate、cron、hooks、security audit、doctor）：`/cli`
- `<command> --help` 权威性：`/cli`（Command tree 段）
- `openclaw cron` 为官方命令（旧版使用 `automations` 别名，2026.7.1-2 起已移除）：`/automation/cron-jobs`
