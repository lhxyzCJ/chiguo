"""state.interaction — 交互编排基座（Issue #379：按子域拆分为 personality / mood / pending / limits）。

InteractionMixin 组装四个子域 mixin，对外仍是 ChiguoState 唯一的 interaction 引入点，
门面调用方零改动。
"""
import logging
import sys
from datetime import datetime, timedelta
from chiguo_math import decay, drop_damp
from chiguo_state_models import REFUND_FIFO_MAX
from chiguo_time import CST
from trigger_types import TriggerType

from state.personality import PersonalityMixin
from state.mood import MoodMixin
from state.pending import PendingMixin
from state.limits import LimitsMixin


class InteractionMixin(PersonalityMixin, MoodMixin, PendingMixin, LimitsMixin):
    def _record_bayesian(self, now: datetime, latency_h: float | None, msg_length: int) -> None:
        try:
            silence_h = self.cooldown.silent_hours(now, wall=True) if now else 0
            obs = {"reply_latency": round(latency_h, 3) if latency_h else None, "msg_length": msg_length, "silence_hours": round(silence_h, 2)}
            actual = "chatting" if latency_h is not None and latency_h < 0.5 else None
            self.bayesian_estimator.record_observation(obs, actual_state=actual)
        except (ValueError, TypeError, OSError):
            logging.debug("bayesian 记录失败: %s", __import__('traceback').format_exc(), exc_info=False)

    def on_user_message(self, now: datetime, msg_length: int = 10, analysis: dict | None = None):
        """收到主人消息：情绪骤降编排（helpers 纯函数可测）。"""
        latency_h = self._compute_latency(now)
        lat_mult = self._latency_multiplier(latency_h) if latency_h is not None else {}
        cfg = self.config.get("cooldown", {})
        damp = self._reply_damp(now, window_minutes=cfg.get("drop_damp_window_minutes", 30), factor=cfg.get("drop_damp_factor", 0.5), cap=cfg.get("drop_damp_max", 3))
        lo, anx = self._decay_all(self.emotion.loneliness, self.emotion.anxiety, now, damp)
        self.emotion.loneliness, self.emotion.anxiety = lo, anx
        if lat_mult.get("anxiety_rebound", 0) > 0:
            self.emotion.anxiety += lat_mult["anxiety_rebound"]
        self.emotion.affection += self._affection_gain(msg_length, lat_mult.get("affection", 1.0), damp)
        self.emotion.energy += self._energy_bonus(lat_mult.get("energy_extra", 0), damp)
        self.emotion.tsundere_index -= self._tsundere_drop(lat_mult.get("tsundere_extra_drop", 0), damp)
        if analysis is not None:
            self._apply_analysis_impact(analysis, now)
        self.cooldown.last_user_message_at = now.isoformat()
        self.cooldown.last_user_msg_length = msg_length
        self.cooldown.messages_without_reply = 0
        if cfg.get("drop_damp_window_minutes", 30) > 0:
            self.cooldown.drop_events.append({"time": now.isoformat(), "direction": "reply"})
        self._reset_rate_limit()
        self._adapt_on_reply(latency_h, analysis, msg_length)
        self._record_bayesian(now, latency_h, msg_length)
        circ_cfg = self.config.get("circadian", {})
        self.circadian.record(now, circ_cfg.get("history_days", 14), self._current_bucket(now))
        self._relearn_windows(now)
        self._finalize(now)

    def _reply_damp(self, now: datetime, window_minutes: float = 30.0,
                    factor: float = 0.5, cap: int = 3) -> float:
        """A10: 30 分钟窗口内同向回复事件计数 → 饱和阻尼系数。
        recents = 窗口内已有同向事件数（不含本次）→ drop_damp(recents, factor, cap)。
        顺带清理窗口外事件（滚动窗口，防无限增长）；window_minutes <= 0 → 关闭（恒 1.0）。"""
        try:
            window_minutes = float(window_minutes)
        except (TypeError, ValueError):
            window_minutes = 30.0
        if window_minutes <= 0:
            self.cooldown.drop_events = []
            return 1.0
        cutoff = now - timedelta(minutes=window_minutes)
        kept: list[dict] = []
        recents = 0
        for ev in self.cooldown.drop_events:
            try:
                t = datetime.fromisoformat(str(ev.get("time", "")))
            except (ValueError, TypeError):
                continue  # 坏时间戳丢弃，不影响其余
            if t.tzinfo is None:
                t = t.replace(tzinfo=CST)
            if t < cutoff:
                continue  # 窗口外 → 丢弃
            kept.append(ev)
            if ev.get("direction") == "reply":
                recents += 1
        self.cooldown.drop_events = kept
        return drop_damp(recents, factor, cap)

    def on_character_message(self, now: datetime, trigger_type: str = "",
                             msg_id: str | None = None):
        """迟菓发出主动消息后。msg_id（v6）写入 Hawkes 事件，供 refund_send 按 id 回滚。"""
        cfg = self.config.get("emotion", {})

        cost = cfg.get("energy_cost_per_message", 20.0)
        self.emotion.energy = max(0, self.emotion.energy - cost)

        send_hl = cfg.get("loneliness_decay_on_send", 2.0)
        self.emotion.loneliness = decay(self.emotion.loneliness, 1.0, send_hl)

        anx_gain = cfg.get("anxiety_gain_on_send", 2.0)
        self.emotion.anxiety += anx_gain

        self.cooldown.last_message_at = now.isoformat()
        self.cooldown.messages_today += 1
        self.cooldown.messages_without_reply += 1

        if trigger_type:
            event = {
                "type": trigger_type,
                "time": now.isoformat(),
            }
            if msg_id is not None:
                event["msg_id"] = msg_id  # v6: 供 refund_send 按 msg_id 精确回滚
            self.cooldown.event_timestamps.append(event)
            if len(self.cooldown.event_timestamps) > 50:
                self.cooldown.event_timestamps = self.cooldown.event_timestamps[-50:]

        self.cooldown.held_count = 0
        self.cooldown.accumulated_lambda = 0.0

        crash_types = (TriggerType.LONELY_HIGH, TriggerType.ANXIETY)
        if trigger_type in crash_types:
            self.cooldown.last_crash_at = now.isoformat()
            self.cooldown.crash_timestamps.append(now.isoformat())
            if len(self.cooldown.crash_timestamps) > 50:
                self.cooldown.crash_timestamps = self.cooldown.crash_timestamps[-50:]
        self._prune_crash_history(now)

        self._finalize(now)

    def refund_send(self, now: datetime, msg_id: str | None = None) -> bool:
        """发送失败退款（v6 反馈闭环）：退还元气/不安消耗、日计数、未回复计数。
        消息从未真正发出 → 情绪消耗与额度统计全部回滚，下次 tick 可重发。
        - 重置逃生阀冷却：未送达的消息不该白扣 3 天破防机会。
        - held_count/accumulated_lambda 不回滚（每次发送都会清零，重累积即可）。
        - loneliness 缓降不回滚（决策本身已产生释压感，语义合理）。
        - v6: 提供 msg_id 时按 msg_id 精确移除对应 Hawkes 事件（乱序回传不弹错）；
          未提供 → 回退移除最后一条（旧行为，向后兼容）；
          提供但未匹配到任何在途事件（或在途为空）→ 不产生任何退款副作用，仅告警
          （防凭空刷新逃生阀冷却/误删其他事件，#83）。
        - legacy 事件（全部无 msg_id 键）→ 沿用旧回退 pop()（单一判定，见下）。
        - last_message_at 不还原（设计取舍，保持现状）。
        - F-A15-002: 有界 FIFO（refunded_msg_ids）记录已退款 msg_id——同 msg_id 越窗口
          重放（chiguo_decisions.jsonl 尾 500 行之外的 replay）双退被直接拒收。
        - 返回 True=已执行退款副作用（成本回滚+事件移除+逃生阀冷却重置）；
          False=msg_id 未在任何在途事件中定位且存在带 msg_id 的事件（或事件为空），
          调用方据此决定是否 save。msg_id 与 legacy 判定收敛于此单处。"""
        if msg_id is not None and msg_id in self.cooldown.refunded_msg_ids:
            print(f"[refund_send] msg_id {msg_id!r} 已退款过（FIFO），拒绝重复退款", file=sys.stderr)
            return False
        memory_marker = None
        if msg_id is not None:
            events = self.cooldown.event_timestamps
            if not events:
                print(f"[refund_send] msg_id {msg_id!r} 未匹配到事件记录，保留", file=sys.stderr)
                return False
            all_legacy_batch = all("msg_id" not in ev for ev in events)
            matched = False
            for i, ev in enumerate(events):
                if ev.get("msg_id") == msg_id:
                    memory_marker = ev.get("memory_marker") if isinstance(ev, dict) else None
                    del self.cooldown.event_timestamps[i]
                    matched = True
                    break
            if not matched and not all_legacy_batch:
                print(f"[refund_send] msg_id {msg_id!r} 未匹配到事件记录，保留", file=sys.stderr)
                return False
            if not matched:
                memory_marker = self.cooldown.event_timestamps[-1].get("memory_marker") \
                    if isinstance(self.cooldown.event_timestamps[-1], dict) else None
                self.cooldown.event_timestamps.pop()  # 无 msg_id 旧批次：回退删除最后一条
        elif self.cooldown.event_timestamps:
            memory_marker = self.cooldown.event_timestamps[-1].get("memory_marker") \
                if isinstance(self.cooldown.event_timestamps[-1], dict) else None
            self.cooldown.event_timestamps.pop()
        cfg = self.config.get("emotion", {})
        cost = cfg.get("energy_cost_per_message", 20.0)
        self.emotion.energy = min(100.0, self.emotion.energy + cost)
        anx_gain = cfg.get("anxiety_gain_on_send", 2.0)
        self.emotion.anxiety = max(0.0, self.emotion.anxiety - anx_gain)
        self.cooldown.messages_today = max(0, self.cooldown.messages_today - 1)
        self.cooldown.messages_without_reply = max(0, self.cooldown.messages_without_reply - 1)
        self.cooldown.last_longing_break_at = None
        if msg_id is not None:
            self.cooldown.refunded_msg_ids.append(msg_id)
            if len(self.cooldown.refunded_msg_ids) > REFUND_FIFO_MAX:
                self.cooldown.refunded_msg_ids = self.cooldown.refunded_msg_ids[-REFUND_FIFO_MAX:]
        if memory_marker:
            self._unmark_memory_by_key(memory_marker)
        self._finalize(now)
        return True

    def _finalize(self, now: datetime):
        """统一收尾：情绪归位 + 跨日重置。"""
        self.emotion.clamp()
        self._check_daily_reset(now)

    def _check_daily_reset(self, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if self.cooldown.current_date != today:
            self.cooldown.current_date = today
            self.cooldown.messages_today = 0
            self.cooldown.morning_sent = False
            self.cooldown.night_sent = False
