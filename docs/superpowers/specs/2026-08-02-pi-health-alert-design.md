# pi-agent 假死检测与微信告警 — 设计文档

- 日期: 2026-08-02
- 状态: 已批准（用户确认盲区：空闲期假死不主动探测，延迟到下次真实交互）

## 问题

wechat-bridge（微信接入）与 pi-agent（回复/主动消息生成后端）是两套独立进程。pi-agent 假死
（进程/API key/模型不可达）时，bridge 仍在线收消息——用户收到 `⚠️ 处理失败` 或无主动消息，
没有系统级告警告知"后端已挂"。

## 设计约束（用户明确）

1. **零新增 token**：不做定时探测。所有检测挂在本来就发生的真实 pi 调用上（bridge askPi / tick pi-run），失败/成功顺带记账。
2. **pi 零修改**：不动 pi-run.mjs、pi 二进制、上游 agent。检测不注入人格文件、不新增会话。
3. 假死盲区（用户已接受）：长期空闲期（无人发消息 + daemon 判定 idle）pi 挂掉 → 告警延迟到下次真实交互时立即暴露。
4. 告警方式：微信直接发消息给哥哥（OWNER_ID）。发故障告警 + 恢复通知。不自动重启 pi。

## 架构

三个信号来源汇入共享状态文件，告警文案由状态机统一生成：

- **bridge.mjs**（回复链路）：askPi 成功/失败 → 记账；transition 触发 → `bot.send` 告警/恢复。现有逐条 `⚠️ 处理失败` 回复保留。
- **scripts/chiguo-tick.sh**（主动链路）：pi-run 成功/失败 → 记账；transition 触发 → `curl --noproxy '*'` `/send` 告警/恢复。修复"tick 失败无人知"的洞。
- **scripts/pi_health.py**（新增）：唯一状态机实现。flock + 原子写维护 `pi_health.json`；stdout 输出 `{state, transition, message}`。

### pi_health.py

CLI: `pi_health.py record --outcome fail|success [--reason <r>] [--config <path>] [--state <path>]`

- `--config` 默认锚定脚本所在仓库的 `chiguo_proactive.toml`；`[health].fail_threshold` 决定阈值（默认 3；bool/float/非整型/0/负值视为无效回退 3）
- `--state` 默认 `Path(__file__).parent.parent / "pi_health.json"`（仓库根）
- 状态文件字段：`{state: up|down, fail_streak, last_fail_at, last_success_at, fail_reason, changed_at}`
  - fail_reason 保留**本串首次失败**的原因（不覆盖），供告警诊断
- 状态机：
  - fail → fail_streak+1；`fail_streak ≥ threshold` 且此前 up → state=down，transition=down（仅这一次）
  - down 期间继续 fail → transition=none（防重复告警）
  - down 后首次 success → state=up，transition=up（恢复通知，仅这一次）
  - up 期间 success → transition=none
- 写入：`.tmp` + `os.replace` 原子写；`fcntl.flock`（LOCK_EX | LOCK_NB，短超时，照 chiguo_state.py `_lock_acquire` 模式）；**锁获取失败（5s）→ 本次不写并 stderr 告警**（宁丢一次记账，不无锁写共享 .tmp）
- 输出（stdout JSON）：`{"state", "transition": "none|down|up", "message"}` — message 为现成告警/恢复文案，发送方只负责投递
  - down: `⚠️ 后端异常：pi-agent 连续 N 次调用失败（原因：…）。回复和主动消息都会受影响，我还在线但脑子不转了～恢复后告诉你`
  - up: `✅ 后端已恢复，我活过来了！`

### bridge.mjs 改动

- 新 helper `recordPiHealth(bot, outcome, reason)`：execFileP 调 pi_health.py（30s 超时），解析 stdout JSON，transition 非 none 时 `bot.send(OWNER_ID, message)`（投递失败打 `[pi health alert send error]` 日志）；整体 `.catch(()=>{})` —— 记账失败绝不影响回复流
- 解释器独立环境变量 `WECHAT_BRIDGE_PI_HEALTH_PY`（默认 `/root/chiguo/.venv/bin/python`，不随 DAEMON_PY 走——测试可能把后者换成 node 跑 fake daemon）；`WECHAT_BRIDGE_PI_HEALTH` 覆盖脚本路径（默认 `new URL('../scripts/pi_health.py', import.meta.url)`，照 PI_RUN_SCRIPT 模式）
- 接入点：`handleMessage` askPi 成功路径（reply 后）→ record success；catch 路径（`⚠️ 处理失败` 回复后）→ record fail（reason = 错误消息截断 100 字符）。**bot.reply 移出 try 独立 `.catch`**——微信发送故障 ≠ pi 假死，不得误记 fail
- 特殊命令路径不经 pi → 不记账（已正确）

### chiguo-tick.sh 改动

- 失败分支（TEXT 为空）：`pi_health.py record --outcome fail --reason 'tick pi-run 未生成消息'`；transition=down → curl `/send` 告警。退出码语义不变（exit 1）
- 成功分支：`record --outcome success`；transition=up → curl `/send` 恢复
- 用 `$PY`（.venv 3.14）执行

### 配置

`chiguo_proactive.toml` 新增：

```toml
[health]
fail_threshold = 3
```

### 状态文件

`pi_health.json`（仓库根，跟 chiguo_watchdog_state.json 一样默认 git 跟踪——便于故障后诊断推送）

## 测试策略（TDD，先红后绿）

- `test_pi_health.py`（独立 runner + assert）：状态机全矩阵（fail×2 不触发 / ×3 触发 down / ×4 去重 / 恢复 / 恢复后 success 无 transition / 首次失败原因保留 / 阈值从 toml 读 / 回退 3 / 原子写无 .tmp 残留）
- `test_bridge_health.mjs`（复用 test_bridge_askpi.mjs fake 模式：fake pi-run + stub bot 记录 send）：连续失败 ×3 → 恰 1 次告警；×4 无新告警；恢复 1 次；正常路径零告警；记账失败不阻塞回复流
- `test_tick_health.sh`（temp repo + fake daemon/pi-run + node http 记录服务）：失败 → 状态文件记账；×3 → 收到 1 次告警 POST

## 不做（YAGNI）

- 不自动重启 pi-agent
- 不主动探测（无心跳、无定时器、无新会话）
- 不检测 bridge 自身假死（另一个问题域）
- pi-run.mjs / pi 二进制 / 上游零改动

## 文档更新（AGENTS.md 铁律）

- `doc/SYSTEM.md`、`doc/IMPROVE.md`、`MEMORY.md`
