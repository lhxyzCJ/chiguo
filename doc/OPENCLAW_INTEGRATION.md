# OpenClaw 集成指南

> daemon v4 | 基于 `openclaw cron add --help` (v2026.6.10)

## 架构

```
openclaw cron (每30分钟, --session main --wake now)
  → agent 收到 message payload
  → 执行 python3 chiguo_daemon.py
    ├─ action=idle   → 结束
    └─ action=send   → SUN2.md 生成消息 → openclaw-weixin 发送

WeChat 消息到达
  → UserPromptSubmit hook 触发
  → agent 收到 hook 提示
  → LLM 分析情绪 → daemon --user-msg --analysis → SUN2.md 回复
```

---

## 一、创建 Cron

在 OpenClaw 终端执行：

```bash
openclaw cron add \
  --name "chiguo-check" \
  --cron "*/30 * * * *" \
  --tz "Asia/Shanghai" \
  --session main \
  --wake now \
  --exact \
  --timeout-seconds 120 \
  --system-event "运行 python3 <仓库目录>/chiguo_daemon.py。解析 stdout JSON。若 action=idle，回复 NO_REPLY。若 action=send，读取 context 字段，按 SUN2.md 人格生成 1-3 句微信消息，通过 openclaw-weixin 通道发给 owner@im.wechat。遵守 context.layer_guidance 的语气指引和 context.instruction 的格式约束。若 layer_guidance 含【安全阀】标记，语气务必温和克制。"
```

### 参数说明（来自 `openclaw cron add --help`）

| 参数 | 值 | 说明 |
|------|-----|------|
| `--name` | `chiguo-check` | cron 任务名 |
| `--cron` | `*/30 * * * *` | 5 字段 cron 表达式，每 30 分钟 |
| `--tz` | `Asia/Shanghai` | 时区，中国标准时间 |
| `--session` | `main` | 发送到 main session |
| `--wake` | `now` | 立即唤醒 session（不等 heartbeat） |
| `--exact` | — | 禁用 cron 随机抖动（精确 30 分钟） |
| `--timeout-seconds` | `120` | agent 处理超时 2 分钟 |
| `--system-event` | `"..."` | agent 收到后按此指令执行 |

### 为什么不用 `--command`

`--command` 直接在 Gateway 进程跑 shell，不启动 agent。daemon 输出的 JSON 需要 agent 解析并生成消息，所以必须用 `--system-event` 让 agent 处理。

### 为什么是 30 分钟

`base_lambda=0.25`（平均 4 小时一次事件），`min_interval=30min`（发消息后 30min 内不会再发），`emotion` 半衰期 30-500 小时。15 分钟粒度在数学上无意义——96 次/天的 cron 触发中 ~90 次输出 idle。30 分钟减少一半无效调用，延迟上限 30 分钟对情绪动力学无任何影响。

### 管理

```bash
openclaw cron list                  # 列出所有 cron
openclaw cron show chiguo-check     # 查看详情
openclaw cron disable chiguo-check  # 暂停
openclaw cron enable chiguo-check   # 恢复
openclaw cron run chiguo-check      # 手动触发一次
openclaw cron rm chiguo-check       # 删除
```

---

## 二、UserPromptSubmit Hook

项目根目录创建 `.claude/settings.json`：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.openclaw/workspace/skills/chiguo/scripts/on-user-msg.sh"
          }
        ]
      }
    ]
  }
}
```

Hook 脚本 `scripts/on-user-msg.sh`：

```bash
#!/bin/bash
MSG="${CLAUDE_USER_PROMPT:-}"
if [ -z "$MSG" ]; then exit 0; fi
echo "<chiguo>主人发了新消息。chiguo skill §二：1.LLM分析情绪 2.daemon --user-msg --analysis 3.SUN2.md回复</chiguo>"
```

---

## 三、情绪分析 prompt

Agent 收到主人消息后，LLM 分析：

```
分析主人发给迟菓的微信消息。只输出 JSON，不输出解释。

{
  "warmth": <float -1.0~1.0>,
  "effort": <float 0.0~1.0>,
  "attention": <float 0.0~1.0>,
  "suppress_hours": <float 0.0~24.0 可选>
}

warmth: -1.0=敌意/烦躁("别烦我","滚"), -0.5=冷淡("嗯","..."), 0=中性("好的"), 0.5=温暖("菓菓辛苦了"), 1.0=亲密("菓菓最好了！想你了")
effort: 0=敷衍("嗯","ok","."), 0.5=一般("好的菓菓"), 1.0=用心长消息
attention: 0=无视迟菓(说别的事), 0.5=部分回应, 1.0=直接回应她的话题
suppress_hours: 检测到忙碌/结束对话时设置("在开会"→4,"晚安睡了"→8)，不确定时省略此字段
```

更新 daemon：
```bash
python3 <仓库目录>/chiguo_daemon.py \
  --user-msg "<消息原文>" \
  --analysis '<LLM输出的JSON>'
```

---

## 四、特殊命令

| 主人说 | 执行 CLI |
|--------|---------|
| "记住X月X日是XX" | `python3 <仓库目录>/chiguo_daemon.py --anniversary "add anniversary MM-DD <名称>"` |
| "X月X日要XX" | `python3 <仓库目录>/chiguo_daemon.py --anniversary "add countdown YYYY-MM-DD <名称>"` |
| "有哪些纪念日" | `python3 <仓库目录>/chiguo_daemon.py --anniversary list` |
| "放暑假了"/"放假了" | `python3 <仓库目录>/chiguo_daemon.py --break on` |
| "开学了" | `python3 <仓库目录>/chiguo_daemon.py --break off` |

---

## 五、决策输出参考

### send 示例
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

### 触发类型速查
lonely_low / lonely_mid / lonely_high / anxiety / longing / morning / night / meal / playful / memory / reflect / special

### 安全阀标记
若 `layer_guidance` 含以下标记，必须遵守：
- **【安全阀】48h内多次崩溃** → 语气温和克制，用关心代替不安，不质问不崩溃
- **【安全阀】距上次崩溃不足24h** → 语气放软，不再次崩溃

### idle 示例
```json
{"action":"idle","reason":"no_trigger","state":{...}}
```
`reason`: no_trigger / quiet_hours / daily_limit / low_energy / min_interval / user_sleeping / user_busy

---

## 六、调试

```bash
# 手动决策
python3 <仓库目录>/chiguo_daemon.py

# 模拟主人消息
python3 <仓库目录>/chiguo_daemon.py --user-msg "我回来了" --analysis '{"warmth":0.5,"effort":0.4,"attention":0.7}'

# 状态 + 健康
python3 <仓库目录>/chiguo_daemon.py --status
python3 <仓库目录>/chiguo_daemon.py --health

# 统计 + 告警
python3 <仓库目录>/chiguo_daemon.py --stats 7
python3 <仓库目录>/chiguo_daemon.py --alerts

# 手动触发 cron
openclaw cron run chiguo-check

# 查看 cron 运行历史
openclaw cron runs chiguo-check
```

---

## 七、文件清单

```
<仓库目录>/
├── chiguo_daemon.py               # 决策引擎
├── chiguo_proactive.toml          # 全部配置
├── doc/OPENCLAW_INTEGRATION.md    # 本文档

~/.openclaw/workspace/skills/chiguo/
├── SKILL.md                       # agent 指令集
├── SUN2.md                        # 迟菓人格
├── scripts/
│   └── on-user-msg.sh             # hook 脚本
└── references/
    └── 迟菓语言技巧指南.md
```
