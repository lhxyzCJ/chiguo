# 迟菓主动消息系统

角色主动消息守护进程 — 数学驱动的决策引擎 + pi-agent 消息生成（Phase 4 寄主，OpenClaw 已停用）。

**需要 Python 3.14+**（通过 uv 安装：`uv python install 3.14`）

## 快速开始

```bash
cd <仓库根目录>

# 查看当前状态
uv run python chiguo_daemon.py --status

# 单次决策（输出 JSON）
uv run python chiguo_daemon.py

# 交互式 Demo
python3 chiguo_demo.py
```

## 架构

```
chiguo_daemon.py（决策引擎，零 LLM）
  ├─ 情绪推进（半衰期衰减）
  ├─ 发送门控（静默/上限/间隔/元气）
  ├─ 触发评估（sigmoid 权重 + 加权随机）
  ├─ 话题注入（8 来源破冰）
  ├─ 生物钟学习（circadian：双作息双桶分桶学习 → 动态静默窗口）(v7/v8)
  ├─ 接话茬（follow_up：pending 话题续聊）(v7)
  ├─ 听歌双向联动（netease：睡眠窗口内播放反证 sleeping + 反向校正生物钟）(v8)
  ├─ 音乐话题源（netease 策略层：第 8 源 + 降级链 + peek/consume 两阶段配额）(v9)
  └─ 输出 JSON → pi-agent 生成消息 → wechat-bridge 发送微信（Phase 4；OpenClaw 停用）
```

决策与生成分离：daemon 只输出结构化 JSON，不调用 LLM。消息生成由 pi-agent 完成
（`scripts/pi-run.mjs` 注入 `personality/SUN2.md` + 迟菓语言技巧指南.md，详见 `doc/PI_INTEGRATION.md`）。

## 核心机制

### 5 维情绪

| 维度 | 范围 | 推进方式 |
|------|------|------|
| 孤独值 | 0-100 | 半衰期 40h 向 100 靠拢；主人回复 0.35h 骤降 |
| 好感度 | 5-100 | 半衰期 500h 向 0 靠拢；主人回复 +0.8 |
| 不安值 | 0-100 | 半衰期 30h 向 100 靠拢；主人回复 0.5h 骤降 |
| 元气值 | 0-100 | 半衰期 8h 恢复；发消息 -20 |
| 傲娇度 | 10-95 | 好感>65 降低，不安>60 升高 |

### 触发类型（sigmoid 权重 + 加权随机）

13 种触发（v7 含 follow_up 接话茬），sigmoid 替代硬阈值，加权随机替代优先级排序。

### 话题注入（8 来源）

lonely_low/mid 触发时，70% 概率从 8 个来源选自然话题破冰：

| 来源 | 权重 | 说明 |
|------|:--:|------|
| schedule | 0.30 | 课表/假期/周末状态 |
| memory | 0.25 | LanceDB 随机回忆 |
| general | 0.25 | 通用关心按时段 |
| weather | 0.20 | 季节感知 |
| anniversary | 0.15 | 纪念日/倒计时 |
| solar_terms | 0.10 | 24 节气 |
| preference_followup | 0.10 | LanceDB 偏好追问 |
| netease | 0.12 | v9: 网易云音乐话题（策略层委托，可注入/可省略） |

连续 3 次孤独触发 → 强制注入话题。

### LLM 内容分析

主人回复时，wechat-bridge 调 pi-agent 传入 `--analysis` JSON（warmth/effort/attention），daemon 据此差异化情绪变化。热情回复好感大幅上升，敷衍回复几乎不涨。

## 文件结构

```
chiguo_proactive.toml    # 主配置（所有参数，v4: +personality/bayesian/composer 段；v8: +[netease] 段）
chiguo_daemon.py         # 决策引擎（主入口，v4: +动态休眠 + Bayesian + 人格 + 组合；v7: +生物钟/接话茬；v8: +听歌反证）
chiguo_state.py          # 情绪引擎 + 人格 + Bayesian + 课表 + 节假日 + 记忆 + circadian/pending_topics (v4/v7；v8: 双作息迁移)
chiguo_circadian.py      # [v7] 生物钟学习（v8: 双作息双桶分桶学习 + 听歌活跃合并计数）
chiguo_trigger.py        # 触发器（sigmoid 权重 + 加权随机 + reflect + v7 follow_up 接话茬）(v4)
chiguo_topics.py         # 话题选择器（8 来源 + Ebbinghaus + 人格调制 + v9 netease 委托）(v4/v9)
chiguo_math.py           # 数学库（sigmoid/半衰期/Hawkes/概率累积）(v4)
chiguo_personality.py    # [v4] 多维人格系统（Big Five + 角色特有 8 维）
chiguo_bayesian.py       # [v4] Bayesian 用户状态推断（6 状态 + 在线学习）
chiguo_composer.py       # [v4] 消息组合系统（Intent × Cue × Vibe 三层）
chiguo_eventbus.py       # [v4] 轻量事件总线（发布/订阅模块解耦）
netease_bridge.py        # 网易云桥接（v8: +fetch_recent_play 最近播放记录，睡眠窗口内夜间活跃反证 + 缓存；v9: +有限重试 _api_get + 每日推荐 schema 过滤）
chiguo_netease.py        # [v9] 网易云策略层（健康状态/登录失效检测/降级链/音乐+故障双日配额/随机选源/话题素材组装）
schedule_parser.py       # 课表解析（xskb.xlsx → cache；解析失败保留旧缓存；v2 缓存含 alternates）
holiday_parser.py        # 节假日判断（2026 国务院安排）
solar_terms.py           # 24 节气查询
memory_bridge.py         # LanceDB 只读桥接 + Ebbinghaus 遗忘 (v4)
anniversary_manager.py   # 纪念日/倒计时 CRUD（无参构造锚定项目根，防 cwd 写散）
chiguo_monitor.py        # 结构化监控（stats / alerts / health；alerts 对 state:null 等脏数据归一化不崩）
chiguo_watchdog.py       # 独立看门狗（tick_seq 回退=重启不误报，相等>3h 才告警停滞）
chiguo_demo.py           # 演示模式（v2 架构，非生产行为）
scripts/chiguo-watch.js  # [v11] OpenClaw trigger-script（旧架构，已停用；回退参考）
scripts/install_integration.sh # [v11] OpenClaw 集成安装器（旧架构，已停用；回退参考）
scripts/pi-run.mjs      # [Phase 4] pi 调用统一封装（发送生成 + 回复分析；PIRUN_* 环境变量/toml [host] 段配置；NDJSON 解析 + <<ANALYSIS>> 提取）
scripts/chiguo-tick.sh  # [Phase 4] 系统 crontab 入口（daemon --compact 零模型门控 → pi-run 生成 → bridge /send → --record-send）
scripts/install_pi.sh   # [Phase 4] pi 环境安装器（memory-lancedb-pro/settings/json5/ollama/auth/crontab/冒烟，dry-run/yes/ask）
wechat-bridge/          # [Phase 4] 微信桥（bridge.mjs askPi + command-detect.mjs 特殊命令 + credentials/ 登录态）
test_*.py                # 测试（23 个文件，含 test_circadian/test_followup/test_netease_proof/test_netease_service/test_envcheck 等）
test_trigger_script.js   # [v11] chiguo-watch.js 契约测试（node，15 用例）
test_pi_run.mjs          # [Phase 4] pi-run.mjs 单测（node，19 用例：解析/提取/调用链路/exec 抛错/非零退出 salvage/配置读取）
test_bridge_askpi.mjs    # [Phase 4] bridge.mjs askPi 测试（node，10 用例：fake pi-run/daemon 真实 execFile 链路）
test_bridge_cmd.mjs      # [Phase 4] 特殊命令检测/执行测试（node，31 用例：detect 防误伤/buildReply/executeSpecialCommand）
test_install_integration.sh # [v11] 安装器桩测试（bash，12 用例）
test_install_pi.sh      # [Phase 4] pi 环境安装器桩测试（bash，14 用例：dry-run 零写入/待办清单/--skip-pi/--yes 产物断言/auth 合并/两遍幂等）
data/                    # 数据文件：课表 xskb.xlsx、手动记忆 chiguo_memories.json、网易云二维码 netease_qr.png
```

## CLI 参考

```bash
# 决策引擎
python3 chiguo_daemon.py                  # 单次决策
python3 chiguo_daemon.py --status         # 查看状态
python3 chiguo_daemon.py --compact        # 紧凑模式（idle 不输出）
python3 chiguo_daemon.py --loop 120       # 持续运行（间隔最小60秒，低于60自动按60并提示）

# 主人消息
python3 chiguo_daemon.py --user-msg "消息"
python3 chiguo_daemon.py --user-msg "消息" --analysis '{"warmth":0.7,"effort":0.8,"attention":0.9}'

# 纪念日管理
python3 chiguo_daemon.py --anniversary "add anniversary 11-03 主人生日"
python3 chiguo_daemon.py --anniversary "add countdown 2026-12-25 考试"
python3 chiguo_daemon.py --anniversary list
python3 chiguo_daemon.py --anniversary "remove <id>"
python3 chiguo_daemon.py --anniversary cleanup

# 寒暑假模式
python3 chiguo_daemon.py --break add 2026-01-12 2026-02-22 寒假   # 添加假期区间
python3 chiguo_daemon.py --break remove 0  # 删除指定区间
python3 chiguo_daemon.py --break list      # 列出所有区间
python3 chiguo_daemon.py --break on        # 手动无限期开启
python3 chiguo_daemon.py --break off       # 关闭
python3 chiguo_daemon.py --break status    # 查看完整状态

# 健康检查 & 测试
python3 chiguo_daemon.py --health          # 健康检查（daemon + 日志 + 配置）
python3 test_chiguo_math.py                # 单元测试（test_monitor/test_feedback 失败时退出码为 1）

# 监控 & 统计
python3 chiguo_daemon.py --stats           # 最近7天统计（JSON）
python3 chiguo_daemon.py --stats 30        # 最近30天统计
python3 chiguo_daemon.py --alerts          # 异常检测告警
python3 chiguo_daemon.py --monitor         # 完整监控报告
python3 chiguo_monitor.py --summary        # 人类可读摘要
python3 chiguo_watchdog.py               # 独立看门狗（JSON）
python3 chiguo_watchdog.py --quiet       # 仅异常输出（退出码驱动）
python3 chiguo_watchdog.py --notify      # stderr 告警摘要

# 环境就绪检查（只读）
python3 chiguo_envcheck.py               # 检查 Python/uv/pi/LanceDB/网易云/数据，退出码 0=就绪 1=警告 2=严重；--skip-pi 时 pi 缺失降为警告不阻塞
```

## 运行时数据回流（git 跟踪策略）

目标机运行产生的分析数据直接进仓库，方便本地 pull 分析（私人仓库）：

- **跟踪**：`chiguo_decisions.jsonl`、`chiguo_state.json`、`chiguo_messages.jsonl`、`chiguo_state_audit.jsonl`、`chiguo_message_log.json`、`chiguo_alerts.json`、`chiguo_watchdog_state.json`、`anniversaries.json`、`break_state.json`、`holidays.json`、`solar_terms.json`、`schedule_cache.json*`、`state.json`、`netease_cache.json`
- **忽略**：备份/临时文件（`*.bak`/`*.tmp`/`*.pid`/`*.lock`）与敏感 token（`netease_cookie.txt`——如确需入库须 `git add -f`）
- 目标机推送节奏与方式自定（如 cron `git add -A && git commit && git push`）

## 版本号

版本号单一来源：`chiguo_version.py` 的 `VERSION`。当前为 `v1.4`；每完成一轮修改由维护者手动 +0.1（`v1 → v1.1 → v1.2`，类似 Linux 次版本步进）。daemon 决策 JSON、`--version`、envcheck/monitor 报告均带版本号。

## 文档

- [SYSTEM.md](SYSTEM.md) — 完整系统文档（架构、业务逻辑、配置参考）
- [PI_INTEGRATION.md](PI_INTEGRATION.md) — pi-agent 集成指南（当前架构）
- [OPENCLAW_INTEGRATION.md](OPENCLAW_INTEGRATION.md) — OpenClaw 部署指南（已废弃，回退参考）
- [IMPROVE.md](IMPROVE.md) — 改进清单
- [2026-08-01-openclaw-integration-design.md](2026-08-01-openclaw-integration-design.md) — v11 集成设计文档（trigger-script + standing order）
- [2026-08-01-openclaw-integration-plan.md](2026-08-01-openclaw-integration-plan.md) — v11 集成实现计划（6 任务）
