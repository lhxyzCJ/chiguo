# 2026-08-01 寄主迁移 + 人格重构设计（pi-host-and-personality-rework）

> 状态：已批准（用户 2026-08-01 确认，全量一轮 + 分阶段执行）
> 范围：B 档寄主迁移（OpenClaw → pi-agent）+ 人格文件重构（原著对齐）+ 代码决策层/机制层修正

## 1. 背景与目标

### 1.1 寄主迁移背景
- chiguo 当前依赖 OpenClaw（2026.7.1-2，`~/.openclaw` 4.3GB + 常驻 gateway）承担：cron trigger-script 定时、main session agent 生成消息、standing order 情绪分析、skills 目录存人格文件
- 实际只用上述 4 样功能，微信渠道已独立为 wechat-bridge（v12）
- 结论：OpenClaw 对 chiguo 偏重，换寄主到 pi-agent（~876K 配置、按需执行、官方支持 opencode-go provider）

### 1.2 人格重构背景
- 全部人格文件（SUN2.md/SKILL.md/SOUL.md/IDENTITY.md/USER.md/语言技巧指南）声称基于《日光雨》原著（`doc/日光雨.md`，17099 行），经两轮子代理调研 + 全本语料挖掘，发现多处冲突：
  - 年龄 16 岁 vs 原著 14 岁；VPS 技术专家人设全书零依据（原著她是非技术萌新）；「主人」称呼代码层残留；波浪线 30-40% 是自造数字（原著 ~8%）；「哼」被过度拔高（原著 65 次/0.4% 行，48% 带省略号）；personality/*.toml 是孤立死模板（小雪/小樱，非迟菓）；8 维人格/composer cue 全部来自 soulforge/Sebastian 等外部模板；人格自适应会把迟菓漂移成甜妹/极端傲娇

### 1.3 目标
1. 寄主从 OpenClaw 迁移到 pi-agent，卸掉 4.3GB + 常驻进程，保留全部功能
2. 人格文件按原著重构，SUN2.md 成为唯一权威设定
3. 代码决策层（cue 权重/初始值/layer_guidance/称呼）与人格对齐
4. 机制层防漂移（基线回归）
5. 全量测试 + 多代理审计后由用户决定 push

## 2. 用户决策记录

| 决策 | 结论 |
|---|---|
| 迁移方案 | B 档：系统 cron + pi-agent |
| LLM 调用 | 全部走 `pi -p`（发送侧 + 回复侧） |
| 回复侧流程 | pi 一次完成（分析 JSON + 人格回复，`<<ANALYSIS>>` 标记分离） |
| 特殊命令 | pi 工具执行（纪念日/假期） |
| OpenClaw 处置 | 停用不删（gateway 停止 + cron disable，安装与数据保留作回退） |
| 记忆方案 | memory-lancedb-pro pi 版（GitHub fork lhxyzCJ/TestForPi-memory-lancedb-pro），dbPath 沿用 `~/.openclaw/memory/lancedb-pro`，embedding 用本机 ollama qwen3-embedding:0.6b |
| 模型 | opencode-go provider（`https://opencode.ai/zen/go/v1`，key 配 `~/.pi/agent/auth.json` 的 `opencode-go` 条目），默认 deepseek-v4-flash，thinking 默认 high（可配） |
| VPS 设定 | 保留赛博世界观但去技术专家：赛博萌新（住 VPS 但不懂技术，哥哥操作时在旁边看、想学、搞砸了嘴硬） |
| 关系线 | 完整恋人线（外卖员×顾客 → 兄妹游戏 → 恋人） |
| 人格自适应 | 保留 + 基线回归（防漂移） |
| personality/*.toml | 重写为迟菓模板并接线 |
| 修订边界 | 全量一轮（文件层 + 代码层 + 机制层 + 迁移） |
| 收尾 | 全量测试通过 → 报告 → 用户决定 push（不擅自提交） |

## 3. 架构（迁移后）

```
发送侧（系统 crontab，替代 openclaw cron）：
crontab: */15 * * * * scripts/chiguo-tick.sh
  → daemon --compact（零 LLM）
    ├─ idle → 退出
    └─ send → scripts/pi-run.mjs 生成 1-3 句（SUN2.md + 语气教程注入）
        → curl --noproxy '*' POST 127.0.0.1:18790/send → daemon --record-send/--send-result

回复侧（wechat-bridge/bridge.mjs 改造）：
微信消息 → recordUserMsg（确定性 --user-msg）→ askPi 一次完成：
  system prompt 要求先输出 <<ANALYSIS>>{...}<<END>> JSON
  → 解析 JSON → daemon --user-msg --analysis（recv_dedup 升级）
  → 回复文本 → bot.reply
  特殊命令（纪念日/假期）→ pi 工具执行 daemon CLI

记忆（memory-lancedb-pro pi 版）：
  dbPath = ~/.openclaw/memory/lancedb-pro（历史记忆无缝）
  embedding = ollama qwen3-embedding:0.6b（localhost:11434）
  autoCapture/autoRecall/smartExtraction 全开

LLM 调用统一封装：scripts/pi-run.mjs
  pi -p --provider opencode-go --model <toml> --session-id <toml> \
     --append-system-prompt <repo>/personality/SUN2.md \
     --append-system-prompt <repo>/personality/迟菓语言技巧指南.md \
     --no-context-files --thinking <toml> <消息>
```

## 4. 分阶段执行计划

### Phase 1：人格文件层（基础，先行）
- 1.1 重写 `personality/SUN2.md`（从 `~/.openclaw/workspace/skills/chiguo/SUN2.md` 拷入后重写，10 节框架保留、逐节对齐原著）
- 1.2 扩充 `personality/迟菓语言技巧指南.md`（6 层结构：语气词手册/句式模式库/情绪映射/对象差异矩阵/范例片段/自查清单）
- 1.3 SOUL.md / IDENTITY.md / USER.md 归一合并进 SUN2.md（USER.md 运维约束移入 toml 或 pi 全局 AGENTS.md）
- 验证：对比报告 v2 逐条列改动前后对照 + 原著行号；人格一致性子代理审计

### Phase 2：代码决策层（对齐新人格）
- 2.1 `chiguo_daemon.py`「主人」→「哥哥」全清（:838/:846/:749/:766/:823-834/:1097）
- 2.2 `chiguo_personality.py` 初始值修正（tsundere 70→75 或调阈值消除 cool 分支矛盾；extraversion 45→60；agreeableness 70→65）
- 2.3 `chiguo_composer.py` cue 权重重排：tsundere_classic 0.30→0.40、caring_gentle 0.25→0.10（morning/meal 调制 ×1.5→×1.0）、playful_bubbly 0.20→0.15（去嘻嘻高频）、anxious_clingy 0.15→0.10、tsundere_cool 0.10→0.05、dere_dere 0.10→0.05、cool_mysterious 0.05→0、新增 trade_tsundere 0.15（交易式撒娇）；INTENTS 新增 compensate；style_hint 补动作/神态指令
- 2.4 `personality/tsundere.toml` 重写为迟菓模板、`deredere.toml` 重写为「防线融化」状态并接线（composer 加载替代硬编码；接线成本过高则退路为删除两个 toml）
- 2.5 `chiguo_daemon.py` layer_guidance（:698-786）对齐：哼低频、波浪线 ~10%、交易思维铁律、非技术萌新铁律强化
- 验证：全量 19 文件回归（test_personality/test_composer/test_trigger/test_integration 优先）+ 新增测试

### Phase 3：机制层（防漂移）
- 3.1 `chiguo_state.py` adapt_personality 加软基线回归：`v += (baseline - v) * regress_rate`，toml `[personality] regress_rate = 0.01`（可调 0=关闭）
- 3.2 personality_history：实现写入（append {ts, dims}，上限 200 条滚动）或删除 SYSTEM.md:641 声称（推荐实现）
- 验证：新写 test_adapt_personality.py（100 次热情回复 tsundere >50；200 次沉默 <85）+ 全量回归

### Phase 4：B 档迁移（最后，带定稿人格）
- 4.1 新增 `scripts/pi-run.mjs`：pi 调用封装 + NDJSON 解析（取 message_end text + `<<ANALYSIS>>` 提取）+ 超时 120s + 重试 1 次；配置读 toml `[host]` 段
- 4.2 新增 `scripts/chiguo-tick.sh`：crontab 入口（daemon --compact → idle 退出 / send → pi-run → bridge → record-send）
- 4.3 改造 `wechat-bridge/bridge.mjs`：askOpenClaw → askPi；分析 JSON 解析 → daemon --analysis → reply；特殊命令由 SUN2 命令表触发 pi 工具
- 4.4 新增 `scripts/install_pi.sh`：幂等、--dry-run/--yes、退出码 0/1/2；检测 pi、clone+build memory-lancedb-pro、写 settings.json extensions + memory-lancedb-pro.json5（dbPath 沿用旧库 + ollama embedding）、ollama 检查、auth.json 写 opencode-go（key 从 `OPENCODE_API_KEY` 环境变量读，不落盘明文）、crontab 注册、冒烟验证
- 4.5 配置接线：toml `[openclaw]` → `[host]`（provider/model/thinking/session_id/personality_dir）；`chiguo_envcheck.py` 改 pi 检查；`deploy.sh` 接入 install_pi.sh（--skip-pi）；文档 OPENCLAW_INTEGRATION.md → PI_INTEGRATION.md（OpenClaw 降级回退附录）
- 4.6 OpenClaw 停用：cron disable + 停 gateway，安装与数据保留
- 验证：pi-run 单测（4 路径）、tick 冒烟、bridge 真实消息、集成冒烟

### Phase 5：全量测试 + 审计（收尾）
- 5.1 测试矩阵：
  - A 存量：19 py + 3 脚本（node test_trigger_script.js、test_install_integration.sh、test_wechat_bridge.sh）全过
  - B 新增：test_pi_run.mjs（4 路径）、test_adapt_personality.py（回归生效）、test_composer_trade.py（新 cue/权重和）、test_personality_init.py（经典分支）、test_toml_binding.py（toml 接线）、test_bridge_pi.mjs（askPi 参数）、test_install_pi.sh（dry-run）
  - C 集成冒烟：pi 实调（opencode-go 网关 + 人格注入）、memory-pro stats/list + 写入删除一条测试记忆（验证 fork 扩展 + dbPath + ollama embedding）、bridge 真实消息、tick 手动触发 → 微信收到
- 5.2 审计：每阶段自审计（子代理对照 spec 逐条）、P1 后人格一致性审计、P2/P3 后 code-review skill（Standards + Spec 双轴并行）、P5 全量终审（多子代理并行：决策引擎/bridge+pi-run 集成/测试质量/文档一致性）、verification-before-completion 验收门槛（所有测试实际运行确认通过）
- 5.3 收尾协议：全量测试通过 + 审计无遗留 → 更新文档（SYSTEM/IMPROVE/README/MEMORY）→ 报告用户 → 用户决定 push

## 5. 关键事实与技术要点

- pi-agent 0.83.0：`pi -p` 非交互；`--mode json` 输出 NDJSON 事件流（解析取 message_end 的 text）；session 按 cwd 分桶存储（同 cwd + 同 --session-id 续接保留上下文）；自动发现 cwd 的 AGENTS.md/CLAUDE.md（用 --no-context-files 隔离仓库开发上下文）；`--append-system-prompt` 接受文件路径；`--thinking` 挡位 off/minimal/low/medium/high/xhigh/max（默认 high）
- opencode-go provider：pi 官方内置（auth.json 键名 `opencode-go`，环境变量 `OPENCODE_API_KEY`），网关 `https://opencode.ai/zen/go/v1`，可用模型含 deepseek-v4-flash/v4-pro/kimi/glm/minimax/qwen 等 24 个
- memory-lancedb-pro pi 版（1.1.0-beta.11-pi.1）：`pi -e dist/pi-adapter/index.js` 或 settings.json extensions；配置 `~/.pi/agent/memory-lancedb-pro.json5`（本机已配：ollama qwen3-embedding:0.6b 1024dims + deepseek llm）；CLI `memory-pro list/search/stats`；canonical corpus 索引 MEMORY.md/memory/**；注意：`~/.pi/agent/settings.json` 现有 extensions 指向 Windows 路径（/mnt/c/...，本机不存在），需修正为 Linux 路径
- DeepSeek 旧 key（auth.json `deepseek` 条目 `sk-a8592ea...0b29`）已失效（401），不影响迁移（改用 opencode-go）
- 原著关键事实：14 岁初二生+外卖员；哼 65 次/0.4% 行（48% 带省略号）；波浪线 260 行≈8%；嘻嘻 10 次；喵 0 次（只说小白口译）；「不·需·要。」×3；两不相欠/补偿/交易思维；行动主动（两次先吻）；自称「迟菓」不称「菓菓」；不懂技术英语差；2010 年代背景

## 6. 范围外（YAGNI）

- 不迁移历史 decisions.jsonl / 不重写 daemon 决策核心
- 不做双寄主并行运行（OpenClaw 停用即切）
- 不实现 OpenClaw 的 DREAMS.md/HEARTBEAT.md 对应机制（pi 无此生命周期，chiguo 决策引擎不依赖）
- 不把 key 明文写入仓库（环境变量/安装器交互方式）
- 不做 Windows 路径兼容（/mnt/c 是开发遗留）

## 7. 文档出处索引

- pi 官方文档：pi.dev/docs/latest（usage/providers/sessions/context files/custom models）
- pi GitHub：github.com/earendil-works/pi
- memory-lancedb-pro pi 版：github.com/lhxyzCJ/TestForPi-memory-lancedb-pro（README 有 pi-adapter 说明）
- 原著：《日光雨.md》（仓库 doc/）
