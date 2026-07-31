# v6 系统修复设计 — 溢出逃生阀 / 状态路径修复 / 反馈闭环

日期: 2026-07-31
状态: 已批准（用户审阅对话版设计后确认）
范围: 单一 spec，三处系统级修复

## 背景（实测证据）

- 死锁态: `chiguo_state.json` anxiety=100 封顶（阻塞阈值 70），loneliness=69，silent_hours=999，连续 6 条无回复。anxiety 阻塞 longing 累积 → 高孤独转化不成发送 → 破防型消息永远不发生。12 种触发仅 3 种出现过，`longing` 从未触发。
- 状态健忘症: 96 条审计事件 94 条 `state_fresh_start`。状态文件反复丢失重建，情绪连续性断裂，好感从 55 漂到 27.5。
- 反馈闭环缺失: daemon 不知发送成败。失败消息照扣元气、照占日限额；统计失真。

## §1 溢出逃生阀

**意图**: 沉默极久 + 高焦虑时，思念累积到极限突破焦虑阻塞，发一条破防消息（傲娇人设高光时刻）。复用现有 longing 累积与 overflow 机制，不加新概念。

### 机制

实现时发现：`longing_accumulate` 在焦虑阻塞时不增长，且 daemon 的 `accumulated_lambda or current_lambda(now)` 回退值在阻塞态被 availability×0.3 + 退避系数压到 ~0.008，永远够不到 overflow 的 `base×1.5` 条件。λ 阈值触发在阻塞态自我否决 → **触发条件改为时间+状态驱动**：

- 触发条件：`anxiety ≥ anxiety_block_threshold`（阻塞态）且 `silent_hours_wall ≥ longing_break_min_silence_hours`（默认 72h）且冷却期外。
- 触发后记录 `last_longing_break_at` 进入冷却；held/accumulated 由现有 `on_character_message` 发送后清零逻辑自然归零（不需要 drain_ratio）。
- `chiguo_trigger.py`: 逃生阀激活 → 直接返回 `Trigger("longing", "high", {"escape_valve": True})`，不走加权随机（现有 longing 触发在焦虑高时权重≈0，永远选不中）。
- `chiguo_daemon.py _build_context`: `trigger.data["escape_valve"]` → 追加 `【破防】` 语气标记（真挚克制，不质问不卖惨）。
- `can_send` 日限额对逃生阀放行（`is_longing_overflow()` 或 `longing_break_eligible()` 均可突破 daily_max）；min_interval 与 quiet_hours 照常执行。
- 用户回复后 anxiety 骤降，机制自然休眠。

### 配置（`chiguo_proactive.toml` `[cooldown]` 节新增）

```toml
longing_break_enabled = true             # 总开关
longing_break_min_silence_hours = 72     # 沉默多久触发破防（墙钟）
longing_break_cooldown_days = 3          # 破防后冷却天数，防刷屏
```

## §2 状态路径修复

**根因假设**（实现时验证）: cron 工作目录漂移 + 多实例竞争写（checksum_mismatch → 删重建）。

- 路径锚定: 所有运行时文件（state/decisions/audit/alerts/watchdog）基于 config_path 所在目录解析，不依赖 cwd。启动日志打印解析后绝对路径。
- 并发写保护: state 写操作加 `fcntl.flock`（原子写已有，补跨进程锁）。`--loop` PID 锁已有，补单次 cron 写锁。
- 损坏降级保留: checksum 不匹配仍走 .bak 恢复，只修路径与并发，不动恢复策略。

## §3 反馈闭环

- 新 CLI: `--send-result <MSG_ID> --send-status success|failed [--error <code>]`（实现时改名：`--status` 已被状态视图占用，`--msg-id` 简化为 `--send-result` 直接传值）
- 失败退款: 退还 `energy_cost_per_message` + `anxiety_gain_on_send`；日发送计数回退（不占 `max_daily_silent` 额度）；`messages_without_reply` 回退（发送时已递增）；下次 tick 可重发。
- 结果记录: `send_result` 条目写入 decisions.jsonl，`refunded` 标志（true=failed 已退款，false=success 等价 delivered）；监控统计 `send_success`/`send_failed`。
- SKILL.md（允许路径内）: 生成端发送后加一步回传 `--send-result`，失败路径（掉线/过期）也回传。

## 测试

沿用 tempfile 隔离模式:
- 逃生阀: anxiety=100 + 沉默72h+ → 必发；冷却期内不重发；非阻塞态不触发；日限额放行。
- 路径: 从不同 cwd 运行 → 状态文件仍找到。
- 反馈: failed → 元气/焦虑退款、日额度回退、可重试；success → 统计入账。

## 文档

按 CLAUDE.md: 更新 doc/SYSTEM.md（架构/CLI/配置参考）、doc/IMPROVE.md（#9 反馈闭环、死锁、路径修复条目状态）、root MEMORY.md（本次改动记录）。

## 不做（YAGNI）

- 不新增消息状态机/送达/已读追踪（微信侧无 API，低价值）。
- 不改 2027 节假日数据（等国务院通知，独立事项）。
- 不顺手重构 50+ `except: pass`（独立清理事项，风险外溢）。
- 不补 topics/solar_terms/watchdog 等零测试模块（独立事项，本次范围外）。
