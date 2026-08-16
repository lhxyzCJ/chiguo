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
from datetime import datetime, timezone, timedelta

from decision.base import DecisionEngineBase
from chiguo_trigger import evaluate_triggers
from chiguo_version import VERSION
from chiguo_math import in_quiet_window, longing_accumulate
from chiguo_circadian import bucket_for
from trigger_types import TriggerType  # T7·Q3 (#265) 移植：触发类型枚举单一事实源
from decision_schema import with_contract, validate as validate_decision  # Q16 移植：决策 JSON schema 写前校验

CST = timezone(timedelta(hours=8))


class DecisionCoreMixin(DecisionEngineBase):
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
                with open(self.log_path, "a") as f:
                    try:
                        os.chmod(self.log_path, 0o600)  # 决策日志含对话/状态隐私 → 0600
                    except OSError:
                        pass
                    f.write(json.dumps(decision, ensure_ascii=False) + "\n")
            except (TypeError, ValueError) as e:
                # F-A22-001 加固：决策含非 JSON 类型（如 set）时不再裸吞。
                # 计数 + 明确 stderr，并尝试 default=str 兜底写出，避免决策数据丢失。
                self._decision_write_failures = getattr(
                    self, "_decision_write_failures", 0) + 1
                print(
                    f"[error] 决策 JSON 序列化失败（累计 {self._decision_write_failures} 次）"
                    f" {self.log_path} msg_id={decision.get('msg_id')}: {e}",
                    file=sys.stderr)
                try:
                    with open(self.log_path, "a") as f:
                        f.write(json.dumps(decision, ensure_ascii=False,
                                           default=str) + "\n")
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
                except Exception:
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
                except Exception:
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
    
                # 3. 评估触发
                trigger = evaluate_triggers(self.state, now, trigger_scale=self.state.trigger_scale_now(now))
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
                # #79: reminder 一次性提醒去重——发送确认后经 state 公开 API 标记
                # (last_triggered_at)。Q7(#260, T2 移植): 标记并入 state JSON 持久化
                # (memory_dedup)，cron 每 15 分钟新进程 load 时读回 →
                # 窗口内多评估路径不再重复触发。
                if trigger.type == TriggerType.MEMORY:
                    mem_ref = trigger.data.get("memory")
                    if isinstance(mem_ref, dict) and mem_ref.get("type") == "reminder":
                        self.state.mark_memory_triggered(mem_ref, now)
    
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
                except Exception:
                    pass
    
                # v11 (#75): save 返回 bool，失败输出 state_save_failed 告警
                if not self.state.save():
                    print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
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
                except Exception:
                    pass
                return
    
            # ── v13 (#206): 持久化单调锚点封顶 NTP 前跳（cron 新进程无 _monotonic_at_save）──
            # save() 每次写盘锚点对（chiguo_state.monotonic_anchor）。monotonic 显示只过了
            # elapsed_real、而壁钟前跳很多 → 用真实流逝封顶。wall_anchor 非法 ISO → 视为
            # 无锚点不加封顶；time.monotonic() < mono_anchor（系统重启单调钟归零）→ 不加
            # 封顶走壁钟。min() 只在 elapsed_real 更小时收敛，正常时无感。
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
    
            if self._monotonic_at_save > 0:
                elapsed_mono = (time.monotonic() - self._monotonic_at_save) / 3600
                # 壁钟前进了超过 monotonic 的 2x + 1h → 信任 monotonic（NTP 跳变防护）
                if elapsed_mono > 0 and elapsed > elapsed_mono * 2 + 1.0:
                    msg = (f"wall clock jump detected: wall={elapsed:.1f}h mono={elapsed_mono:.1f}h, "
                           f"capping to monotonic")
                    print(f"[chiguo_daemon] {msg}", file=sys.stderr)
                    try:
                        self.state._audit("clock_jump_forward", msg)
                    except Exception:
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

        def _bayesian_block_confidence(self) -> float:
            """Bayesian 阻塞置信度阈值（[bayesian] min_confidence_for_block，默认 0.5）。
            evaluate 睡觉门控与 _idle_reason 共用同一来源；热重载后即时生效。"""
            return self.config.get("bayesian", {}).get("min_confidence_for_block", 0.5)

        def _fetch_play_proof(self, now: datetime) -> list:
            """v8/v1.15(#163): 锁外拉取近期播放(纯网络 IO, 超时10s)。
            仅 enabled 且评估时刻在静默窗口内才拉取(白天无意义);返回原始播放
            记录列表,不做任何状态变更——静默窗口判定与活跃记账在锁内基于
            _load 后的最新状态重查(见 _apply_play_proof)。API 失败降级返回 []。"""
            if not self.netease_service.enabled:
                return []  # 网易云可选来源未启用 → 不拉取
            qs, qe = self.state.cooldown.quiet_window()
            if not in_quiet_window(now, qs, qe):
                return []
            try:
                return self.netease_service.fetch_play_proof(now) or []
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
                        p_bucket = bucket_for(dt, self.state.holiday_parser.is_holiday,
                                              self.state.holiday_parser.is_makeup_workday)
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

        def _emit_idle(self, reason: str, now, user_state, data_warning: bool) -> dict:
            """idle 决策统一出口：概率累积（no_trigger/user_busy）+ 落盘。"""
            # C1: 空闲静默路径确定性记忆巩固（config 门控默认关闭；失败不阻断主链路）
            self._maybe_consolidate(now)
            if reason in ("no_trigger", "user_busy"):
                self.state.cooldown.held_count += 1
                cfg_cooldown = self.config.get("cooldown", {})
                base_lambda = self.config.get("poisson", {}).get("base_lambda", 0.25)
                current_lam = self.state.cooldown.accumulated_lambda or self.state.current_lambda(now)
                new_lam, _ = longing_accumulate(
                    current_lam, base_lambda,
                    growth_factor=cfg_cooldown.get("longing_growth_factor", 0.08),
                    anxiety=self.state.emotion.anxiety,
                    anxiety_block_threshold=cfg_cooldown.get("anxiety_block_threshold", 70.0),
                    held_count=self.state.cooldown.held_count,
                    max_lambda_multiplier=cfg_cooldown.get("max_lambda_multiplier", 5.0),
                )
                self.state.cooldown.accumulated_lambda = new_lam
    
            # v11 (#75): save 返回 bool，失败输出 state_save_failed 告警
            if not self.state.save():
                print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
            self._monotonic_at_save = time.monotonic()
    
            decision = {
                "action": "idle",
                "version": VERSION,
                "reason": reason,
                "state": self.state.snapshot(now, user_state),
            }
            nxt = self._estimate_next_check(now, reason)
            if nxt:
                decision["next_evaluation_at"] = nxt
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

        def _idle_reason(self, now: datetime, user_state: dict = None,
                         quiet_ok: bool = False) -> str:
            # ── v6 修复: 先报具体门禁（确定性约束），再报 Bayesian 状态（概率推断）。
            # 设计依据：① 门禁是"无论用户状态如何都会拦截"的确定性约束——当多个约束
            # 同时生效时，门禁才是真正的 binding constraint，且其 next_evaluation_at
            # 精确（min_interval → +32min / quiet_hours → quiet_end / daily_limit → 明早）；
            # Bayesian 只能给 1-2h 猜测。② evaluate() 按 reason 决定是否累积 longing
            # （user_busy 会累积）——若被 min_interval 挡住却误报 user_busy，会在冷却期
            # 错误累积。③ 纯 Bayesian 阻塞场景不受影响：evaluate() 的 sleeping 门控只在
            # 具体门禁放行（can_send=True）时才覆盖 can_send，此时走完门禁必然落到
            # user_sleeping/user_busy。
            # v1.15 (#162): 门禁顺序对齐 can_send（daily_limit→min_interval→energy 含
            # rate_energy_override→quiet_hours→busy_suppressed）。busy_suppressed 从
            # 最优先移到最后——与 can_send 判定一致，否则 can_send=False(busy) 时
            # _idle_reason 却因顺序不同返回其它 reason，next_evaluation_at 估算失真。
    
            # daily limit: mirror can_send() logic — active vs silent
            silent_h = self.state.cooldown.silent_hours(now)
            daily_max = self.config.get("cooldown", {}).get(
                "max_daily_active", 4) if silent_h < 8 else self.config.get("cooldown", {}).get("max_daily_silent", 2)
            if self.state.cooldown.messages_today >= daily_max:
                # L4 (#234, D4): 逃生阀放行 → 继续走后续门禁，不直接 return daily_limit。
                # 与 can_send:2169-2172 的 longing 溢出逃生阀语义对齐——can_send 内部已含
                # 逃生阀（is_longing_overflow + 冷却期）判定，若 it 放行则 daily_limit 不应
                # 成为 binding constraint（否则 next_evaluation_at 估到明早、逃生消息被压）。
                try:
                    if not self.state.can_send(now, quiet_ok=quiet_ok):
                        return "daily_limit"
                except Exception:  # noqa: BLE001 - 逃生阀判定异常时保守按 daily_limit 拦截
                    return "daily_limit"
                # can_send 逃生阀放行 → 不 return daily_limit，落到后续门禁（min_interval 等）
    
            # 最小间隔
            min_interval = self.config.get("cooldown", {}).get("min_interval_minutes", 30)
            mins_since = self.state.cooldown.minutes_since_last_message(now)
            # None = 时间戳解析失败（数据损坏）→ 与"从未发过"一致放行，不误判为过频
            if mins_since is not None and mins_since < min_interval:
                return "min_interval"
    
            # 元气检查（孤独暴涨时可覆盖，与 can_send 一致）
            if self.state.emotion.energy < 12:
                emo_cfg = self.config.get("emotion", {})
                override_ok = (
                    emo_cfg.get("rate_energy_override", False)
                    and self.state.emotion.loneliness_rate > emo_cfg.get("rate_energy_threshold", 5.0)
                    and self.state.emotion.energy >= emo_cfg.get("rate_energy_min", 5)
                )
                if not override_ok:
                    return "low_energy"
    
            # 检查是否在静默时段（quiet_ok=播放反证成立时跳过，与 can_send 的
            # `if not quiet_ok:` 判定对齐——否则播放反证成立但被其它 gate 拦下时
            # reason 误报 quiet_hours，next_evaluation_at 失真）
            if not quiet_ok:
                qs, qe = self.state.cooldown.quiet_window()
                if in_quiet_window(now, qs, qe):
                    return "quiet_hours"
    
            # ── v7: 忙碌抑制期（用户说"别烦我"）→ 独立 reason，抑制期不累积 longing ──
            if self.state.cooldown.is_busy_suppressed(now):
                return "busy_suppressed"
    
            # 门禁全部放行后 → 报告 Bayesian 状态（v4: 概率推断的用户状态）
            if user_state:
                ml = user_state.get("most_likely", "")
                conf = user_state.get("confidence", 0)
                block_conf = self._bayesian_block_confidence()
                if ml == "sleeping" and conf > block_conf:
                    return "user_sleeping"
                if ml == "busy" and conf > block_conf:
                    return "user_busy"
            return "no_trigger"

        def _estimate_next_check(self, now: datetime, idle_reason: str) -> str | None:
            """估算下次评估的最优时间。idle 决策时提供动态调度提示。
            调度方（chiguo-tick.sh）可据此替代固定 cron 间隔。"""
            cfg_emo = self.config.get("emotion", {})
            cfg_cooldown = self.config.get("cooldown", {})
    
            if idle_reason == "min_interval":
                min_int = cfg_cooldown.get("min_interval_minutes", 30)
                if self.state.cooldown.last_message_at:
                    try:
                        last = datetime.fromisoformat(self.state.cooldown.last_message_at)
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=CST)  # 与 _parse_tz 一致：naive → 补 CST
                        nxt = last + timedelta(minutes=min_int + 2)
                        if nxt > now:
                            return nxt.isoformat()
                    except (ValueError, TypeError):
                        pass
    
            elif idle_reason == "low_energy":
                e = self.state.emotion.energy
                hl = cfg_emo.get("energy_regen_half_life", 8.0)
                if e < 12:
                    try:
                        ratio = (100.0 - e) / 88.0
                        h = hl * math.log2(max(ratio, 1.001))
                        nxt = now + timedelta(hours=min(h, 4.0))
                        return nxt.isoformat()
                    except (ValueError, ZeroDivisionError):
                        pass
    
            elif idle_reason == "quiet_hours":
                qs, qe = self.state.cooldown.quiet_window()
                if qe < qs and now.hour >= qs:
                    tomorrow = now.date() + timedelta(days=1)
                    nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, qe,
                                   2, tzinfo=CST)
                else:
                    nxt = datetime(now.year, now.month, now.day, qe, 2, tzinfo=CST)
                if nxt > now:
                    return nxt.isoformat()
    
            elif idle_reason == "daily_limit":
                tomorrow = now.date() + timedelta(days=1)
                nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 5, tzinfo=CST)
                return nxt.isoformat()
    
            elif idle_reason == "no_trigger":
                lam = self.state.current_lambda(now)
                if lam > 0:
                    h = min(math.log(2) / lam, 2.0)
                    h = max(h, 5.0 / 60.0)
                    return (now + timedelta(hours=h)).isoformat()
    
            elif idle_reason == "busy_suppressed":
                if self.state.cooldown.busy_suppress_until:
                    try:
                        until = datetime.fromisoformat(self.state.cooldown.busy_suppress_until)
                        if until > now:
                            return until.isoformat()
                    except (ValueError, TypeError):
                        pass
    
            elif idle_reason in ("user_sleeping", "user_busy"):
                # ── v6 修复: Bayesian 状态无精确恢复时间 → 估算 1-2h 后重评
                # （与 _dynamic_sleep_interval 的 user_sleeping/user_busy 分支一致）──
                return (now + timedelta(seconds=3600 + random.uniform(0, 3600))).isoformat()
    
            elif idle_reason == "sleeping_guard":
                # v7: 逃生阀被睡觉高置信拦截 → 延后到下一次常规评估（30min cron 兜底）
                return None
    
            return None

