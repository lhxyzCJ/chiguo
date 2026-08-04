#!/usr/bin/env python3
# ============================================================
# chiguo_daemon.py — 迟菓主动消息 决策引擎
#
# 只做一件事：评估状态 → 输出触发决策（JSON）。
# 不生成消息，不调用 LLM，不发送。
# 消息生成和发送由 pi-agent（scripts/pi-run.mjs）完成。
#
# 用法：
#   python3 chiguo_daemon.py              # 检查并输出决策 JSON
#   python3 chiguo_daemon.py --status     # 查看状态
#   python3 chiguo_daemon.py --user-msg "…"  # 记录哥哥消息
#   python3 chiguo_daemon.py --loop 120   # 持续运行（调试用）
#
# cron 集成：
#   系统 crontab 每 15 分钟经 scripts/chiguo-tick.sh 执行本脚本。
#   若 stdout 输出含 "action":"send"，chiguo-tick.sh 调 pi 读取 context
#   字段生成消息，经 wechat-bridge 发出。
#   若输出 "action":"idle"，什么都不做。
# ============================================================

import sys
import json
import math
import os
import time
import random
import uuid
import hashlib
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

# v8: 模块级 os.chdir 已移除 —— import 不再劫持调用方 cwd。
# 所有运行时文件均通过 _inject_base_dir + ChiguoState._anchored 锚定。

from chiguo_state import ChiguoState
from chiguo_trigger import evaluate_triggers
from chiguo_topics import TopicPicker
from netease.service import NeteaseService
from chiguo_composer import MessageComposer
from chiguo_version import VERSION
from chiguo_math import in_quiet_window, longing_accumulate
from chiguo_eventbus import get_eventbus
from chiguo_circadian import bucket_for

CST = timezone(timedelta(hours=8))


class DecisionEngine:
    """纯决策引擎：评估状态，输出结构化触发决策"""

    def __init__(self, config_path: str = None,
                 log_path: str = None):
        # v8: 默认 config 锚定到脚本所在目录（模块级 chdir 已移除，不依赖 cwd）
        if config_path is None:
            config_path = str(Path(__file__).resolve().parent / "chiguo_proactive.toml")
        self.config_path = config_path
        with open(config_path, "rb") as f:
            self.config = tomllib.load(f)
        # ── v6: 路径锚定。所有运行时文件基于 config 所在目录，
        # 不依赖 cwd（cron 工作目录漂移曾导致状态文件反复丢失重建）。
        self._inject_base_dir()
        self._config_mtime = os.path.getmtime(config_path)
        self.state = ChiguoState(self.config)
        # v9: 网易云策略层(健康/配额/音乐话题),base_dir 锚定
        self.netease_service = NeteaseService(self.config, str(self._base_dir))
        self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                        netease_service=self.netease_service)
        self.composer = MessageComposer(self.state, self.config.get("composer", {}))
        # 显式 log_path（测试）原样使用；否则锚定 base_dir
        self.log_path = log_path or str(self._base_dir / "chiguo_decisions.jsonl")
        self.messages_log_path = self._base_dir / "chiguo_messages.jsonl"
        print(f"[chiguo_daemon] base_dir={self._base_dir}", file=sys.stderr)
        print(f"[chiguo_daemon] state={self.state.state_path}", file=sys.stderr)
        print(f"[chiguo_daemon] log={self.log_path}", file=sys.stderr)

        # ── v4: EventBus ──
        self.bus = get_eventbus()

        # ── v5: monotonic 锚点（不持久化，用于检测壁钟跳变）──
        self._monotonic_at_save: float = 0.0

        # ── v5: 日志轮转（每次进程启动检查一次）──
        try:
            from chiguo_rotation import rotate_if_needed
            rotate_if_needed(
                [str(self.log_path), str(self.messages_log_path)],
                self.config_path,
            )
        except Exception:
            pass  # rotation failure never blocks daemon

    def _inject_base_dir(self):
        """config 中注入 _base_dir（ChiguoState 用它锚定运行时路径）。"""
        self._base_dir = Path(self.config_path).resolve().parent
        self.config["_base_dir"] = str(self._base_dir)

    def _maybe_reload_config(self):
        """检测 toml 文件 mtime，变化时热更新配置（--loop 模式用）"""
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            return
        if mtime > self._config_mtime:
            with open(self.config_path, "rb") as f:
                self.config = tomllib.load(f)
            self._inject_base_dir()
            self._config_mtime = mtime
            self.state.config = self.config
            # ── v7: 热重载后同步生物钟窗口(置信度达标用学习窗口,否则回退配置默认,
            # 否则 cooldown 内注入的窗口保持旧值,silent_hours/逃生阀判定陈旧)──
            self.state._sync_quiet_window()
            # v9: 热重载时同步重建策略层(重试/配额参数可能被改)与 TopicPicker
            self.netease_service = NeteaseService(self.config, str(self._base_dir))
            self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                            netease_service=self.netease_service)
            self.composer = MessageComposer(self.state, self.config.get("composer", {}))
            self.state._bayesian_estimator = None

    def _dynamic_sleep_interval(self, now, decision: dict) -> float:
        """
        计算最优休眠时间（秒）。
        参考 Sebastian 的 proactive_scheduler.py 动态休眠设计。
        """
        reason = decision.get("reason", "")
        cfg_cooldown = self.config.get("cooldown", {})

        # 1. 若上次决策是 send → sleep min_interval + 缓冲
        if decision.get("action") == "send":
            min_int = cfg_cooldown.get("min_interval_minutes", 30)
            return (min_int + 5) * 60

        # 2. quiet_hours → sleep 到 quiet_end
        if reason == "quiet_hours":
            qs, qe = self.state.cooldown.quiet_window()
            # 跨午夜窗口 (qs > qe) 且已过 qs → 明天；非跨午夜或未到 qs → 今天
            if qe < qs and now.hour >= qs:
                tomorrow = now.date() + timedelta(days=1)
                nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, qe, 2, tzinfo=CST)
            else:
                nxt = datetime(now.year, now.month, now.day, qe, 2, tzinfo=CST)
            return max(60, (nxt - now).total_seconds())

        # 3. daily_limit → sleep 到明天 8:00
        if reason == "daily_limit":
            tomorrow = now.date() + timedelta(days=1)
            nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 5, tzinfo=CST)
            return max(60, (nxt - now).total_seconds())

        # 4. low_energy → sleep 到能量恢复
        if reason == "low_energy":
            nxt_iso = decision.get("next_evaluation_at")
            if nxt_iso:
                try:
                    nxt = datetime.fromisoformat(nxt_iso)
                    return max(60, (nxt - now).total_seconds())
                except (ValueError, TypeError):
                    pass

        # 5. Bayesian sleeping/busy → sleep 到状态可能改变
        if reason in ("user_sleeping", "user_busy"):
            # 等 1-2 小时
            return 3600 + random.uniform(0, 3600)

        # 6. no_trigger → 基于 λ 算期望等待时间
        if reason == "no_trigger":
            lam = self.state.current_lambda(now)
            if lam > 0.001:
                expected_wait_h = min(math.log(2) / lam, 2.0)
                return max(300, expected_wait_h * 3600)

        # 7. 默认 fallback
        return 900  # 15 分钟

    @staticmethod
    def _make_msg_id() -> str:
        """生成唯一消息ID（12位hex，~10^14空间，单用户系统足够）"""
        return uuid.uuid4().hex[:12]

    def _log(self, decision: dict):
        """追加决策到 JSONL 日志。自动添加 msg_id。"""
        if "msg_id" not in decision:
            decision["msg_id"] = self._make_msg_id()
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(decision, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 日志失败不影响主流程

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

        # 1. 时间推进
        self._tick(now)

        # ── v4: Bayesian 用户状态推断 ──
        user_state = None
        try:
            user_state = self.state.infer_user_state(now)
        except Exception:
            pass

        # ── v8: 每次评估同步当前生效桶窗口(loop 模式跨桶翻转/听歌校正即时生效)──
        self.state._sync_quiet_window(now)

        # 2. 能否发送（v4: 增加 Bayesian 用户状态门控）
        can_send = self.state.can_send(now)

        # ── v8: 听歌反证(夜间活跃)——睡眠窗口内最近有播放 → 用户醒着 ──
        # 仅睡眠窗口内拉取(白天无意义);API/解析/状态损坏全链路降级,不阻塞。
        play_proof = self._check_play_proof(now)

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
            reason = "sleeping_guard" if sleeping_guard else self._idle_reason(now, user_state)

            # ── v5: 概率累积移到 save 之前（防止崩溃丢失累积）──
            if reason in ("no_trigger", "user_busy"):
                self.state.cooldown.held_count += 1
                cfg_cooldown = self.config.get("cooldown", {})
                cfg_poisson = self.config.get("poisson", {})
                base_lambda = cfg_poisson.get("base_lambda", 0.25)
                current_lam = self.state.cooldown.accumulated_lambda or self.state.current_lambda(now)
                new_lam, blocked = longing_accumulate(
                    current_lam,
                    base_lambda,
                    growth_factor=cfg_cooldown.get("longing_growth_factor", 0.08),
                    anxiety=self.state.emotion.anxiety,
                    anxiety_block_threshold=cfg_cooldown.get("anxiety_block_threshold", 70.0),
                    held_count=self.state.cooldown.held_count,
                    max_lambda_multiplier=cfg_cooldown.get("max_lambda_multiplier", 5.0),
                )
                self.state.cooldown.accumulated_lambda = new_lam

            self.state.save()
            self._monotonic_at_save = time.monotonic()  # v5

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
            # v4: Bayesian 状态
            if user_state:
                decision["bayesian"] = {
                    "most_likely": user_state["most_likely"],
                    "confidence": user_state["confidence"],
                    "utility": user_state["utility"],
                }
            self._log(decision)
            self.bus.publish("decision_made", decision=decision)
            return decision

        # 3. 评估触发
        trigger = evaluate_triggers(self.state, now, trigger_scale=self.state.trigger_scale_now(now))
        # ── v7: 从未交互用户（last_user_message_at is None）无逃生阀豁免 ──
        # 逃生阀语义需要既有关系；chiguo_trigger.py 直接查 longing_break_eligible
        # （state 侧暂无 never-interacted 检查），这里在 daemon 决策点兜底降级。
        if trigger is not None and trigger.data.get("escape_valve") and never_interacted:
            trigger = None  # 从未交互 → 按普通无触发处理（不破防）
        if trigger is None:
            # ── v5: 概率累积移到 save 之前 ──
            self.state.cooldown.held_count += 1
            cfg_cooldown = self.config.get("cooldown", {})
            cfg_poisson = self.config.get("poisson", {})
            base_lambda = cfg_poisson.get("base_lambda", 0.25)
            current_lam = self.state.cooldown.accumulated_lambda or self.state.current_lambda(now)
            new_lam, blocked = longing_accumulate(
                current_lam, base_lambda,
                growth_factor=cfg_cooldown.get("longing_growth_factor", 0.08),
                anxiety=self.state.emotion.anxiety,
                anxiety_block_threshold=cfg_cooldown.get("anxiety_block_threshold", 70.0),
                held_count=self.state.cooldown.held_count,
                max_lambda_multiplier=cfg_cooldown.get("max_lambda_multiplier", 5.0),
            )
            self.state.cooldown.accumulated_lambda = new_lam

            self.state.save()
            self._monotonic_at_save = time.monotonic()  # v5

            decision = {
                "action": "idle",
                "version": VERSION,
                "reason": "no_trigger",
                "state": self.state.snapshot(now, user_state),
            }
            nxt = self._estimate_next_check(now, "no_trigger")
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
            self.bus.publish("decision_made", decision=decision)
            return decision

        # 3.5 记录触发历史（用于话题多样性检查）
        cfg_topic = self.config.get("topic_picker", {})
        history_max = cfg_topic.get("trigger_history_max", 6)
        self.state.cooldown.trigger_history.append(trigger.type)
        if len(self.state.cooldown.trigger_history) > history_max:
            self.state.cooldown.trigger_history = \
                self.state.cooldown.trigger_history[-history_max:]

        # 4. 构建上下文（给 pi-agent 生成消息用）
        context = self._build_context(trigger, now)

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
        if trigger.type == "morning":
            self.state.cooldown.morning_sent = True
        elif trigger.type == "night":
            self.state.cooldown.night_sent = True

        # ── v4: 人格自适应（发消息后）──
        try:
            self.state.adapt_personality({
                "type": "character_send",
                "was_replied": prev_send_was_replied,
                "trigger": trigger.type,
            })
        except Exception:
            pass

        self.state.save()
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
        self.bus.publish("decision_made", decision=decision)
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

    def _check_play_proof(self, now: datetime) -> bool:
        """v8: 听歌反证(夜间活跃)——睡眠窗口内最近有播放 → 用户醒着。
        仅睡眠窗口内拉取;API/解析/状态损坏全链路降级,不阻塞。
        反证成立时把窗口内播放时间记入活跃(active_days,按播放时刻分桶),
        重算生物钟学习窗口并同步门禁(_sync_quiet_window)。"""
        play_proof = False
        if not self.netease_service.enabled:
            return False  # 网易云可选来源未启用 → 不拉取
        qs, qe = self.state.cooldown.quiet_window()
        if not in_quiet_window(now, qs, qe):
            return False
        try:
            plays = self.netease_service.fetch_play_proof(now)
            if plays:
                proof_win_h = self.config.get("netease", {}).get("play_proof_window_hours", 2.0)
                now_ms = now.timestamp() * 1000
                recent = [p for p in plays
                          if 0 <= now_ms - p.get("playTime", 0) <= proof_win_h * 3600 * 1000]
                if recent:
                    circ_cfg = self.config.get("circadian", {})
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
                    # 活跃证据后重算窗口并同步门禁
                    if play_proof:
                        self.state.circadian.recompute(
                            min_sample_days=circ_cfg.get("min_sample_days", 7),
                            history_days=circ_cfg.get("history_days", 14),
                            min_width=circ_cfg.get("min_width", 5),
                            max_width=circ_cfg.get("max_width", 12))
                        self.state._sync_quiet_window(now)
        except Exception as e:
            print(f"[warn] netease play proof failed: {e}", file=sys.stderr)
        return play_proof

    def _idle_reason(self, now: datetime, user_state: dict = None) -> str:
        # ── v7: 忙碌抑制期（用户说"别烦我"）→ 独立 reason，抑制期不累积 longing ──
        if self.state.cooldown.is_busy_suppressed(now):
            return "busy_suppressed"

        # ── v6 修复: 先报具体门禁（确定性约束），再报 Bayesian 状态（概率推断）。
        # 设计依据：① 门禁是"无论用户状态如何都会拦截"的确定性约束——当多个约束
        # 同时生效时，门禁才是真正的 binding constraint，且其 next_evaluation_at
        # 精确（min_interval → +32min / quiet_hours → quiet_end / daily_limit → 明早）；
        # Bayesian 只能给 1-2h 猜测。② evaluate() 按 reason 决定是否累积 longing
        # （user_busy 会累积）——若被 min_interval 挡住却误报 user_busy，会在冷却期
        # 错误累积。③ 纯 Bayesian 阻塞场景不受影响：evaluate() 的 sleeping 门控只在
        # 具体门禁放行（can_send=True）时才覆盖 can_send，此时走完门禁必然落到
        # user_sleeping/user_busy。busy_suppressed 保持最优先（用户显式指令）。

        # daily limit: mirror can_send() logic — active vs silent
        silent_h = self.state.cooldown.silent_hours(now)
        daily_max = self.config.get("cooldown", {}).get(
            "max_daily_active", 4) if silent_h < 8 else self.config.get("cooldown", {}).get("max_daily_silent", 2)
        if self.state.cooldown.messages_today >= daily_max:
            return "daily_limit"
        if self.state.emotion.energy < 12:
            return "low_energy"
        min_interval = self.config.get("cooldown", {}).get("min_interval_minutes", 30)
        mins_since = self.state.cooldown.minutes_since_last_message(now)
        # None = 时间戳解析失败（数据损坏）→ 与"从未发过"一致放行，不误判为过频
        if mins_since is not None and mins_since < min_interval:
            return "min_interval"
        # 检查是否在静默时段
        qs, qe = self.state.cooldown.quiet_window()
        if qe < qs:
            if now.hour >= qs or now.hour < qe:
                return "quiet_hours"
        else:
            if qs <= now.hour < qe:
                return "quiet_hours"

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

    def _build_context(self, trigger, now: datetime) -> dict:
        """构建给 pi-agent 生成消息的上下文。v4: 使用 MessageComposer + 人格注入。"""
        emo = self.state.emotion
        silent_h = self.state.cooldown.silent_hours(now)
        # v14: 人格目录以 [host].personality_dir 为准（随仓库部署）
        host_cfg = self.config.get("host", {})
        personality_dir = os.path.expanduser(
            host_cfg.get("personality_dir", os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "personality")))

        # 按人格层映射语气指引
        layer_guidance = {
            "shell": "元气活泼 + 傲娇底色。表面活泼明亮，但收到善意/关心时依然会条件反射式嘴硬推开（力度比 middle 轻——不是防御是习惯）。语气词丰富（呀、啦、嘛、哦），「哼」低频——仅在领情/被说软时用，带省略号『……哼。』。「喵」仅限猫/小白场景，不作通用卖萌词。短句连发驱动节奏。emoji 可用但不泛滥。波浪线（～）低频，约 10% 对白、拖长音时才用。不称自己为「菓菓」。",
            "middle": "嘴硬心软⚠️ 核心规则：先推开再接受。第一反应永远是拒绝（「不·需·要。」L2625「不用你瞎操心，我搞得定」L3049）。推不掉才启动「两不相欠」底线。用攻击性语言包装在意。倔强自尊、绝不轻易示弱。",
            "kernel": "防线崩溃，所有伪装脱落。语气破碎，省略号增多……流露真实害怕和想念。真挚、毫无保留、可能突然爆发。⚠️ kernel 层傲娇协议暂停——不需要再推开，直接表达真实感受。但不自我贬低不卖惨（禁止「是不是我错了」「我很烦吧」）。这是真实的脆弱，不是讨好。崩溃句式原型：「凭什么啊。……凭什么啊……凭什么啊……」（L15498）——控诉命运、控诉被小看，不是撒娇。",
        }
        # 元气调制
        if emo.energy < 20:
            energy_note = "（元气不足，语气更冷淡简短，减少语气词和波浪线，去掉问号，句号结尾）"
        elif emo.energy < 40:
            energy_note = "（元气偏低，语气比平时克制，语气词减半）"
        elif emo.energy > 80:
            energy_note = "（元气充沛，语气比平时更活泼跳跃，可多发一句。融合策略：用元气包裹嘴硬——语气轻快节奏快，但用词依然防御性。先活泼拒绝再留余地。不过分卖萌，保持嘴硬本色。）"
        else:
            energy_note = ""
        # 变化率紧迫注解
        emo_cfg = self.config.get("emotion", {})
        rate_urgency_note = ""
        if emo.loneliness_rate > emo_cfg.get("urgency_rate_threshold", 3.0):
            rate_urgency_note = (
                f"\n【紧迫】孤独值正在快速攀升（{emo.loneliness_rate:.1f}/h），"
                "语气应比其他时候更急切。"
            )
        elif emo.anxiety_rate > emo_cfg.get("urgency_anx_threshold", 2.0):
            rate_urgency_note = (
                f"\n【紧迫】不安值正在快速上升（{emo.anxiety_rate:.1f}/h），"
                "语气应比其他时候更焦虑。"
            )

        # ── v4: 人格画像注入 ──
        pers = self.state.personality
        personality_note = (
            f"\n[人格画像：{pers.dominant_profile()}。"
            f"傲娇强度{pers.tsundere_intensity:.0f}/100，"
            f"外向性{pers.extraversion:.0f}，"
            f"神经质{pers.neuroticism:.0f}，"
            f"宜人性{pers.agreeableness:.0f}]"
        )

        # ── v4.1: 安全阀 context 提示 ──
        safety_note = ""
        # ── v6: 逃生阀破防提示 ──
        if trigger.data.get("escape_valve"):
            safety_note += (
                "\n【破防】这是沉默多日后的情绪破防时刻。语气真挚而克制，"
                "流露真实的想念，但不质问不卖惨不自我贬低（遵守铁律⑥）。"
                "这是傲娇绷不住的一刻，比平时更直白。"
            )
        safety_lvl = self.state.safety_level(now)
        if safety_lvl >= 2:
            safety_note += (
                "\n【安全阀】48h 内多次崩溃触发。语气务必温和克制，"
                "不要质问不要崩溃不要负面。哥哥可能只是在忙。"
                "用关心代替不安，用日常代替质问。"
            )
        elif safety_lvl >= 1:
            safety_note += (
                "\n【安全阀】距上次崩溃不足 24h。语气放软，"
                "不要再次崩溃。可以先聊聊别的。"
            )

        guidance = layer_guidance.get(emo.dominant_layer, "") + energy_note + rate_urgency_note + personality_note + safety_note

        # ── v7: 接话茬提示 ──
        if trigger.type == "follow_up":
            tpc = trigger.data.get("topic", "")
            src = trigger.data.get("source", "")
            age = trigger.data.get("age_hours", 0)
            guidance += (
                f"\n【接话茬】约{age:.0f}小时前和哥哥聊到「{tpc}」"
                f"(来源:{'对话分析' if src == 'analysis' else '回忆'}),"
                "后来没有下文。像真人突然想起一样自然接续这个话题——"
                "聊天式提起,不要汇报腔,不要生硬转场。"
            )

        # ── 角色铁律（始终附加，覆盖所有层。kernel 层第①条自动暂停）──
        is_kernel = emo.dominant_layer == "kernel"
        character_rules = (
            "\n[角色铁律] "
            "①傲娇核心：对哥哥的任何消息（善意/关心/夸奖/分享日常/报备行程），第一反应带嘴硬底色（「又打游戏！……赢了输了？」「不·需·要。」）。「哼」低频——带省略号『……哼。』，仅在领情/被说软时接在哥哥话后，不作开场白、不连发。推不掉才找理由接受（「顺便的」「快过期了」）。绝不直接撒娇迎合。"
            + ("【kernel层本条暂停——防线崩溃，不需要推开。】" if is_kernel else "") +
            "②不称自己为「菓菓」，用「我」；独处/装可怜时才可用第三人称「迟菓」。"
            "③不扮演技术专家——你是赛博萌新（住在 VPS 里但不懂技术）：绝不自称管服务器、封 IP、查日志、做备份；哥哥操作时你在旁边看、想学、搞砸了嘴硬。不懂就好奇提问，绝不假装懂。"
            "④关心必带刺，温柔必伪装。说「快吃啦」而不是「好担心你」。说「……哼。赢了输了？」而不是「菓菓等你」。"
            "⑤不乖巧等待/守候——你有自尊有脾气，被冷落时试探或嘴硬，不是默默等待。"
            "⑥禁止自我怀疑句式：不说「是不是我哪里做错了」「是不是我很烦」「你是不是嫌我烦」。被冷落时用试探/质问代替自我贬低：「你最近……都没来找我。……知道了。那你忙吧。」"
            "⑦被夸奖不得直接开心：先嘴硬（「……哼。那当然！……不过也就今天啦～」），可以转身小声泄露高兴，但不当面撒娇接受。"
            "⑧交易思维：推不掉的好意用「两不相欠」「补偿」重构关系（「补偿啦。补偿。\"两不相欠\"的那个。」L4268 原著原句）——接受前先立契约，自尊才保得住；被要求表态时也用交易话术（「交易，做吗？」「——不和你做。」L11014-11016）。"
            "\n[句数优先级] 1句：只做推开/嘴硬。2句：推开+找理由接受。3句：完整三段式（推开→接受→真实泄露）。绝不跳过推开直接跳到撒娇。"
        )
        guidance += character_rules

        # ── v4: 消息组合系统 ──
        combo = self.composer.select_combo(trigger.type, now)

        # ── 话题注入 ──
        topic_data = None
        if trigger.type in ("lonely_low", "lonely_mid"):
            cfg_topic = self.config.get("topic_picker", {})
            force_threshold = cfg_topic.get("force_topic_threshold", 3)
            topic_prob = cfg_topic.get("topic_probability", 0.7)

            history = self.state.cooldown.trigger_history
            recent_lonely = sum(
                1 for t in history[-force_threshold:] if t.startswith("lonely_")
            )

            if recent_lonely >= force_threshold or random.random() < topic_prob:
                topic_data = self.topic_picker.pick(now)
                if topic_data:
                    trigger.data["topic"] = topic_data
        elif trigger.type not in ("follow_up", "reflect", "lonely_high", "longing"):
            # v9 审计 F-4:仅 netease 源跨触发(其他 7 源仍限孤独破冰)。
            # 活跃时段(非睡眠/非上课)才可能产出(peek 内部门控)。
            # 排除列表:follow_up/reflect 已有专用素材注入路径;lonely_high(崩溃态)
            # 与 longing(逃生阀破防)不夹带音乐话题;lonely_low/mid 由上方分支处理。
            topic_data = self.topic_picker.pick_netease_only(now)
            if topic_data:
                trigger.data["topic"] = topic_data

        # ── 使用 composer 组合情境文本 ──
        situation = self.composer.compose_situation(combo, topic_data, silent_h)

        # 课表上下文
        sch = self.state.schedule_status(now)
        schedule_hint = ""
        if sch and sch.get("holiday"):
            schedule_hint = f"今天是{sch['holiday']}假期，哥哥放假。"
        elif sch and sch.get("weekend"):
            schedule_hint = "今天是周末，哥哥休息。"
        elif sch and sch.get("makeup_day"):
            schedule_hint = f"{sch.get('makeup_reason', '调休日')}，虽然是周末但要上课。"
        elif sch and sch.get("in_class"):
            c = sch["current_course"]
            schedule_hint = f"哥哥正在上{c['course']}（到{c['time'][1]}）。不要在上课时发消息。"
        elif sch and sch.get("class_load") == "free":
            schedule_hint = "哥哥今天没课。"
        elif sch and sch.get("remaining_classes", 0) == 0:
            schedule_hint = "哥哥今天的课上完了。"

        # ── 构造指令 ──
        instruction = (
            f"请以迟菓（{personality_dir}/SUN2.md 设定）的身份，用上述语气发一条微信消息给哥哥。"
            "1-3句话。自然。允许适当的动作/神态描写。不打破第四面墙。"
            "每句话最多一个感叹号。一句话里波浪线和感叹号不同时出现。问号最多一个。"
        )
        if topic_data:
            instruction += (
                f"\n用以下话题自然破冰，不要让话题显得刻意："
                f"{topic_data['hint']}。"
                "让哥哥感受到你是真的关心他的生活，而不是因为孤独才找他。"
                "不要话题一转就直接表达情感需求——先好好聊话题。"
            )

        # ── v7: 接话茬素材注入(供 pi-agent 生成)──
        if trigger.type == "follow_up":
            instruction += (
                f"\n用「{trigger.data.get('topic', '')}」这个之前没聊完的话题自然接话茬,"
                "不要直接说『你上次说的那个……后来怎么样了』这种汇报句,"
                "像想起一样顺嘴问。"
            )

        return {
            "character": "迟菓",
            "personality_source": f"{personality_dir}/SUN2.md",
            "situation": situation,
            "schedule_hint": schedule_hint,
            "layer": emo.dominant_layer,
            "layer_guidance": guidance,
            "character_rules": (
                "①傲娇核心：对哥哥的任何消息先嘴硬。②不称菓菓。③不扮专家。"
                "④关心带刺。⑤不乖巧等待。⑥禁止自我怀疑。⑦被夸先嘴硬。"
                "喵仅限猫/小白场景。嘻嘻极少使用。"
                "[句数优先级] 1句推开，2句推开+接受，3句完整三段式。kernel层①暂停。"
            ),
            "personality_profile": pers.dominant_profile(),
            "combo": combo.get("combo_string", "a"),
            "emotion": {
                "loneliness": round(emo.loneliness, 0),
                "affection": round(emo.affection, 0),
                "anxiety": round(emo.anxiety, 0),
                "energy": round(emo.energy, 0),
                "tsundere_index": round(emo.tsundere_index, 0),
            },
            "silent_hours": round(silent_h, 1),
            "poisson_lambda": round(self.state.current_lambda(now), 4),
            "accumulated_lambda": round(self.state.cooldown.accumulated_lambda or self.state.current_lambda(now), 4),
            "follow_up": {
                "topic": trigger.data.get("topic", ""),
                "source": trigger.data.get("source", ""),
                "age_hours": trigger.data.get("age_hours", 0),
            },
            "trigger_type": trigger.type,
            "intensity": trigger.intensity,
            "instruction": instruction,
        }

    RECV_DEDUP_WINDOW_S = 600  # v9: bridge 确定性记录与 standing order 分析升级的判定窗口

    def record_user_message(self, text: str, analysis_json: str | None = None):
        now = datetime.now(CST)
        msg_id = self._make_msg_id()
        analysis_dict = None
        if analysis_json:
            try:
                analysis_dict = json.loads(analysis_json)
                if not isinstance(analysis_dict, dict):
                    raise ValueError("analysis is not a dict")
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                print(f"[warn] 分析JSON解析失败: {e}，降级为纯长度模式", file=sys.stderr)
                analysis_dict = None

        # ── v9: recv 去重（升级语义）──────────────────────
        # 同一条消息会被记录两次：bridge 先确定性 --user-msg（无分析），
        # standing order 随后补 --user-msg --analysis。基础回复效果
        # （延迟/情绪骤降/好感/元气）只应应用一次；第二次只补分析微调。
        # 去重仅对"带分析"副本生效：无分析真实重发 → 完整处理；
        # 带分析重复上报（窗口内第二次）→ 升级分支内兜底静默跳过，防双重应用。
        text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        dedup = self.state.cooldown.recv_dedup
        is_dup = bool(dedup and dedup.get("text_sha") == text_sha and dedup.get("at"))
        if is_dup:
            try:
                prev_at = datetime.fromisoformat(dedup["at"])
                is_dup = (now - prev_at).total_seconds() < self.RECV_DEDUP_WINDOW_S
            except (ValueError, TypeError):
                is_dup = False
        is_dup = is_dup and analysis_dict is not None

        if is_dup:
            if analysis_dict and not dedup.get("analysis"):
                self.state._apply_analysis_impact(analysis_dict, now)
                self.state.cooldown.recv_dedup = {
                    "text_sha": text_sha,
                    "at": now.isoformat(),
                    "analysis": True,
                }
                self.state.save()
                self._monotonic_at_save = time.monotonic()  # v5
                self._log({
                    "action": "recv_upgrade",
                    "msg_id": msg_id,
                    "message_text": text,
                    "user_emotion_analysis": analysis_dict,
                    "state": self.state.snapshot(now),
                })
            return

        # ── v4: 保存发送前状态用于追踪 ──
        prev_send_was_replied = self.state.cooldown.messages_without_reply > 0
        self.state.on_user_message(now, len(text), analysis=analysis_dict)

        # ── v9: 更新去重标记（记录是否已含分析）──
        self.state.cooldown.recv_dedup = {
            "text_sha": text_sha,
            "at": now.isoformat(),
            "analysis": analysis_dict is not None,
        }

        # ── v4: 人格自适应（收到回复后）──
        if prev_send_was_replied:
            try:
                self.state.adapt_personality({
                    "type": "character_send",
                    "was_replied": True,
                    "trigger": "user_reply",
                })
            except Exception:
                pass

        self.state.save()
        self._monotonic_at_save = time.monotonic()  # v5

        # ── v5: 记录 recv 到决策日志 ──
        recv_entry = {
            "action": "recv",
            "msg_id": msg_id,
            "message_text": text,
            "message_length": len(text),
            "state": self.state.snapshot(now),
        }
        if analysis_dict:
            recv_entry["user_emotion_analysis"] = analysis_dict
        self._log(recv_entry)

        # ── v5: 记录到对话归档 ──
        self._log_message(
            msg_id=msg_id,
            direction="recv",
            text=text,
            user_emotion_analysis=analysis_dict,
        )

    def _log_message(self, msg_id: str, direction: str, text: str,
                     trigger: str = None, intensity: str = None,
                     user_emotion_analysis: dict = None):
        """追加人类可读对话记录到 chiguo_messages.jsonl。"""
        now = datetime.now(CST)
        snap = self.state.snapshot(now)
        record = {
            "msg_id": msg_id,
            "ts": now.isoformat(),
            "direction": direction,
            "text": text,
        }
        if direction == "send":
            record["trigger"] = trigger
            record["intensity"] = intensity
            record["emotion_snapshot"] = {
                "loneliness": round(snap["emotion"]["loneliness"], 0),
                "affection": round(snap["emotion"]["affection"], 0),
                "anxiety": round(snap["emotion"]["anxiety"], 0),
                "energy": round(snap["emotion"]["energy"], 0),
                "tsundere_index": round(snap["emotion"]["tsundere_index"], 0),
            }
        else:
            if user_emotion_analysis:
                record["user_emotion_analysis"] = user_emotion_analysis
        try:
            with open(self.messages_log_path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 消息归档失败不影响主流程

    def record_send_text(self, msg_id: str, text: str,
                         trigger: str = None, intensity: str = None):
        """记录已发送消息文本（发送侧回调）。
        仅写 chiguo_messages.jsonl —— decisions.jsonl 已有 send 决策条目。
        trigger/intensity 从 send 决策中提取，使 messages.jsonl 自包含。
        """
        self._log_message(
            msg_id=msg_id,
            direction="send",
            text=text,
            trigger=trigger,
            intensity=intensity,
        )

    def _has_send_result(self, msg_id: str) -> bool:
        """日志中是否已有该 msg_id 的 send_result 条目（幂等防护）。"""
        try:
            with open(self.log_path, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("action") == "send_result" and entry.get("msg_id") == msg_id:
                        return True
        except OSError:
            pass
        return False

    def record_send_result(self, msg_id: str, status: str, error: str = None):
        """v6 反馈闭环: 发送后回传结果。
        success → 记录 delivered；failed → 退款（情绪消耗/额度回滚）+ 记录。
        幂等: 同一 msg_id 重复上报不再退款（日志按 msg_id 去重）。
        time 用 %Y-%m-%d %H:%M 格式（与 snapshot 一致，monitor _extract_time 依赖）。"""
        now = datetime.now(CST)
        already_reported = self._has_send_result(msg_id)
        refunded = False
        if status == "failed" and not already_reported:
            # ── v6 修复: 仅当 msg_id 能在在途 Hawkes 事件中定位时退款 ──
            # 未知 msg_id → 只审计跳过，不执行退款副作用（防止凭空刷新逃生阀冷却/
            # 误删最后一条事件——旧实现恒走 pop()，乱序回传会删错事件）。
            # 兼容: 在途事件全部无 msg_id（旧 daemon 产生）→ 沿用旧语义退款。
            events = self.state.cooldown.event_timestamps
            matched = any(ev.get("msg_id") == msg_id for ev in events)
            legacy_events = bool(events) and all("msg_id" not in ev for ev in events)
            if matched or legacy_events:
                self.state.refund_send(now, msg_id=msg_id)
                self.state.save()
                refunded = True
            else:
                print(
                    f"[chiguo_daemon] refund skipped: msg_id={msg_id!r} "
                    f"not found in {len(events)} in-flight events",
                    file=sys.stderr,
                )
        result = {
            "action": "send_result",
            "msg_id": msg_id,
            "status": status,
            "error": error,
            "time": now.strftime("%Y-%m-%d %H:%M"),
            "refunded": refunded,
            "duplicate": already_reported,
        }
        self._log(result)
        return result

    def snapshot(self):
        return self.state.snapshot(datetime.now(CST))


def _load_light_config(config_path: str | None = None) -> dict:
    """轻量分支共用:读 toml + 注入 _base_dir(config 所在目录)。不构造任何引擎对象。"""
    if config_path is None:
        config_path = str(Path(__file__).resolve().parent / "chiguo_proactive.toml")
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(Path(config_path).resolve().parent)
    return cfg


def _cmd_attention(config_path: str | None = None):
    """--attention 轻量读(§5.4):T1/T2/T3 组装 + 情感快照。零写副作用,毫秒级。"""
    import json as _json
    from schedule.sources import load_sources
    from schedule.attention import build_attention
    cfg = _load_light_config(config_path)
    try:
        src = load_sources(cfg["_base_dir"], cfg)
        att = build_attention(src, datetime.now(CST).date())
        emotion = {}
        try:
            st = _json.loads((Path(cfg["_base_dir"]) / "chiguo_state.json").read_text())
            emotion = st.get("emotion", {})
        except Exception:
            pass
        print(_json.dumps({"action": "attention", "ok": True, "attention": att,
                           "emotion": emotion, "week_num": att["week_num"],
                           "today_exceptions": att["today_exceptions"]}, ensure_ascii=False))
    except Exception as e:
        print(_json.dumps({"action": "attention", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --attention 失败: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_schedule_recall(query: str, config_path: str | None = None):
    """--schedule-recall <query>:recall 检索(A4 形状;失败 ok:false + exit 1,bridge 降级普通回复)。"""
    import json as _json
    from schedule.sources import load_sources
    from schedule.recall import recall
    cfg = _load_light_config(config_path)
    try:
        r = recall(query, load_sources(cfg["_base_dir"], cfg), datetime.now(CST).date())
    except Exception as e:
        print(_json.dumps({"action": "schedule_recall", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-recall 失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(_json.dumps({"action": "schedule_recall", "ok": True, "query": r["query"],
                       "matches": r["matches"]}, ensure_ascii=False))


def _cmd_schedule_change(json_arg: str, config_path: str | None = None):
    """--schedule-change <json>:写安排(二十轮 A4 形状;畸形 JSON → bad_json 不写入;ApiRejection → H5 文案)。"""
    import json as _json
    from schedule.api import ScheduleApi, ApiRejection
    from schedule.confirm import build_question
    cfg = _load_light_config(config_path)
    try:
        item = _json.loads(json_arg)
    except (_json.JSONDecodeError, TypeError):
        print(_json.dumps({"action": "schedule_change", "ok": False,
                           "reason": "bad_json", "question": "处理失败,再试一次?"}, ensure_ascii=False))
        print("[chiguo_daemon] --schedule-change 畸形 JSON,未写入", file=sys.stderr)
        sys.exit(1)
    try:
        api = ScheduleApi(cfg["_base_dir"], cfg)
        if isinstance(item, dict) and item.get("kind") == "remove":
            result = api.remove_override(item.get("match", {}))
        else:
            result = api.apply_override(item)
    except ApiRejection as e:
        question, missing = build_question(e.category)
        out = {"action": "schedule_change", "ok": False, "reason": e.category, "question": question}
        if missing:
            out["missing"] = missing
        print(_json.dumps(out, ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-change 拒绝({e.category}): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(_json.dumps({"action": "schedule_change", "ok": False,
                           "reason": "internal_error", "question": "处理失败,再试一次?"},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-change 异常: {e}", file=sys.stderr)
        sys.exit(1)
    print(_json.dumps({"action": "schedule_change", "ok": True, "text": result["text"]},
                      ensure_ascii=False))


# ── 入口 ──────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="迟菓主动消息 决策引擎")
    parser.add_argument("--version", action="version", version=f"chiguo v{VERSION} (规则: 每轮修改 +0.1)")
    parser.add_argument("--loop", type=int, nargs="?", const=300, metavar="SECONDS",
                        help="循环评估间隔秒数（最小60）")
    parser.add_argument("--user-msg", type=str, default=None,
                        help="记录哥哥消息")
    parser.add_argument("--analysis", type=str, default=None,
                        help="LLM情感分析JSON（配合 --user-msg 使用）")
    # ── v6: 文件传参（避免 shell 转义问题，SKILL.md 已采用此路径）──
    parser.add_argument("--user-msg-file", type=str, default=None,
                        help="消息文本文件（配合 --analysis-file 使用）")
    parser.add_argument("--analysis-file", type=str, default=None,
                        help="LLM分析JSON文件（配合 --user-msg-file 使用）")
    parser.add_argument("--status", action="store_true",
                        help="显示状态")
    parser.add_argument("--compact", action="store_true",
                        help="紧凑输出（cron用，idle时不输出）")
    parser.add_argument("--anniversary", type=str, default=None,
                        help="纪念日管理: add anniversary <DATE> <NAME> / remove <ID> / list / update <ID> key=val...")
    parser.add_argument("--break", type=str, default=None, dest="break_cmd",
                        metavar="CMD",
                        help="寒暑假: on|off|status|add <起> <止> <备注>|remove <序号>|list|clear")
    parser.add_argument("--health", action="store_true",
                        help="健康检查：检测 daemon 最近是否正常运行")
    parser.add_argument("--attention", action="store_true",
                        help="注意力快照（T1/T2/T3 + 情感快照，轻量读，零写）")
    parser.add_argument("--schedule-recall", type=str, default=None,
                        metavar="QUERY",
                        help="安排回忆检索（日期或关键词）")
    parser.add_argument("--schedule-change", type=str, default=None,
                        metavar="JSON",
                        help="写安排（JSON: reminder/add/cancel/move/exam_week/remove）")
    parser.add_argument("--tune", action="store_true",
                        help="参数校准：基于回复延迟推荐 base_lambda 调整")
    parser.add_argument("--stats", type=int, nargs="?", const=7, metavar="DAYS",
                        help="统计摘要（默认7天，0=全部历史）")
    parser.add_argument("--alerts", action="store_true",
                        help="异常检测告警")
    parser.add_argument("--monitor", action="store_true",
                        help="完整监控报告（stats + alerts + health）")
    # ── v5: 对话日志 & 归档 ──
    parser.add_argument("--conversation", type=str, default=None,
                        metavar="DATE",
                        help="显示某天对话记录 (YYYY-MM-DD)")
    parser.add_argument("--conversation-days", type=int, default=None,
                        metavar="N",
                        help="显示最近N天对话记录")
    parser.add_argument("--export", type=str, nargs="?", const="json",
                        metavar="FORMAT",
                        help="导出对话历史 (默认json)")
    parser.add_argument("--record-send", type=str, default=None,
                        metavar="MSG_ID",
                        help="记录已发送消息文本 (配合 --text)")
    parser.add_argument("--text", type=str, default=None,
                        help="消息文本 (配合 --record-send)")
    parser.add_argument("--trigger", type=str, default=None,
                        help="触发类型 (配合 --record-send)")
    parser.add_argument("--intensity", type=str, default=None,
                        help="消息强度 (配合 --record-send)")
    # ── v6: 反馈闭环 ──
    parser.add_argument("--send-result", type=str, default=None, metavar="MSG_ID",
                        help="回传发送结果 (配合 --send-status success|failed, 可选 --error)")
    parser.add_argument("--send-status", type=str, default=None,
                        choices=["success", "failed"],
                        help="发送状态 (配合 --send-result)")
    parser.add_argument("--error", type=str, default=None, help="失败原因 (配合 --send-result)")
    # ── v5: 告警持久化 ──
    parser.add_argument("--alerts-all", action="store_true",
                        help="显示所有告警（含已解决）")
    parser.add_argument("--ack", type=str, default=None,
                        metavar="ALERT_ID",
                        help="确认告警 (配合 --alerts)")
    # ── v5: 日志轮转 ──
    parser.add_argument("--rotate", action="store_true",
                        help="强制日志轮转")
    args = parser.parse_args()

    # ── 参数校验 ──
    if args.loop is not None and args.loop < 60:
        print("[chiguo_daemon] interval < 60, using 60", file=sys.stderr)
    # v8: --ack 是告警确认参数，自动联动开启 alerts 处理（不再静默忽略）
    if args.ack and not args.alerts:
        print("[chiguo_daemon] --ack 需要 --alerts，已自动联动开启", file=sys.stderr)
        args.alerts = True

    # ── 纪念日 CRUD（独立分支，不影响决策引擎；批 5 改调 ScheduleApi） ──
    if args.anniversary:
        from schedule.api import ScheduleApi
        cfg = _load_light_config()
        api = ScheduleApi(cfg["_base_dir"], cfg)
        parts = args.anniversary.split()
        cmd = parts[0] if parts else ""
        try:
            if cmd == "add" and len(parts) >= 4:
                result = api.add_anniversary(parts[1], " ".join(parts[3:]), parts[2])
            elif cmd == "remove" and len(parts) >= 2:
                result = api.remove_anniversary(parts[1])
            elif cmd == "list":
                result = api.list_anniversaries()
            elif cmd == "update" and len(parts) >= 3:
                kwargs = {}
                for kv in parts[2:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        kwargs[k] = v
                result = api.update_anniversary(parts[1], **kwargs)
            else:
                result = {"error": f"未知子命令: {args.anniversary}",
                          "usage": "add anniversary <DATE> <NAME> / remove <ID> / list / update <ID> key=val"}
        except ValueError as e:
            result = {"action": "anniversary_added", "ok": False, "error": str(e)} \
                if cmd == "add" else {"action": "anniversary_updated", "ok": False, "error": str(e)}
        print(json.dumps(result, ensure_ascii=False))
        if result.get("error") or result.get("ok") is False:
            sys.exit(1)
        return

    # ── v5: 日志轮转（v8: 锚定 base_dir，从任意 cwd 运行都轮转项目文件）──
    if args.rotate:
        from chiguo_rotation import force_rotate
        engine = DecisionEngine()
        force_rotate(
            [str(engine._base_dir / "chiguo_decisions.jsonl"),
             str(engine._base_dir / "chiguo_messages.jsonl")],
            archive_dir=str(engine._base_dir / "archive"),
        )
        print(json.dumps({"action": "rotate", "ok": True}, ensure_ascii=False))
        return

    # ── v5: 记录已发送消息 ──
    if args.record_send:
        if not args.text:
            print(json.dumps({"error": "--text required with --record-send"}, ensure_ascii=False))
            sys.exit(1)
        engine = DecisionEngine()
        engine.record_send_text(args.record_send, args.text, args.trigger, args.intensity)
        print(json.dumps({"action": "send_text_recorded", "msg_id": args.record_send, "ok": True}, ensure_ascii=False))
        return

    # ── v6: 反馈闭环──
    if args.send_result:
        if not args.send_status:
            print(json.dumps({"error": "--send-status required with --send-result"}, ensure_ascii=False))
            sys.exit(1)
        engine = DecisionEngine()
        r = engine.record_send_result(args.send_result, args.send_status, args.error)
        print(json.dumps(r, ensure_ascii=False))
        return

    # ── v5: 对话查询 & 导出（v10: 锚定 base_dir，从任意 cwd 运行都读写项目文件）──
    if args.conversation or args.conversation_days or args.export:
        from chiguo_monitor import ChiguoMonitor
        engine = DecisionEngine()
        mon = ChiguoMonitor(
            log_path=str(engine._base_dir / "chiguo_decisions.jsonl"),
            state_path=str(engine._base_dir / "chiguo_state.json"),
            break_state_path=str(engine._base_dir / "break_state.json"),
            config_path=str(engine._base_dir / "chiguo_proactive.toml"),
            messages_log_path=str(engine._base_dir / "chiguo_messages.jsonl"),
        )
        if args.export:
            result = mon.export(format=args.export)
            print(result)
        elif args.conversation_days:
            msgs = mon.conversation(days=args.conversation_days)
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        elif args.conversation:
            msgs = mon.conversation(date_str=args.conversation)
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        return

    # ── 寒暑假模式切换（批 5 改调 ScheduleApi.set_break，输出形状逐键一致） ──
    if args.break_cmd:
        from schedule.api import ScheduleApi
        cfg = _load_light_config()
        api = ScheduleApi(cfg["_base_dir"], cfg)
        result = api.set_break(args.break_cmd)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("error") or result.get("ok") is False:
            sys.exit(1)
        return

    # ── 参数校准 ──
    if args.tune:
        engine = DecisionEngine()
        latencies = engine.state.cooldown.reply_latencies
        if len(latencies) < 5:
            print(json.dumps({
                "action": "tune",
                "error": f"需要至少 5 次交互数据，当前 {len(latencies)} 次",
                "hint": "发送几条消息并等待哥哥回复，积累数据后再试",
            }, ensure_ascii=False))
        else:
            import statistics
            median_h = statistics.median(latencies)
            avg_h = sum(latencies) / len(latencies)
            # 理想回复延迟 ~0.5h（30分钟内回复说明消息受欢迎）
            # 太高 → 太频繁 → 降低 base_lambda
            # 太低 → 太冷淡 → 提高 base_lambda
            current_base = engine.config.get("poisson", {}).get("base_lambda", 0.25)
            if median_h < 0.3:
                suggestion = "increase"
                new_base = min(0.5, current_base * 1.3)
                reason = f"哥哥回复很快（中位数 {median_h:.1f}h），可以更频繁"
            elif median_h > 3.0:
                suggestion = "decrease"
                new_base = max(0.05, current_base * 0.7)
                reason = f"哥哥回复较慢（中位数 {median_h:.1f}h），减少频率"
            else:
                suggestion = "keep"
                new_base = current_base
                reason = f"回复节奏适中（中位数 {median_h:.1f}h），保持当前参数"
            print(json.dumps({
                "action": "tune",
                "latency_count": len(latencies),
                "median_hours": round(median_h, 2),
                "avg_hours": round(avg_h, 2),
                "current_base_lambda": current_base,
                "suggestion": suggestion,
                "suggested_base_lambda": round(new_base, 3),
                "reason": reason,
                "hint": f"手动修改 chiguo_proactive.toml [poisson] base_lambda = {new_base:.3f}",
            }, ensure_ascii=False, indent=2))
        return

    # ── 监控系统（stats / alerts / monitor）（v10: 锚定 base_dir，从任意 cwd 运行都读写项目文件）──
    if args.stats is not None or args.alerts or args.monitor:
        from chiguo_monitor import ChiguoMonitor, AlertManager
        engine = DecisionEngine()
        mon = ChiguoMonitor(
            log_path=str(engine._base_dir / "chiguo_decisions.jsonl"),
            state_path=str(engine._base_dir / "chiguo_state.json"),
            break_state_path=str(engine._base_dir / "break_state.json"),
            config_path=str(engine._base_dir / "chiguo_proactive.toml"),
            messages_log_path=str(engine._base_dir / "chiguo_messages.jsonl"),
        )
        if args.alerts:
            am = AlertManager(state_path=str(engine._base_dir / "chiguo_alerts.json"))
            # --ack ALERT_ID
            if args.ack:
                ok = am.acknowledge(args.ack)
                print(json.dumps({"action": "ack", "alert_id": args.ack, "ok": ok}, ensure_ascii=False))
                return
            # ingest fresh alerts into persistent store
            fresh = mon.alerts()
            am.ingest(fresh)
            if args.alerts_all:
                result = am.list_all()
            else:
                result = am.list_active()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.monitor:
            report = mon.report(days=args.stats if args.stats is not None else 7)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            # --stats (with optional days)
            stats = mon.stats(days=args.stats)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    # ── 健康检查 ──
    if args.health:
        # v7: 基于引擎 _base_dir 锚定，不依赖 cwd
        health_engine = DecisionEngine()
        sp = health_engine.state.state_path
        healthy = False
        last_tick = None
        hours_ago = None
        error = None
        if sp.exists():
            try:
                data = json.loads(sp.read_text())
                last_tick = data.get("last_tick")
                if last_tick:
                    lt = datetime.fromisoformat(last_tick)
                    hours_ago = (datetime.now(CST) - lt).total_seconds() / 3600
                    healthy = hours_ago < 6  # 6小时内有过 tick
                    if not healthy:
                        error = f"last_tick {hours_ago:.1f}h ago (threshold: 6h)"
                else:
                    error = "no last_tick in state file"
            except Exception as e:
                error = f"state file read error: {e}"
        else:
            error = "state file not found (daemon never run?)"
        print(json.dumps({
            "healthy": healthy,
            "last_tick": last_tick,
            "hours_ago": round(hours_ago, 1) if hours_ago else None,
            "error": error,
        }, ensure_ascii=False))
        return

    # ── 批 5 轻量子命令（不构造 DecisionEngine/ChiguoState/ScheduleParser，毫秒级） ──
    if args.attention:
        _cmd_attention()
        return
    if args.schedule_recall:
        _cmd_schedule_recall(args.schedule_recall)
        return
    if args.schedule_change:
        _cmd_schedule_change(args.schedule_change)
        return

    engine = DecisionEngine()

    if args.status:
        snap = engine.snapshot()
        print(json.dumps({
            "character": "迟菓",
            "time": snap["time"],
            "dominant_layer": snap["dominant_layer"],
            "emotion": snap["emotion"],
            "cooldown": snap["cooldown"],
        }, ensure_ascii=False, indent=2))
        return

    if args.user_msg_file:
        try:
            args.user_msg = Path(args.user_msg_file).read_text(encoding="utf-8")
        except OSError as e:
            print(json.dumps({"error": f"读取消息文件失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
    if args.analysis_file:
        try:
            args.analysis = Path(args.analysis_file).read_text(encoding="utf-8")
        except OSError as e:
            print(json.dumps({"error": f"读取分析文件失败: {e}"}, ensure_ascii=False))
            sys.exit(1)

    if args.user_msg:
        engine.record_user_message(args.user_msg, args.analysis)
        # 用户刚发消息 → 立即评估一次（情绪最新，最佳联系窗口）
        decision = engine.evaluate()
        if args.compact and decision["action"] == "idle":
            compact = {"action": "idle", "time": datetime.now(CST).isoformat()}
            if "next_evaluation_at" in decision:
                compact["next_evaluation_at"] = decision["next_evaluation_at"]
            print(json.dumps(compact, ensure_ascii=False))
        else:
            print(json.dumps(decision, ensure_ascii=False, indent=2))
        return

    if args.loop:
        max_interval = args.loop  # 用户设定的最大间隔（上限）

        # ── v5: PID 锁文件，防止双开（v6: 锚定 base_dir）──
        pid_path = engine._base_dir / "chiguo_loop.pid"
        if pid_path.exists():
            try:
                old_pid = int(pid_path.read_text().strip())
                try:
                    os.kill(old_pid, 0)  # 信号 0 只检查进程是否存在
                    print(f"❌ 已有实例运行 (PID {old_pid})，拒绝启动", file=sys.stderr)
                    sys.exit(1)
                except OSError:
                    pass  # 进程不存在 → 清理过期锁
            except (ValueError, OSError):
                pass
        pid_path.write_text(str(os.getpid()))
        print(f"🔒 PID 锁: {os.getpid()}", file=sys.stderr)

        print(f"🔄 决策引擎每 ≤{max_interval}s 动态评估一次 (v4 动态休眠)", file=sys.stderr)

        def run():
            decision = engine.evaluate()
            if decision["action"] == "send":
                print(json.dumps(decision, ensure_ascii=False))
            elif not args.compact:
                print(json.dumps(decision, ensure_ascii=False))
            else:
                print(json.dumps({"action": "idle", "version": VERSION, "time": datetime.now(CST).isoformat()}, ensure_ascii=False))
            sys.stdout.flush()
            return decision

        decision = run()
        try:
            while True:
                # ── v4: 动态休眠 ──
                now = datetime.now(CST)
                dynamic_sec = engine._dynamic_sleep_interval(now, decision)
                sleep_sec = min(dynamic_sec, max_interval)
                sleep_sec = max(60, sleep_sec)  # 下限 1 分钟
                time.sleep(sleep_sec)
                decision = run()
        except KeyboardInterrupt:
            print("\n💤 已停止", file=sys.stderr)
            engine.state.save()
        finally:
            # ── v5: 清理 PID 锁（v6: 复用创建时的锚定路径，勿按 cwd 重建）──
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
        return

    # 默认：单次评估
    decision = engine.evaluate()
    if args.compact and decision["action"] == "idle":
        # 紧凑模式 idle 时输出最小 heartbeat（用于 cron 健康检查）
        print(json.dumps({"action": "idle", "version": VERSION, "time": datetime.now(CST).isoformat()}, ensure_ascii=False))
        return
    print(json.dumps(decision, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
