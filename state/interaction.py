"""state.interaction — 交互/人格/情绪影响/发送反馈域（AUD-001）。"""

import logging
import random
import re
import math
import sys
from datetime import datetime, timedelta
from dataclasses import asdict
from datetime import date as date_type
from pathlib import Path

from chiguo_math import in_quiet_window, sigmoid, decay, elastic_recover, dynamic_lambda, hawkes_intensity, longing_decay, apply_interaction_matrix, drop_damp, impact_inertia, user_mood_impact, MOOD_DELTA, ou_step, noise_cap, baseline_shift_of, mood_fresh, user_mood_note, self_mood_note
from chiguo_state_models import BASELINE_DEFAULTS, EVENT_DELTA, EVENT_TYPE_SYNONYMS, REFUND_FIFO_MAX, _memory_dedup_key, emotion_tag_snapshot
from chiguo_personality import PersonalityTraits, PersonalityDelta, PersonalityDeltas
from chiguo_circadian import bucket_for
from chiguo_time import CST
from trigger_types import TriggerType

from chiguo_pending import pending_add, pending_resolve, pending_mark_attempted, pending_prune


class InteractionMixin:
    def adapt_personality(self, interaction: dict):
        """
        根据互动微调人格。变化极小（每次 <0.15），经数周/月才显著。

        interaction types:
        - {"type": "user_reply", "warmth": float, "latency_category": str, "msg_length": int}
        - {"type": "character_send", "was_replied": bool, "trigger": str}
        """
        delta = PersonalityDelta()

        itype = interaction.get("type", "")

        if itype == "user_reply":
            try:
                warmth = float(interaction.get("warmth", 0.0))
            except (TypeError, ValueError):
                warmth = 0.0
            lat_cat = interaction.get("latency_category", "normal")
            try:
                msg_len = int(interaction.get("msg_length", 10))
            except (TypeError, ValueError):
                msg_len = 10

            if warmth > 0.3:
                delta = delta.evolve(PersonalityDeltas.WARM_REPLY)
            elif warmth < -0.2:
                delta = delta.evolve(PersonalityDeltas.COLD_REPLY)

            if lat_cat == "fast":
                delta = delta.evolve(PersonalityDeltas.FAST_REPLY)
            elif lat_cat == "slow":
                delta = delta.evolve(PersonalityDeltas.SLOW_REPLY)
            elif lat_cat == "very_slow":
                delta = delta.evolve(PersonalityDeltas.VERY_SLOW_REPLY)

            if msg_len > 30:
                delta = delta.evolve(PersonalityDeltas.LONG_MESSAGE)

        elif itype == "character_send":
            prev_send_was_replied = interaction.get("was_replied", False)
            if prev_send_was_replied:
                delta = delta.evolve(PersonalityDeltas.SENT_AND_REPLIED)
            else:
                delta = delta.evolve(PersonalityDeltas.SENT_NO_REPLY)

        self.personality.evolve(delta)

        try:
            rate = float(self.config.get("personality", {}).get("regress_rate", 0.01))
        except (ValueError, TypeError):
            rate = 0.01
        self.personality.regress_to_baseline(rate)

        self.personality_history.append({
            "ts": datetime.now(CST).isoformat(),
            "dims": {
                field_name: getattr(self.personality, field_name)
                for field_name in PersonalityTraits.__dataclass_fields__
            },
        })
        if len(self.personality_history) > 200:
            del self.personality_history[:-200]

    def add_pending_topic(self, topic: str, now: datetime, source: str = "analysis"):
        """薄包装：委托 chiguo_pending.pending_add（纯函数），保持 API 与行为不变。"""
        self.pending_topics = pending_add(self.pending_topics, topic, now, source)

    def resolve_pending_topic(self, topic: str | None, now: datetime):
        """薄包装：委托 pending_resolve。"""
        self.pending_topics = pending_resolve(self.pending_topics, topic, now)

    def mark_pending_topic_attempted(self, topic: str):
        """薄包装：委托 pending_mark_attempted（就地）。"""
        pending_mark_attempted(self.pending_topics, topic)

    def prune_pending_topics(self, now: datetime, max_age_hours: float = 48.0):
        """薄包装：委托 pending_prune。"""
        self.pending_topics = pending_prune(self.pending_topics, now, max_age_hours)

    def _cap_pending_topics(self, cap: int = 20):
        if len(self.pending_topics) > cap:
            self.pending_topics = self.pending_topics[-cap:]

    def _apply_analysis_impact(self, analysis: dict, now: datetime | None = None):
        """v9: LLM 分析微调独立应用（情绪影响 + 接话茬话题摄入）。
        供 on_user_message（首次记录）与 recv_dedup 升级路径（bridge 已记录后
        standing order 补分析）共用——只叠加分析维度，不重复基础回复效果。
        v1.11 ①: 额外消费 user_mood（用户情绪感知）——写入 cooldown.user_mood
        并叠加情绪 delta（系数默认 0 关闭）。
        B1: 事件类型化情绪 delta 最先应用（规则表直接加减，_anxiety_before_analysis
        在其后取值 → 事件 delta 不被 anxiety_sensitivity 二次缩放）。"""
        self.apply_event_delta(self._extract_event_type(analysis), now or datetime.now(CST))
        self._anxiety_before_analysis = self.emotion.anxiety
        self._apply_emotion_impact(analysis, now)

        self._consume_user_mood(analysis, now or datetime.now(CST))
        mood = self.cooldown.user_mood
        if mood_fresh(mood, now or datetime.now(CST),
                      self.config.get("trigger", {}).get("user_mood_ttl_minutes", 360.0)):
            cfg = self.config.get("emotion", {})
            for dim, d in user_mood_impact(
                    mood.get("mood", "calm"), mood.get("intensity", 0.0), cfg).items():
                setattr(self.emotion, dim, getattr(self.emotion, dim) + d)

        topic = analysis.get("topic")
        if analysis.get("topic_resolved"):
            self.resolve_pending_topic(topic, now)
        elif topic:
            self.add_pending_topic(topic, now)

        self.emotion.clamp()

    def apply_analysis_impact(self, analysis: dict, now: datetime | None = None):
        """T11·Q1 公开 API：仅补分析微调路径（recv_dedup 升级），与 _apply_analysis_impact 同源。"""
        self._apply_analysis_impact(analysis, now)

    def _consume_user_mood(self, analysis: dict, now: datetime):
        """v1.11 ①: 解析 analysis 的 user_mood/user_mood_intensity → cooldown.user_mood。
        容错语义（5 层）：analysis 无 user_mood 键 / 非法枚举 / 非数值强度 → 本次
        零效果且**保留旧感知**（旧 analysis 天然兼容，TTL 由读取端 mood_fresh 判定）；
        仅显式 calm 或强度 <=0 才清空感知。"""
        if "user_mood" not in analysis:
            return  # 本次未感知 → 不覆盖旧感知
        try:
            mood = str(analysis.get("user_mood", "calm")).strip().lower()
        except (TypeError, ValueError):
            return
        if mood == "calm":
            self.cooldown.user_mood = None  # 显式平静 → 清空
            return
        if mood not in MOOD_DELTA:
            return  # 非法枚举 → 视为未感知，保留旧感知
        try:
            intensity = float(analysis.get("user_mood_intensity", 0.0))
        except (TypeError, ValueError):
            return
        intensity = max(0.0, min(1.0, intensity))
        if intensity <= 0:
            self.cooldown.user_mood = None  # 强度 0 → 等价平静
        else:
            self.cooldown.user_mood = {
                "mood": mood, "intensity": intensity, "at": now.isoformat()}

    @staticmethod
    def _normalize_event_type(event_type: str) -> str:
        """B1: 事件类型宽松归一化（小写 + 去标点，保留中文/字母/数字/下划线）。
        下划线保留以便规范键 new_topic 直接命中规则表；空格等其余字符去除。"""
        s = str(event_type or "").strip().lower()
        return re.sub(r"[^a-z0-9_一-鿿]", "", s)

    def _extract_event_type(self, analysis: dict) -> str | None:
        """B1: 从 analysis JSON 宽松提取事件类型。

        优先显式 event_type/event 键；缺省按信号推断（warmth 正负 → 夸奖/批评、
        user_mood 低落 → comfort、有 topic → new_topic）。返回原始字符串，
        由 apply_event_delta 内部归一化+别名映射；无事件 → None（零效果）。
        """
        if not isinstance(analysis, dict):
            return None
        for key in ("event_type", "event"):
            v = analysis.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        try:
            warmth = float(analysis.get("warmth", 0.0))
        except (TypeError, ValueError):
            warmth = 0.0
        mood = analysis.get("user_mood")
        if isinstance(mood, str) and mood.strip().lower() in ("low", "distressed"):
            return "comfort"
        if warmth > 0.3:
            return "praise"
        if warmth < -0.2:
            return "criticism"
        if analysis.get("topic"):
            return "new_topic"
        return None

    def apply_event_delta(self, event_type: str, now: datetime | None = None):
        """B1: 事件类型化情绪 delta 入口（规则表命中直接加减，不走 inertia）。

        now 保留为签名扩展位（未来可做按时间衰减）；当前直接加减。未知事件类型
        → 零效果。event_delta_enabled=False（默认）→ 整体恒等跳过。
        """
        cfg = self.config.get("emotion", {})
        if not cfg.get("event_delta_enabled", False):
            return
        if not event_type:
            return
        key = self._normalize_event_type(event_type)
        key = EVENT_TYPE_SYNONYMS.get(key, key)
        delta = EVENT_DELTA.get(key)
        if not delta:
            return
        for dim, d in delta.items():
            if hasattr(self.emotion, dim):
                setattr(self.emotion, dim, getattr(self.emotion, dim) + d)
        self.emotion.clamp()

    def record_trigger_sent(self, trigger_type: str):
        """A2: 发送一条消息 → 该触发类型 sent+1（daemon record_send_text 调用）。

        同时把 trigger 推进 FIFO 归因队列（未回复发送按发送顺序排队，回复时消费最旧
        一条）——防多条未回复发送时回复全记给最新 trigger（审查 #6）。队列有界截断
        （保留最近 64 条，防用户长期不回导致无界增长）。
        """
        if not trigger_type:
            return
        key = str(trigger_type)
        stats = self.cooldown.reply_stats.setdefault(key, {"sent": 0, "replied": 0})
        stats["sent"] = stats.get("sent", 0) + 1
        pending = self.cooldown.reply_pending
        pending.append(key)
        if len(pending) > 64:
            del pending[:-64]

    def record_trigger_replied(self):
        """A2: 收到一次回复 → FIFO 归因队列最旧一条未回复发送的 replied+1
        （daemon --user-msg 调用）。队列空（无未回复发送）则零效果。

        修复审查 #6：原实现取 trigger_history[-1]（最近发送），多条未回复发送时
        回复全部记给最新 trigger，导致早期发送回复率被系统性低估。
        """
        if not self.cooldown.reply_pending:
            return
        trigger_type = self.cooldown.reply_pending.pop(0)
        stats = self.cooldown.reply_stats.setdefault(str(trigger_type), {"sent": 0, "replied": 0})
        stats["replied"] = stats.get("replied", 0) + 1

    def _compute_latency(self, now: datetime) -> float | None:
        """计算距上次发送的小时数并维护 latencies 滑动 20。纯 helper 可测。"""
        if not self.cooldown.last_message_at:
            return None
        try:
            last = datetime.fromisoformat(self.cooldown.last_message_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=CST)
            h = max(0.0, (now - last).total_seconds() / 3600)
            self.cooldown.reply_latencies.append(h)
            if len(self.cooldown.reply_latencies) > 20:
                self.cooldown.reply_latencies = self.cooldown.reply_latencies[-20:]
            return h
        except (ValueError, TypeError):
            return None

    def _decay_all(self, lo: float, anx: float, now=None, damp: float = 1.0) -> tuple[float, float]:
        """纯 helper：孤独/不安骤降 decay，可测。now 仅为兼容旧签。"""
        cfg = self.config.get("emotion", {})
        lo_hl = cfg.get("loneliness_decay_on_reply", 0.35)
        anx_hl = cfg.get("anxiety_decay_on_reply", 0.5)
        lo1 = lo + (decay(lo, 1.0, lo_hl) - lo) * damp
        anx1 = anx + (decay(anx, 1.0, anx_hl) - anx) * damp
        return lo1, anx1

    def _affection_gain(self, msg_length: int, affection_mult: float = 1.0, damp: float = 1.0) -> float:
        """纯 helper：好感增量，可测。"""
        g = self.config.get("emotion", {}).get("affection_gain_per_interaction", 0.8)
        if msg_length > 30:
            g *= 1.5
        return g * affection_mult * damp

    def _affection_energy(self, msg_length: int, affection_mult: float, damp: float) -> float:  # alias for spec
        return self._affection_gain(msg_length, affection_mult, damp)

    def _energy_bonus(self, energy_extra: float = 0.0, damp: float = 1.0) -> float:
        """纯 helper：元气奖励，可测。"""
        bonus = self.config.get("emotion", {}).get("energy_bonus_on_reply", 10.0)
        return (bonus + energy_extra) * damp

    def _tsundere_drop(self, extra: float = 0.0, damp: float = 1.0) -> float:
        return (1.5 + extra) * damp

    def _reset_rate_limit(self) -> None:
        self.cooldown.held_count = 0
        if self.cooldown.accumulated_lambda > 0:
            base = self.config.get("poisson", {}).get("base_lambda", 0.25)
            self.cooldown.accumulated_lambda = longing_decay(self.cooldown.accumulated_lambda, base, decay_factor=self.config.get("cooldown", {}).get("longing_decay_factor", 0.5))

    def _adapt_on_reply(self, latency_h: float | None, analysis: dict | None, msg_length: int) -> None:
        try:
            lat_cat = "normal"
            if latency_h is not None:
                if latency_h <= 0.08:
                    lat_cat = "fast"
                elif latency_h <= 1.0:
                    lat_cat = "normal"
                elif latency_h <= 6.0:
                    lat_cat = "slow"
                else:
                    lat_cat = "very_slow"
            warmth = analysis.get("warmth", 0.0) if analysis else 0.0
            inter = {"type": "user_reply", "warmth": warmth, "latency_category": lat_cat, "msg_length": msg_length}
            self.adapt_personality(inter)
            self.update_emotion_baseline(inter)
        except Exception as e:
            self._audit("adapt_personality_error", repr(e))

    def _record_bayesian(self, now: datetime, latency_h: float | None, msg_length: int) -> None:
        try:
            silence_h = self.cooldown.silent_hours(now, wall=True) if now else 0
            obs = {"reply_latency": round(latency_h, 3) if latency_h else None, "msg_length": msg_length, "silence_hours": round(silence_h, 2)}
            actual = "chatting" if latency_h is not None and latency_h < 0.5 else None
            self.bayesian_estimator.record_observation(obs, actual_state=actual)
        except Exception:
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

    def _latency_multiplier(self, latency_hours: float) -> dict:
        """
        根据回复速度返回情感变化倍率。
        秒回 → 好感×1.5, 元气+5, 傲娇多降-2
        正常 → 1.0
        很久才回 → 好感×0.4, 不安回升
        """
        cfg = self.config.get("emotion", {})
        fast = cfg.get("reply_fast_threshold", 0.08)        # 5分钟
        slow = cfg.get("reply_slow_threshold", 1.0)          # 1小时
        very_slow = cfg.get("reply_very_slow_threshold", 6.0)  # 6小时

        if latency_hours <= fast:
            return {
                "affection": cfg.get("reply_fast_affection_mult", 1.5),
                "energy_extra": cfg.get("reply_fast_energy_extra", 5.0),
                "tsundere_extra_drop": cfg.get("reply_fast_tsundere_extra", 2.0),
                "anxiety_rebound": 0,
            }
        elif latency_hours <= slow:
            return {}  # 正常值
        elif latency_hours <= very_slow:
            return {
                "affection": cfg.get("reply_slow_affection_mult", 0.7),
                "energy_extra": 0,
                "tsundere_extra_drop": 0,
                "anxiety_rebound": 0,
            }
        else:
            return {
                "affection": cfg.get("reply_very_slow_affection_mult", 0.4),
                "energy_extra": 0,
                "tsundere_extra_drop": 0,
                "anxiety_rebound": cfg.get("reply_very_slow_anxiety_rebound", 3.0),
            }

    def _inertia_params(self) -> tuple[float, float, float]:
        cfg = self.config.get("emotion", {})
        try:
            pos = float(cfg.get("impact_inertia_positive", 0.0))
            neg = float(cfg.get("impact_inertia_negative", 0.0))
            mod = float(cfg.get("impact_inertia_affection_mod", 0.0))
        except (TypeError, ValueError):
            pos = neg = mod = 0.0
        return pos, neg, mod

    def _damp(self, delta: float, channel: str = "auto") -> float:
        """惯性阻尼 helper：按通道效价选择键，可测。"""
        pos, neg, mod = self._inertia_params()
        if channel == "neg":
            return impact_inertia(delta, neg, neg, mod, self.emotion.affection)
        if channel == "pos":
            return impact_inertia(delta, pos, pos, mod, self.emotion.affection)
        return impact_inertia(delta, pos, neg, mod, self.emotion.affection)

    def _impact_warmth(self, warmth: float, cfg: dict) -> None:
        self.emotion.affection += self._damp(warmth * cfg.get("affection_warmth_factor", 1.5))
        self.emotion.energy += self._damp(warmth * cfg.get("energy_warmth_factor", 4.0))
        if warmth < 0:
            self.emotion.anxiety += self._damp(abs(warmth) * cfg.get("anxiety_warmth_recovery", 3.0), "neg")

    def _impact_effort(self, effort: float, cfg: dict) -> None:
        self.emotion.affection += self._damp(effort * cfg.get("affection_effort_factor", 1.0))
        self.emotion.tsundere_index -= self._damp(effort * cfg.get("tsundere_effort_factor", 2.0), "pos")

    def _impact_attention(self, attention: float, cfg: dict) -> None:
        self.emotion.energy += self._damp(attention * cfg.get("energy_attention_factor", 4.0))
        if attention < 0.3:
            self.emotion.anxiety += self._damp((0.3 - attention) * cfg.get("anxiety_ignore_factor", 2.0), "neg")

    def _impact_anxiety_sens(self) -> None:
        anx_sens = self.personality.anxiety_sensitivity()
        if anx_sens != 1.0 and hasattr(self, '_anxiety_before_analysis'):
            d = self.emotion.anxiety - self._anxiety_before_analysis
            if d != 0:
                self.emotion.anxiety = self._anxiety_before_analysis + d * anx_sens

    def _impact_busy(self, analysis: dict, now: datetime | None, num) -> None:
        suppress_hours = num("suppress_hours", 0, 0, 24)
        if now is not None and suppress_hours > 0:
            until = (now + timedelta(hours=suppress_hours)).isoformat()
            if self.cooldown.busy_suppress_until:
                try:
                    existing = datetime.fromisoformat(self.cooldown.busy_suppress_until)
                    if now + timedelta(hours=suppress_hours) > existing:
                        self.cooldown.busy_suppress_until = until
                except (ValueError, TypeError):
                    self.cooldown.busy_suppress_until = until
            else:
                self.cooldown.busy_suppress_until = until
        elif now is not None and "suppress_hours" in analysis and analysis.get("suppress_hours") == 0:
            self.cooldown.busy_suppress_until = None

    def _apply_emotion_impact(self, analysis: dict, now: datetime | None = None):
        cfg = self.config.get("emotion", {})
        def _num(key: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(analysis.get(key, default))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))
        warmth = _num("warmth", 0.0, -1.0, 1.0)
        effort = _num("effort", 0.0, 0.0, 1.0)
        attention = _num("attention", 0.0, 0.0, 1.0)
        self._impact_warmth(warmth, cfg)
        self._impact_effort(effort, cfg)
        self._impact_attention(attention, cfg)
        self._impact_anxiety_sens()
        self._impact_busy(analysis, now, _num)

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

    def current_lambda(self, now: datetime = None) -> float:
        """
        当前主动消息的事件率 λ（次/小时）。
        λ = base × sigmoid(孤独) × sigmoid(不安) × availability × 退避系数
        """
        cfg = self.config.get("poisson", {})
        base = cfg.get("base_lambda", 0.25)
        lo_mid = cfg.get("lambda_loneliness_mid", 50)
        lo_k = cfg.get("lambda_loneliness_k", 0.08)
        anx_mid = cfg.get("lambda_anxiety_mid", 45)
        anx_k = cfg.get("lambda_anxiety_k", 0.06)

        lam = dynamic_lambda(
            self.emotion.loneliness, self.emotion.anxiety,
            base, lo_mid, lo_k, anx_mid, anx_k,
        )

        if now:
            lam *= self.availability(now)

        decay_factor = self.config.get("cooldown", {}).get("no_reply_lambda_decay", 0.7)
        n = self.cooldown.messages_without_reply
        lam *= decay_factor ** min(n, 5)

        hawkes_cfg = self.config.get("hawkes", {})
        if hawkes_cfg.get("enabled", True) and self.cooldown.event_timestamps:
            alpha = hawkes_cfg.get("alpha", 0.3)
            beta = hawkes_cfg.get("beta", 0.5)
            window = hawkes_cfg.get("window_hours", 24.0)
            lam = hawkes_intensity(
                lam, self.cooldown.event_timestamps, now,
                alpha, beta, window,
            )

        emo_cfg = self.config.get("emotion", {})
        lo_rate = self.emotion.loneliness_rate
        anx_rate = self.emotion.anxiety_rate
        rate_boost = 1.0
        rate_boost += max(0, (lo_rate - 1.0) * emo_cfg.get("lambda_lo_rate_factor", 0.4))
        rate_boost += max(0, (anx_rate - 1.0) * emo_cfg.get("lambda_anx_rate_factor", 0.3))
        lam *= rate_boost

        return lam

    def trigger_weight(self, trigger_type: str | TriggerType) -> float:
        """
        返回某类触发在当前情绪下的概率权重（0~1）。
        不再 if loneliness > 55，而是平滑概率。
        """
        cfg = self.config.get("sigmoid", {})
        lo = self.emotion.loneliness
        anx = self.emotion.anxiety

        if trigger_type == TriggerType.LONELY_LOW:
            return sigmoid(lo, cfg.get("loneliness_low_mid", 38),
                           cfg.get("loneliness_low_k", 0.20))
        elif trigger_type == TriggerType.LONELY_MID:
            return sigmoid(lo, cfg.get("loneliness_mid_mid", 55),
                           cfg.get("loneliness_mid_k", 0.18))
        elif trigger_type == TriggerType.LONELY_HIGH:
            return sigmoid(lo, cfg.get("loneliness_high_mid", 78),
                           cfg.get("loneliness_high_k", 0.15))
        elif trigger_type == TriggerType.ANXIETY:
            return sigmoid(anx, cfg.get("anxiety_mid", 58),
                           cfg.get("anxiety_k", 0.12))
        return 0.0

    def is_longing_overflow(self) -> bool:
        """概率累积溢出检查：held_count > 3 且 λ 累积到阈值且焦虑不阻塞。"""
        cfg = self.config.get("cooldown", {})
        base_lambda = self.config.get("poisson", {}).get("base_lambda", 0.25)
        acc_lam = self.cooldown.accumulated_lambda  # v5: type is always float
        return (self.cooldown.held_count > 3
                and acc_lam >= base_lambda * 1.5
                and self.emotion.anxiety < cfg.get("anxiety_block_threshold", 70.0))

    def longing_break_eligible(self, now: datetime) -> bool:
        """逃生阀激活检查：高焦虑阻塞 + 持续沉默超限 + 冷却期外。"""
        cfg = self.config.get("cooldown", {})
        if not cfg.get("longing_break_enabled", True):
            return False
        block_th = cfg.get("anxiety_block_threshold", 70.0)
        if self.emotion.anxiety < block_th:
            return False  # 非阻塞态 → 交给 is_longing_overflow 正常路径
        min_silence = cfg.get("longing_break_min_silence_hours", 72.0)
        if self.cooldown.silent_hours(now, wall=True) < min_silence:
            return False
        if not self.cooldown.last_longing_break_at:
            return True
        try:
            last = datetime.fromisoformat(self.cooldown.last_longing_break_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=CST)
            cooldown_days = cfg.get("longing_break_cooldown_days", 3)
            return (now - last).total_seconds() >= cooldown_days * 86400
        except (ValueError, TypeError):
            return True

    def on_longing_break(self, now: datetime):
        """记录逃生阀破防时间，进入冷却。累积量由 on_character_message 归零。"""
        self.cooldown.last_longing_break_at = now.isoformat()

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
            legacy_events = all("msg_id" not in ev for ev in events)
            matched = False
            for i, ev in enumerate(events):
                if ev.get("msg_id") == msg_id:
                    memory_marker = ev.get("memory_marker") if isinstance(ev, dict) else None
                    del self.cooldown.event_timestamps[i]
                    matched = True
                    break
            if not matched and not legacy_events:
                print(f"[refund_send] msg_id {msg_id!r} 未匹配到事件记录，保留", file=sys.stderr)
                return False
            if not matched:
                memory_marker = self.cooldown.event_timestamps[-1].get("memory_marker") \
                    if isinstance(self.cooldown.event_timestamps[-1], dict) else None
                self.cooldown.event_timestamps.pop()  # legacy 事件：旧行为回退删除
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

    def clear_unreplied(self, now: datetime) -> None:
        """RF11 (M2): timeout_uncertain 的**轻量清算**——只把本消息在 on_character_message
        里 +1 的未回复计数回滚（messages_without_reply -1，有界到 0），**不**做完整退款。

        区分 refund_send：
          - refund_send（发送**确定失败**）回滚 energy/anxiety/messages_today/逃生阀冷却/
            Hawkes 事件 → 恢复可重发额度，制造下次 tick 重发窗口（已送达时=重复消息）。
          - clear_unreplied（发送结果**不确定**，如 /send 超时/非 JSON 体）只清未回复计数，
            不清额度/冷却/不删 Hawkes 事件、不恢复可重发窗口 —— 防「持续不确定 → 未回复
            计数无限累积 → backoff_level==2 silent 永久禁发」。计数可任意 -1 因每条 send
            决策恰好 +1；事件保留（送达状态未知，Hawkes 自激不强删）。
        """
        self.cooldown.messages_without_reply = max(0, self.cooldown.messages_without_reply - 1)
        self._finalize(now)

    def daily_max(self, now: datetime) -> int:
        """当日配额上限：沉默 <8h 按活跃配额（max_daily_active），否则按静默配额
        （max_daily_silent）。R13 (#315) 抽为公开方法 → can_send 与 decision 二次门禁
        探测共用同一公式（门禁豁免集单一事实源）。"""
        cfg = self.config.get("cooldown", {})
        silent_h = self.cooldown.silent_hours(now)
        return (cfg.get("max_daily_active", 4) if silent_h < 8
                else cfg.get("max_daily_silent", 2))

    def _daily_limit_break_ok(self, now: datetime, must_send: bool = False) -> bool:
        """日限额突破钥匙（门禁豁免集单一事实源，R13 #315）：
        ① is_longing_overflow 概率累积溢出；② 72h 逃生阀 longing_break；
        ③ must_send 高段必发（用户决策 2026-08-16：配额满也发）。
        must_send 只允许「恰好配额满」的一次突破：messages_today == daily_max →
        放行；超额发出后 == daily_max+1 → 封顶（超额每日 ≤1 条，防 spam）。"""
        if self.is_longing_overflow() or self.longing_break_eligible(now):
            return True
        if must_send:
            return self.cooldown.messages_today < self.daily_max(now) + 1
        return False

    def daily_limit_reached(self, now: datetime) -> bool:
        """日配额已满（messages_today >= daily_max）。供 decision 二次门禁
        （must_send 第三把钥匙探测）判定「当前拦截的可疑门禁是否为日限额」。"""
        return self.cooldown.messages_today >= self.daily_max(now)

    def can_send(self, now: datetime, quiet_ok: bool = False,
                 must_send: bool = False) -> bool:
        cfg = self.config.get("cooldown", {})

        if self.cooldown.messages_today >= self.daily_max(now):
            if not self._daily_limit_break_ok(now, must_send=must_send):
                return False

        min_interval = cfg.get("min_interval_minutes", 30)
        mins_since = self.cooldown.minutes_since_last_message(now)
        if mins_since is not None and mins_since < min_interval:
            return False

        if self.emotion.energy < 12:
            emo_cfg = self.config.get("emotion", {})
            if emo_cfg.get("rate_energy_override", False):
                threshold = emo_cfg.get("rate_energy_threshold", 5.0)
                min_energy = emo_cfg.get("rate_energy_min", 5)
                if (self.emotion.loneliness_rate > threshold and
                        self.emotion.energy >= min_energy):
                    pass  # 急迫 → 允许低元气发送
                else:
                    return False
            else:
                return False

        if not quiet_ok:
            qs, qe = self.cooldown.quiet_window()
            if in_quiet_window(now, qs, qe):
                if not self.longing_break_eligible(now):
                    return False

        if self.cooldown.is_busy_suppressed(now):
            return False

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

    def _prune_crash_history(self, now: datetime):
        """v6: 按 48h 窗口滑动过滤崩溃记录。crash_count_48h = 窗口内条数，
        last_crash_at = 窗口内最新一条。较早的崩溃独立过期，不再依赖最后一次。"""
        cutoff = now - timedelta(hours=48)
        kept = []
        for ts in self.cooldown.crash_timestamps:
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CST)
                if dt >= cutoff:
                    kept.append(ts)
            except (ValueError, TypeError):
                continue  # 坏时间戳直接丢弃
        self.cooldown.crash_timestamps = kept
        self.cooldown.crash_count_48h = len(kept)
        self.cooldown.last_crash_at = kept[-1] if kept else None

    def safety_level(self, now: datetime) -> int:
        """
        安全阀等级：防止连续崩溃吓到主人。
        0 = 正常
        1 = 崩溃冷却 (last_crash_at 在 24h 内) → 禁止 lonely_high
        2 = 强制温和模式 (48h 内 ≥2 次崩溃) → 所有触发降级
        """
        cfg = self.config.get("safety", {})
        if not cfg.get("enabled", True):
            return 0

        crash_window = cfg.get("crash_window_hours", 48)
        crash_max = cfg.get("crash_max_in_window", 2)
        cooldown_hours = cfg.get("crash_cooldown_hours", 24)

        if not self.cooldown.last_crash_at:
            return 0

        try:
            last = datetime.fromisoformat(self.cooldown.last_crash_at)
            hours_since = (now - last).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0

        if hours_since > crash_window:
            self._prune_crash_history(now)
            if not self.cooldown.last_crash_at:
                return 0

        if self.cooldown.crash_count_48h >= crash_max:
            return 2

        if hours_since < cooldown_hours:
            return 1

        return 0
