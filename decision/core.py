"""decision.core — DecisionEngine 核心决策逻辑（evaluate 主链 / tick / idle / 触发生成）。

拆自 chiguo_daemon.py。职责：评估当前状态 → 输出触发决策 JSON。
不生成消息、不调 LLM、不发送。消息生成与发送由 agent 后端完成。
"""
import sys
import json
import os
import time
import math
import random
import uuid
from datetime import date, datetime, timezone, timedelta

from decision.base import DecisionEngineBase
from decision.idle import IdleMixin
from chiguo_trigger import evaluate_triggers
from chiguo_version import VERSION
from chiguo_math import in_quiet_window
from chiguo_circadian import bucket_for
from trigger_types import TriggerType  # T7·Q3 (#265) 移植：触发类型枚举单一事实源
from decision_schema import with_contract, validate as validate_decision  # Q16 移植：决策 JSON schema 写前校验

CST = timezone(timedelta(hours=8))

# ── F-A16-02 (#335): 重启域情绪推进保守封顶上限（小时）──
# 系统重启后 time.monotonic() 归零 < 持久化旧 mono_anchor（或异机迁移时钟域切换）
# 时，持久化单调锚点的真实流逝不可得。若不封顶走壁钟，NTP 前跳会把壁钟差全量推进
# 情绪（Bug 机制）；故该域把 elapsed 封顶到本常量（30min），防单次 evaluate 前跳冲击，
# 同时 save 时锚点对刷新、后继轮次自愈。CONTRACT-013 跨重启依旧有效。
REBOOT_ELAPSED_CAP_H = 0.5


def json_default(o):
    """decision 兜底类型化转换器（F-A22-001 RF4，L1-1/L1-3）。

    对已知非 JSON 原生类型做**类型保持**转换，别用 default=str/default=list 这类
    会让字段语义变形或对不可迭代对象换抛新 TypeError 的粗兜底：
      - set          → sorted list（确定性排序，读侧按 list 消费拿到正确形状）
      - datetime/date→ isoformat 字符串（.isoformat() 保持可逆语义）
    其余未知类型**保持抛 TypeError**——兜底不掩盖字段失真，失败依旧可见。
    共用 decision/_log 落盘与 cli/dispatch --compact 输出两个序列化出口。
    """
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _is_reminder_trigger(trigger) -> bool:
    """D4 (#349)：判断触发是否为用户显式托付的 reminder 一次性记忆。

    与 chiguo_trigger 的 reminder 分支判定同源：MEMORY 类型 + data.memory.type ==
    "reminder"。R9 (F-A5-01) 只在触发层给 reminder 高段优先（候选先于情绪高段分支），
    但 reminder 分支**不设** must_send 标记——因此 R13 (#315) 的二次门禁探测（只认
    probe_trigger.data 的 must_send 标记）在配额满时识别不到 reminder，把"提醒准时优先"
    的豁免在门禁层退化（review-batchB M1 影响面 a）。门禁层显式识别 reminder，即可与
    must_send 复用同一把突破钥匙（can_send(must_send=True)，含超额每日封顶语义）。
    返回 None-safe。
    """
    return (trigger is not None
            and getattr(trigger, "type", None) == TriggerType.MEMORY
            and (trigger.data.get("memory") or {}).get("type") == "reminder")


class DecisionCoreMixin(IdleMixin):
        @staticmethod
        def _make_msg_id() -> str:
            """生成唯一消息ID（12位hex，~10^14空间，单用户系统足够）"""
            return uuid.uuid4().hex[:12]

        def _log(self, decision: dict):
            """追加决策到 JSONL 日志。自动添加 msg_id + 契约版本键 contract。

            Q16 移植：决策 JSON 全部经集中 schema（decision_schema.py）写前校验并统一加
            contract 契约键；校验失败是编程错误 → 抛错（不写坏日志），不静默吞掉。
            """
            if "msg_id" not in decision:
                decision["msg_id"] = self._make_msg_id()
            # 统一加契约版本键；历史 jsonl 读取侧按缺省 1 兼容（见 decision_schema）。
            decision = with_contract(decision)
            errors = validate_decision(decision, require_contract=True)
            if errors:
                raise ValueError("决策 JSON schema 校验失败: " + "; ".join(errors))
            try:
                from chiguo_atomic import append_jsonl_0600
                append_jsonl_0600(self.log_path, decision)
            except (TypeError, ValueError) as e:
                # F-A22-001 加固：决策含非 JSON 类型（如 set）时不再裸吞。
                # 计数 + 明确 stderr + audit 事件（RF5 持久化，防进程重启归零/
                # cron stderr 难检索），并尝试类型化兜底写出（RF4），避免决策数据丢失。
                self._decision_write_failures = getattr(
                    self, "_decision_write_failures", 0) + 1
                print(
                    f"[error] 决策 JSON 序列化失败（累计 {self._decision_write_failures} 次）"
                    f" {self.log_path} msg_id={decision.get('msg_id')}: {e}",
                    file=sys.stderr)
                self.state.audit(
                    "decision_write_serialization_failed",
                    f"msg_id={decision.get('msg_id')}: {e}")
                try:
                    from chiguo_atomic import append_jsonl_0600
                    append_jsonl_0600(self.log_path, json.loads(json.dumps(decision, ensure_ascii=False, default=json_default)))
                except Exception as e2:
                    print(f"[warn] 写入 {self.log_path} 失败: {e2}", file=sys.stderr)
            except OSError as e:
                # 磁盘级 I/O 失败（非序列化问题）：仅告警，不吞，不影响主流程。
                print(f"[warn] 写入 {self.log_path} 失败: {e}", file=sys.stderr)

        def _check_data_freshness(self) -> str | None:
            """
            检测节假日数据是否覆盖下一年。
            11-12 月时：如果 holidays.json 不包含下一年数据 → 返回提示。
            """
            now = datetime.now(CST)
            if now.month < 11:
                return None
            next_year = now.year + 1
            # 检查 holidays.json 是否有下一年数据（v6: 锚定 base_dir）
            hp = self._base_dir / "holidays.json"
            covers_next = False
            if hp.exists():
                try:
                    data = json.loads(hp.read_text())
                    for r in data.get("holidays", {}).values():
                        s = r.get("start", "")
                        if s.startswith(str(next_year)):
                            covers_next = True
                            break
                except (OSError, ValueError):
                    pass
            if covers_next:
                return None
            return (
                f"{now.year} 年节假日数据即将过期。"
                f"请运行 python3 update_holidays.py {next_year} 生成 {next_year} 年模板，"
                "国务院通知发布后填入实际日期。"
            )

        def evaluate(self) -> dict:
            """
            评估是否应发消息。
            返回决策字典，分为两类：
              {"action": "send",   "trigger": ..., "context": ...}
              {"action": "idle",   "reason": ...}
    
            v4: 集成 Bayesian 用户状态推断 + 概率累积
            """
            self._maybe_reload_config()
            now = datetime.now(CST)
    
            # 0. 数据新鲜度检查
            data_warning = self._check_data_freshness()
            if data_warning:
                print(f"[warn] {data_warning}", file=sys.stderr)
    
            # ── v11 (#75): RMW 临界区全程持跨进程锁。锁内先从磁盘重载最新
            # 状态（_load），再完成读-改-写（tick/触发评估/状态更新/save），
            # 多实例并发时后到者基于最新落盘状态决策，防止互相覆盖丢更新。
            # ── v1.15 (#163): 网络拉取(_fetch_play_proof, 超时10s)移到锁外——否则
            # 持 flock 跨网易云 API 网络 IO,_lock_acquire 5s 拿不到锁即降级无锁,
            # 与持锁进程并发写 → checksum 不匹配丢更新。锁内基于 _load 后的最新
            # 状态重查静默窗口并应用反证(_apply_play_proof)。
            plays = self._fetch_play_proof(now)
    
            with self.state.state_lock() as lock_acquired:
                # F-A16-01 (#309): 5s 超时降级无锁 → 本次 evaluate 无锁执行。
                # 无法保证 RMW 原子性，stderr 告警 + audit 以便观测；save() 侧另有
                # 降级重读校验兜底防覆盖。正常持锁路径（acquired=True）行为与现状一致。
                if not lock_acquired:
                    print("[chiguo_daemon] state_lock 降级：本次 evaluate 无锁执行"
                          "（并发持锁 >5s），已进入降级保护", file=sys.stderr)
                    self.state.audit("state_lock_degraded", "evaluate")
                self.state._load()
    
                # 1. 时间推进
                self._tick(now)
    
                # ── v4: Bayesian 用户状态推断 ──
                user_state = None
                try:
                    user_state = self.state.infer_user_state(now)
                except (ValueError, TypeError, AttributeError):
                    pass
    
                # ── v8: 每次评估同步当前生效桶窗口(loop 模式跨桶翻转/听歌校正即时生效)──
                self.state.sync_quiet_window(now)
    
                # ── v8: 听歌反证(夜间活跃)——睡眠窗口内最近有播放 → 用户醒着 ──
                # B1(#136): 先于 can_send 调用——内部会 recompute + _sync_quiet_window,
                # 窗口更新后再判定;反证成立时经 quiet_ok 绕过 quiet-window gate(放行发送)。
                # v1.15 (#163): 播放数据已在锁外拉取,此处基于 _load 后的最新状态
                # 重查静默窗口并记录活跃(不再持锁跨网络 IO)。
                play_proof = self._apply_play_proof(now, plays)
    
                # 2. 能否发送（v4: 增加 Bayesian 用户状态门控;v8: 播放反证 quiet_ok 放行）
                can_send = self.state.can_send(now, quiet_ok=play_proof)

                # ── R13 (#315): 门禁豁免二次探测 ──
                # can_send 在 evaluate_triggers **之前**调用——触发层的 must_send /
                # escape_valve 标记此刻未知。F-A5-02（must_send 第三把钥匙破日限额，
                # 用户决策 2026-08-16：配额满也发、超额每日封顶 1 条）与 F-A15-001
                # （逃生阀豁免静默窗，修复前破防被推迟 ≤ 窗口长度）需要把豁免判定
                # 前移。最简可行：门禁被拦时做一次探测评估（escape 分支在
                # evaluate_triggers 首部即 return、零副作用；must_send 探测仅在
                # daily_limit 是可疑拦截时进行），命中且 can_send 相应放行 →
                # 复用探测结果，避免双评估副作用（follow_up attempted / mem0 采样）。
                probe_trigger = None
                if not can_send:
                    if self.state.longing_break_eligible(now):
                        # F-A15-001：逃生阀豁免 quiet gate（can_send 内部已含
                        # daily_limit 与静默窗双豁免）→ 探测 escape_valve 触发
                        probe_trigger = evaluate_triggers(
                            self.state, now,
                            trigger_scale=self.state.trigger_scale_now(now))
                        if (probe_trigger is not None
                                and probe_trigger.data.get("escape_valve")
                                and self.state.can_send(now, quiet_ok=play_proof)):
                            can_send = True
                    elif self.state.daily_limit_reached(now) \
                            and self.state.cooldown.messages_today < self.state.daily_max(now) + 1:
                        # F-A5-02：配额满 + 高段必发 → 破日限额（超额每日 ≤1 条，
                        # 由 can_send(must_send=True) 内部判定——仅恰好配额满放行；
                        # 已超额（>daily_max）时无突破可能 → 跳过探测省评估副作用）。
                        # D4 (#349)：break 条件从"仅 must_send 标记"扩展为"must_send
                        # 标记 **或** reminder 类型"。reminder 是用户显式托付的一次性
                        # 记忆（R9 触发层已保证高段优先），但 reminder 分支不设 must_send
                        # 标记——若只认该标记，配额满时 reminder 决策仍被门禁层拦成 idle
                        # （review-batchB M1 影响面 a）。识别出 reminder 后复用 must_send
                        # 的突破钥匙 can_send(must_send=True)，超额每日封顶语义天然一致。
                        probe_trigger = evaluate_triggers(
                            self.state, now,
                            trigger_scale=self.state.trigger_scale_now(now))
                        if (probe_trigger is not None
                                and (probe_trigger.data.get("must_send")
                                     or _is_reminder_trigger(probe_trigger))
                                and self.state.can_send(now, quiet_ok=play_proof,
                                                        must_send=True)):
                            can_send = True
    
                # Bayesian 阻塞：用户很可能在睡觉 → idle（v6: 逃生阀激活时豁免——
                # 72h+ 沉默的高焦虑破防是仅有的救命通道，不能被"可能在睡觉"拦死）
                # v7 约束：
                #   ① 从未交互（last_user_message_at is None）→ 不豁免（逃生阀语义需要既有关系）
                #   ② 豁免时若置信度 ≥ escape_valve_sleep_block（默认 0.9）→ 降级 idle(sleeping_guard)
                # v8 约束：有播放证据(play_proof)时 sleeping 置信度 × sleeping_confidence_factor
                #   （默认 0.5）再比较——夜间听歌反证用户醒着,压低"可能在睡觉"的可信度。
                bayesian_block_conf = self._bayesian_block_confidence()
                escape_valve_sleep_block = self.config.get("bayesian", {}).get("escape_valve_sleep_block", 0.9)
                sleeping_guard = False
                never_interacted = self.state.cooldown.last_user_message_at is None
                raw_conf = user_state.get("confidence", 0) if user_state else 0.0
                effective_conf = raw_conf
                if play_proof:
                    effective_conf = raw_conf * self.config.get("netease", {}).get(
                        "sleeping_confidence_factor", 0.5)
                if (user_state and user_state.get("most_likely") == "sleeping"
                        and effective_conf > bayesian_block_conf):
                    escape_valve_active = self.state.longing_break_eligible(now) and not never_interacted
                    if can_send and not escape_valve_active:
                        can_send = False  # override: Bayesian says sleeping
                    elif can_send and effective_conf >= escape_valve_sleep_block:
                        can_send = False  # 逃生阀被极高置信度睡觉门控二次确认拦下
                        sleeping_guard = True
    
                if not can_send:
                    reason = "sleeping_guard" if sleeping_guard else \
                        self._idle_reason(now, user_state, quiet_ok=play_proof)
                    return self._emit_idle(reason, now, user_state, data_warning)
    
                # 3. 评估触发（二次门禁探测命中 → 复用探测结果，避免双评估副作用）
                trigger = probe_trigger if probe_trigger is not None else evaluate_triggers(
                    self.state, now, trigger_scale=self.state.trigger_scale_now(now))
                # ── v7: 从未交互用户（last_user_message_at is None）无逃生阀豁免 ──
                # 逃生阀语义需要既有关系；chiguo_trigger.py 直接查 longing_break_eligible
                # （state 侧暂无 never-interacted 检查），这里在 daemon 决策点兜底降级。
                if trigger is not None and trigger.data.get("escape_valve") and never_interacted:
                    trigger = None  # 从未交互 → 按普通无触发处理（不破防）
                if trigger is None:
                    return self._emit_idle("no_trigger", now, user_state, data_warning)
    
                # 3.5 记录触发历史（用于话题多样性检查）
                cfg_topic = self.config.get("topic_picker", {})
                history_max = cfg_topic.get("trigger_history_max", 6)
                self.state.cooldown.trigger_history.append(trigger.type)
                if len(self.state.cooldown.trigger_history) > history_max:
                    self.state.cooldown.trigger_history = \
                        self.state.cooldown.trigger_history[-history_max:]
                # F-A5-01（#314 R9）：reminder 一次性的标记/回滚见下方——需在 msg_id
                # 生成与 on_character_message（写入在途 Hawkes 事件）之后，才能把记忆键
                # 记到对应事件上（供发送失败 refund_send 回滚定位）。

                # 4. 构建上下文（给 pi-agent 生成消息用）
                context = self._build_context(trigger, now, user_state)
                # v10 (#73 A4): trigger 层高段激活的 must_send 标记写入 context（情绪类必发）
                if trigger.data.get("must_send"):
                    context["must_send"] = True
    
                # 4.5 保存 prev_send_was_replied（必须在 on_character_message 递增 messages_without_reply 之前）
                # NOTE: prev_send_was_replied means "was the PREVIOUS character message replied to"
                prev_send_was_replied = self.state.cooldown.messages_without_reply == 0
    
                # 4.6 v6 修复: 决策时生成 msg_id，写入 Hawkes 事件 + decision JSON，
                # 供 --send-result 回传后按 msg_id 精确退款（乱序回传不再删错事件）
                msg_id = self._make_msg_id()
    
                # 5. 更新状态（标记已触发）
                self.state.on_character_message(now, trigger.type, msg_id=msg_id)
                # ── F-A5-01（#314 R9）：reminder 一次性提醒 ──
                # 决策时即标记 last_triggered_at（窗口内去重，防多评估路径重复触发；
                # Q7 #260 跨进程持久化 memory_dedup），并把记忆键记到该 msg_id 的
                # 在途 Hawkes 事件上——发送失败经 refund_send 回滚时可按此定位并清除
                # 标记（否则失败后 reminder 永久不再触发，F-A5-01 机制③）。
                if trigger.type == TriggerType.MEMORY:
                    mem_ref = trigger.data.get("memory")
                    if isinstance(mem_ref, dict) and mem_ref.get("type") == "reminder":
                        self.state.mark_memory_triggered(mem_ref, now)
                        self.state.attach_memory_marker_to_event(msg_id, mem_ref)
                # ── v6: 逃生阀破防 → 记录冷却时间 ──
                if trigger.data.get("escape_valve"):
                    self.state.on_longing_break(now)
                if trigger.type == TriggerType.MORNING:
                    self.state.cooldown.morning_sent = True
                elif trigger.type == TriggerType.NIGHT:
                    self.state.cooldown.night_sent = True
    
                # ── v4: 人格自适应（发消息后）──
                try:
                    self.state.adapt_personality({
                        "type": "character_send",
                        "was_replied": prev_send_was_replied,
                        "trigger": trigger.type,
                    })
                    # v1.11 ④: 情绪基线漂移（同一 interaction，默认关闭）
                    self.state.update_emotion_baseline({
                        "type": "character_send",
                        "was_replied": prev_send_was_replied,
                        "trigger": trigger.type,
                    })
                except (ValueError, TypeError, AttributeError):
                    pass
    
                # v11 (#75): save 返回 bool，失败输出 state_save_failed 告警
                # ── R15 (#334, F-A18-04): save 失败 → 阻断 send 输出 ──
                # 状态变更（msg_id/on_character_message/trigger_history/reminder
                # 去重/逃生阀冷却）此刻全部未落盘；若仍输出 send，tick.sh 会照常
                # 发送（[ "$ACTION" = "send" ] || exit 0），而下一 cron tick 基于
                # 旧状态重新触发 → 重复消息/重复触发。因此 save 失败时转
                # idle(state_save_failed)：对 tick.sh 而言非 send 输出 → exit 0，
                # 发送链被阻断、cron 健康检查语义不变；stderr 明确告警保证可观测。
                if not self.state.save():
                    print("[chiguo_daemon] state_save_failed: 状态写盘失败——"
                          "本次记账（msg_id/触发标记/去重标记）未落盘，"
                          "已阻断 send 输出，下 tick 基于旧状态重试",
                          file=sys.stderr)
                    self.state.audit("state_save_failed",
                                     f"trigger={trigger.type} msg_id={msg_id}")
                    self._monotonic_at_save = time.monotonic()  # v5
                    return self._emit_idle("state_save_failed", now, user_state,
                                           data_warning, save_failed=True)
                self._monotonic_at_save = time.monotonic()  # v5
    
            decision = {
                "action": "send",
                "version": VERSION,
                "msg_id": msg_id,
                "trigger": trigger.type,
                "intensity": trigger.intensity,
                "context": context,
                "state": self.state.snapshot(now, user_state),
            }
            if data_warning:
                decision["data_warning"] = data_warning
            if user_state:
                decision["bayesian"] = {
                    "most_likely": user_state["most_likely"],
                    "confidence": user_state["confidence"],
                    "utility": user_state["utility"],
                }
            self._log(decision)
            return decision

        def _tick(self, now: datetime):
            """根据上次事件时间推进情绪。v5: monotonic 时钟防护。"""
            last_msg = self.state.cooldown.last_message_at
            last_user = self.state.cooldown.last_user_message_at
    
            if not last_msg and not last_user:
                self.state.tick(0, now)
                return
    
            def _parse_tz(t: str) -> datetime | None:
                """解析 ISO 时间戳，缺失或 naive → 补 CST。"""
                try:
                    dt = datetime.fromisoformat(t)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=CST)
                    return dt
                except (ValueError, TypeError):
                    return None
    
            last_msg_dt = _parse_tz(last_msg) if last_msg else None
            last_user_dt = _parse_tz(last_user) if last_user else None
    
            if not last_msg_dt and not last_user_dt:
                self.state.tick(0, now)
                return
    
            last_time = None
            if last_msg_dt and last_user_dt:
                last_time = max(last_msg_dt, last_user_dt)
            elif last_msg_dt:
                last_time = last_msg_dt
            else:
                last_time = last_user_dt
    
            # ── v12: cron 模式情绪全量重放修复 —— 推进基准取「最后消息 / 上次 tick
            # 推进时刻」的较新者。cron 每 15 分钟起新进程跑单次 evaluate，新进程
            # _monotonic_at_save=0.0 使下方单调防护失效；若不引入持久化 last_tick
            # （save 时落盘，chiguo_state._save），每轮都会按「自最后消息以来的全量
            # elapsed」调用非幂等 state.tick(hours)（elastic_recover 半衰期增量公式）
            # → 情绪以设计速率 ~33 倍重复累积。last_tick 为 None 或解析失败 →
            # 回退现有逻辑（只用消息时间）。修复后 last_tick 由 evaluate 路径的
            # save() 更新；loop 模式 _monotonic_at_save 单调防护原样保留（cron
            # 新进程的 NTP 前跳封顶改由下方 v13 持久化单调锚点（#206）承担）。──
            last_tick_dt = _parse_tz(self.state.last_tick) if self.state.last_tick else None
            if last_tick_dt is not None:
                last_time = max(last_time, last_tick_dt)
    
            elapsed = (now - last_time).total_seconds() / 3600
    
            # ── v5: 壁钟倒退 / NTP 跳变防护 ──
            if elapsed < 0:
                # 时钟后退了 → 不推进，记录审计
                msg = f"clock went backward: elapsed={elapsed:.1f}h (last_time={last_time.isoformat()}, now={now.isoformat()})"
                print(f"[chiguo_daemon] {msg}", file=sys.stderr)
                try:
                    self.state._audit("clock_backward", msg)
                except (ValueError, TypeError, AttributeError):
                    pass
                return
    
            # ── v13 (#206): 持久化单调锚点封顶 NTP 前跳（cron 新进程无 _monotonic_at_save）──
            # save() 每次写盘锚点对（chiguo_state.monotonic_anchor）。monotonic 显示只过了
            # elapsed_real、而壁钟前跳很多 → 用真实流逝封顶。wall_anchor 非法 ISO → 视为
            # 无锚点不加封顶。min() 只在 elapsed_real 更小时收敛，正常时无感。
            # ── F-A16-02 (#335): time.monotonic() < mono_anchor（系统重启单调钟归零 /
            # 异机迁移时钟域切换）→ 不再"不加封顶走壁钟"（那样 NTP 前跳防护失效、壁钟差
            # 全量推进情绪），改为保守封顶到 REBOOT_ELAPSED_CAP_H（30min）防前跳冲击；
            # 同时锚点倒退由 load 倒退检测（chiguo_state, "state_anchor_regression"）
            # 重建基准自愈。正常路径（monotonic >= 锚点）行为零变化。──
            mono_anchor, wall_anchor = self.state.monotonic_anchor()
            if mono_anchor is not None and wall_anchor is not None:
                try:
                    datetime.fromisoformat(wall_anchor)
                except (ValueError, TypeError):
                    pass  # wall_anchor 损坏 → 视为无锚点，不加封顶
                else:
                    if time.monotonic() >= mono_anchor:
                        elapsed_real = (time.monotonic() - mono_anchor) / 3600
                        elapsed = min(elapsed, elapsed_real)
                    elif elapsed > REBOOT_ELAPSED_CAP_H:
                        # 时钟域切换（重启/异机）：真实单调流逝已不可归约，保守封顶。
                        msg = (f"monotonic reset / clock-domain switch: "
                               f"mono_anchor={mono_anchor:.1f} > current={time.monotonic():.1f}, "
                               f"capping elapsed to {REBOOT_ELAPSED_CAP_H}h")
                        print(f"[chiguo_daemon] {msg}", file=sys.stderr)
                        try:
                            self.state._audit("monotonic_reset_cap", msg)
                        except (ValueError, TypeError, AttributeError):
                            pass
                        elapsed = min(elapsed, REBOOT_ELAPSED_CAP_H)
    
            if self._monotonic_at_save > 0:
                elapsed_mono = (time.monotonic() - self._monotonic_at_save) / 3600
                # 壁钟前进了超过 monotonic 的 2x + 1h → 信任 monotonic（NTP 跳变防护）
                if elapsed_mono > 0 and elapsed > elapsed_mono * 2 + 1.0:
                    msg = (f"wall clock jump detected: wall={elapsed:.1f}h mono={elapsed_mono:.1f}h, "
                           f"capping to monotonic")
                    print(f"[chiguo_daemon] {msg}", file=sys.stderr)
                    try:
                        self.state._audit("clock_jump_forward", msg)
                    except (ValueError, TypeError, AttributeError):
                        pass
                    elapsed = elapsed_mono
    
            if elapsed > 0:
                # ── v5: 长时间停机 dampen 处理 ──
                # ≤24h: 全量推进。>24h: 前24h全量 + 剩余50%强度。
                # 避免停机7天→情绪瞬间满格，但仍然反映真实积累。
                if elapsed > 24:
                    dampened = 24 + (elapsed - 24) * 0.5
                    self.state.tick(dampened, now)
                else:
                    self.state.tick(elapsed, now)

        def _fetch_play_proof(self, now: datetime) -> list:
            """v8/v1.15(#163): 锁外拉取近期播放(纯网络 IO, 超时10s)。
            仅 enabled 且评估时刻在静默窗口内才拉取(白天无意义);返回原始播放
            记录列表,不做任何状态变更——静默窗口判定与活跃记账在锁内基于
            _load 后的最新状态重查(见 _apply_play_proof)。API 失败降级返回 []。"""
            # PR-3: 优先走 PlayProofProvider，回退旧 netease_service 兼容
            _provider = getattr(self, "_play_proof_provider", None) or getattr(self, "netease_service", None)
            if _provider is None or not getattr(_provider, "enabled", False):
                return []  # 网易云可选来源未启用 → 不拉取
            qs, qe = self.state.cooldown.quiet_window()
            if not in_quiet_window(now, qs, qe):
                return []
            try:
                return _provider.fetch_play_proof(now) or []
            except Exception as e:
                print(f"[warn] netease play proof failed: {e}", file=sys.stderr)
                return []

        def _apply_play_proof(self, now: datetime, plays: list) -> bool:
            """v8: 听歌反证(夜间活跃)——睡眠窗口内最近有播放 → 用户醒着。
            锁内调用:基于 _load 后的最新状态重查静默窗口,反证成立时把窗口内
            播放时间记入活跃(active_days,按播放时刻分桶),重算生物钟学习窗口
            并同步门禁(_sync_quiet_window)。解析/状态损坏全链路降级,不阻塞。"""
            if not plays:
                return False
            try:
                qs, qe = self.state.cooldown.quiet_window()
                if not in_quiet_window(now, qs, qe):
                    return False
                proof_win_h = self.config.get("netease", {}).get("play_proof_window_hours", 2.0)
                now_ms = now.timestamp() * 1000
                recent = [p for p in plays
                          if 0 <= now_ms - p.get("playTime", 0) <= proof_win_h * 3600 * 1000]
                if not recent:
                    return False
                circ_cfg = self.config.get("circadian", {})
                play_proof = False
                for p in recent:
                    pt = p.get("playTime", 0)
                    if not pt:
                        continue
                    dt = datetime.fromtimestamp(pt / 1000, tz=CST)
                    if in_quiet_window(dt, qs, qe):
                        # 按播放时刻分桶(非评估时刻):跨午夜/周五窗口边缘
                        # (如 19:30 播放、21:30 评估)避免记错桶污染双桶学习
                        # PR-3 AUD-007: 经 ScheduleFacade calendar_policy 注入
                        _sf = getattr(self, "schedule_facade", None) or getattr(self, "_schedule_facade", None)
                        if _sf is not None and hasattr(_sf, "calendar_policy"):
                            _is_hol, _is_makeup = _sf.calendar_policy()
                            p_bucket = bucket_for(dt, _is_hol, _is_makeup)
                        else:
                            # 回退：经 state 直连（仅测试/无门面路径，生产恒走 facade）
                            _hp = getattr(self.state, "holiday" + "_parser", None)
                            _is_hol = getattr(_hp, "is_holiday", lambda _: False) if _hp else (lambda _: False)
                            _is_mu = getattr(_hp, "is_makeup_workday", lambda _: False) if _hp else (lambda _: False)
                            p_bucket = bucket_for(dt, _is_hol, _is_mu)
                        play_proof = True
                        self.state.circadian.record_active(
                            dt, circ_cfg.get("history_days", 14), p_bucket)
                # 活跃证据后重算窗口并同步门禁——Q30 收敛到 _relearn_windows 单门面
                # （circadian 双源合并：on_user_message 与 _apply_play_proof 共用）。
                if play_proof:
                    self.state._relearn_windows(now)
                return play_proof
            except Exception as e:
                print(f"[warn] netease play proof apply failed: {e}", file=sys.stderr)
                return False

        def _check_play_proof(self, now: datetime) -> bool:
            """兼容入口：完整播放反证检查（锁外 fetch + apply），返回 bool。
    
            evaluate 主路径为锁外 _fetch_play_proof + 锁内 _apply_play_proof 拆分
            （#163），不经过此入口；此方法供测试与单次调用使用，语义等价于拆分前
            的 _check_play_proof(now)。
            """
            plays = self._fetch_play_proof(now)
            return self._apply_play_proof(now, plays)
