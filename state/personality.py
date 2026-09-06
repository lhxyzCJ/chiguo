"""state.personality — 人格自适应域（Issue #379 自 state.interaction 拆出）。"""
from datetime import datetime
from chiguo_personality import PersonalityTraits, PersonalityDelta, PersonalityDeltas
from chiguo_time import CST


class PersonalityMixin:
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
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            self._audit("adapt_personality_error", repr(e))
