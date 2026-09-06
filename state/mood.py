"""state.mood — 情绪冲击/消耗/delta 域（Issue #379 自 state.interaction 拆出）。"""
import re
from datetime import datetime, timedelta
from chiguo_math import decay, impact_inertia, user_mood_impact, MOOD_DELTA, mood_fresh
from chiguo_state_models import EVENT_DELTA, EVENT_TYPE_SYNONYMS
from chiguo_time import CST


class MoodMixin:
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
