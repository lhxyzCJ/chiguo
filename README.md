# 迟菓主动消息系统 (Chiguo)

> 一个会主动找主人聊天的 AI 角色 —— 数学驱动的决策引擎 + LLM 消息生成,微信触达。

**迟菓**是一套"角色主动消息"系统:由零 LLM 的数学决策引擎(`chiguo_daemon.py`)基于**情绪模型、生物钟学习、触发评估、话题注入**等机制决定"何时、以什么心情、聊什么",输出结构化 JSON;再由 [OpenClaw](https://www.openclaw.ai) 读取 JSON 生成拟人消息并通过微信发送。

- **决策与生成分离**:daemon 永不调用 LLM、永不生成消息文本,只输出 JSON
- **无需外部服务**:情绪推进、发送门控、触发评估、话题选择全部本地计算
- **可解释**:13 种触发类型、5 维情绪、8 大话题来源,全部参数可在 `chiguo_proactive.toml` 调整

## 特性一览

| 特性 | 说明 |
|------|------|
| 5 维情绪引擎 | 孤独/好感/不安/元气/傲娇,半衰期推进,主人回复实时响应 |
| 生物钟学习 | 双作息双桶分桶学习,动态静默窗口(深夜不打扰) |
| 13 种触发 | sigmoid 权重 + 加权随机,替代硬阈值与优先级排序 |
| 8 大话题来源 | 课表/假期、记忆回忆、节气、纪念日、天气、网易云音乐… |
| 听歌双向联动 | 睡眠窗口内播放音乐反证未睡 + 反向校正生物钟 |
| Bayesian 用户状态 | 6 状态在线推断:忙碌/空闲/情绪/作息 |
| 消息组合系统 | Intent × Cue × Vibe 三层组合,风格可人格化 |
| 寒暑假模式 | 节假日 / 寒假暑假手动或自动切换 |
| 结构化监控 | stats / alerts / health + 独立看门狗进程 |

## 快速开始

需要 **Python 3.14+**(通过 uv 安装:`uv python install 3.14`)。

```bash
cd <仓库根目录>

# 查看当前状态
python3 chiguo_daemon.py --status

# 单次决策(输出 JSON)
python3 chiguo_daemon.py

# 交互式 Demo
python3 chiguo_demo.py

# 跑全部测试(19 个 py 文件 + 2 个脚本测试)
node test_trigger_script.js && bash test_install_integration.sh && \
uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && \
uv run python test_integration.py && uv run python test_monitor.py && \
uv run python test_eventbus.py && uv run python test_personality.py && \
uv run python test_bayesian.py && uv run python test_composer.py && \
uv run python test_ebbinghaus.py && uv run python test_longing.py && \
uv run python test_escape_valve.py && uv run python test_feedback.py && \
uv run python test_trigger.py && uv run python test_topics.py && \
uv run python test_circadian.py && uv run python test_followup.py && \
uv run python test_netease_proof.py && uv run python test_netease_service.py && \
uv run python test_envcheck.py
```

> 注意:集成测试需要当前目录存在 `chiguo_proactive.toml`,请始终从项目根目录运行。

## 部署到其他机器

项目已做多机解耦:运行时文件全部 gitignore;路径基于 config 所在目录/`~` 解析(与 cwd、用户无关);机器相关配置集中在 `chiguo_proactive.toml`。新机器上:

```bash
git clone <仓库地址> && cd <仓库目录>
bash deploy.sh      # 装 uv/Python 3.14 → 建 venv → 全量 19 测试 → 环境检查(OpenClaw skill/网易云登录) → OpenClaw 集成安装(install_integration.sh) + automations 冒烟提示
```

部署脚本会检查:OpenClaw skill 目录(`~/.openclaw/workspace/skills/chiguo/`)、网易云登录(`netease_cookie.txt`,首次需 `uv run python netease_bridge.py --login` 扫码)、以及迁移旧机运行时文件(`chiguo_state.json`/`chiguo_decisions.jsonl` 等,如从旧运行机迁移)。OpenClaw 集成(trigger-script 门控 + standing order)由 `scripts/install_integration.sh` 自动完成,定时作业经 `openclaw automations --trigger-script` 注册,完整指南见 `doc/OPENCLAW_INTEGRATION.md`。

## 架构

```
chiguo_daemon.py(决策引擎,零 LLM)
  ├─ 情绪推进(半衰期衰减)
  ├─ 发送门控(静默/上限/间隔/元气)
  ├─ 触发评估(sigmoid 权重 + 加权随机)
  ├─ 话题注入(8 来源破冰)
  ├─ 生物钟学习(circadian:双作息双桶分桶学习 → 动态静默窗口)
  ├─ 接话茬(follow_up:pending 话题续聊)
  ├─ 听歌双向联动(netease:睡眠窗口内播放反证 sleeping + 反向校正生物钟)
  ├─ 音乐话题源(netease 策略层:第 8 源 + 降级链 + peek/consume 两阶段配额)
  └─ 输出 JSON → OpenClaw 读取 → 生成消息 → 微信发送
```

## 核心机制

### 5 维情绪

| 维度 | 范围 | 推进方式 |
|------|------|------|
| 孤独值 | 0-100 | 半衰期 40h 向 100 靠拢;主人回复 0.35h 骤降 |
| 好感度 | 5-100 | 半衰期 500h 向 0 靠拢;主人回复 +0.8 |
| 不安值 | 0-100 | 半衰期 30h 向 100 靠拢;主人回复 0.5h 骤降 |
| 元气值 | 0-100 | 半衰期 8h 恢复;发消息 -20 |
| 傲娇度 | 10-95 | 好感>65 降低,不安>60 升高 |

### 触发与话题

13 种触发类型(v7 含 follow_up 接话茬),sigmoid 替代硬阈值,加权随机替代优先级排序。触发后可从 8 个来源注入话题破冰:课表/假期、LanceDB 随机回忆、通用关心、天气季节、纪念日/倒计时、24 节气、偏好追问、网易云音乐。连续 3 次孤独触发 → 强制注入话题。

### LLM 内容分析

主人回复时,OpenClaw 可传入 `--analysis` JSON(warmth/effort/attention),daemon 据此差异化情绪变化——热情回复好感大幅上升,敷衍回复几乎不涨。

## 文件结构

```
chiguo_proactive.toml    # 主配置(所有参数,含 personality/bayesian/composer/netease 段)
chiguo_daemon.py         # 决策引擎(主入口)
chiguo_state.py          # 情绪引擎 + 人格 + Bayesian + 课表 + 节假日 + 记忆 + circadian
chiguo_circadian.py      # 生物钟学习(双作息双桶分桶学习)
chiguo_trigger.py        # 触发器(sigmoid 权重 + 加权随机 + follow_up 接话茬)
chiguo_topics.py         # 话题选择器(8 来源 + Ebbinghaus + 人格调制 + netease 委托)
chiguo_math.py           # 数学库(sigmoid/半衰期/Hawkes/概率累积)
chiguo_personality.py    # 多维人格系统(Big Five + 角色特有 8 维)
chiguo_bayesian.py       # Bayesian 用户状态推断(6 状态 + 在线学习)
chiguo_composer.py       # 消息组合系统(Intent × Cue × Vibe 三层)
chiguo_eventbus.py       # 轻量事件总线(发布/订阅模块解耦)
netease_bridge.py        # 网易云桥接(最近播放记录 + 缓存 + 有限重试)
chiguo_netease.py        # 网易云策略层(健康状态/降级链/配额/话题素材组装)
schedule_parser.py       # 课表解析(xskb.xlsx → cache)
holiday_parser.py        # 节假日判断(2026 国务院安排)
solar_terms.py           # 24 节气查询
memory_bridge.py         # LanceDB 只读桥接 + Ebbinghaus 遗忘
anniversary_manager.py   # 纪念日/倒计时 CRUD
chiguo_monitor.py        # 结构化监控(stats / alerts / health)
chiguo_watchdog.py       # 独立看门狗(停滞检测,超 3h 告警)
chiguo_demo.py           # 演示模式
test_*.py                # 测试(19 个文件,300+ 用例,独立 runner)
data/                    # 数据文件:课表 xskb.xlsx、手动记忆 chiguo_memories.json、网易云二维码 netease_qr.png
```

## CLI 参考

```bash
# 决策引擎
python3 chiguo_daemon.py                  # 单次决策
python3 chiguo_daemon.py --status         # 查看状态
python3 chiguo_daemon.py --compact        # 紧凑模式(idle 不输出)
python3 chiguo_daemon.py --loop 120       # 持续运行(间隔最小 60 秒)

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
python3 chiguo_daemon.py --break add 2026-01-12 2026-02-22 寒假
python3 chiguo_daemon.py --break list / remove 0 / on / off / status

# 健康检查 & 监控
python3 chiguo_daemon.py --health          # 健康检查(daemon + 日志 + 配置)
python3 chiguo_daemon.py --stats 30        # 最近 30 天统计(JSON)
python3 chiguo_daemon.py --alerts          # 异常检测告警
python3 chiguo_daemon.py --monitor         # 完整监控报告
python3 chiguo_monitor.py --summary        # 人类可读摘要
python3 chiguo_watchdog.py --quiet         # 看门狗(退出码驱动)

# 环境就绪检查(只读)
python3 chiguo_envcheck.py                 # 检查 Python/OpenClaw/LanceDB/网易云/数据,退出码 0=就绪 1=警告 2=严重
```

## 文档

- [doc/SYSTEM.md](doc/SYSTEM.md) — 完整系统文档(架构、业务逻辑、配置参考)
- [doc/OPENCLAW_INTEGRATION.md](doc/OPENCLAW_INTEGRATION.md) — OpenClaw 部署指南
- [doc/IMPROVE.md](doc/IMPROVE.md) — 改进清单
- [doc/README.md](doc/README.md) — 内部部署版说明
- [CLAUDE.md](CLAUDE.md) / [CLAUDE_CODE_RULES.md](CLAUDE_CODE_RULES.md) — 开发约定与代码规则

## License

[MIT](LICENSE) © 2026 lhxyzCJ
