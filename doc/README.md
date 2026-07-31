# 迟菓主动消息系统

角色主动消息守护进程 — 数学驱动的决策引擎 + OpenClaw LLM 消息生成。

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
  └─ 输出 JSON → OpenClaw 读取 → 生成消息 → 微信发送
```

决策与生成分离：daemon 只输出结构化 JSON，不调用 LLM。消息生成由 OpenClaw 的 chiguo skill 完成。

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

主人回复时，OpenClaw 可传入 `--analysis` JSON（warmth/effort/attention），daemon 据此差异化情绪变化。热情回复好感大幅上升，敷衍回复几乎不涨。

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
test_*.py                # 测试（19 个文件，含 test_circadian/test_followup/test_netease_proof/test_netease_service/test_envcheck）
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
python3 chiguo_envcheck.py               # 检查 Python/OpenClaw/LanceDB/网易云/数据，退出码 0=就绪 1=警告 2=严重
```

## 文档

- [SYSTEM.md](SYSTEM.md) — 完整系统文档（架构、业务逻辑、配置参考）
- [OPENCLAW_INTEGRATION.md](OPENCLAW_INTEGRATION.md) — OpenClaw 部署指南
- [IMPROVE.md](IMPROVE.md) — 改进清单
