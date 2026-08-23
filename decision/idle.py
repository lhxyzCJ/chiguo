"""decision.idle — 空闲决策分支（AUD-002）。"""

import math
import random
import sys
import time
from datetime import datetime, timezone, timedelta

from decision.base import DecisionEngineBase
from chiguo_math import in_quiet_window, longing_accumulate
from chiguo_version import VERSION

CST = timezone(timedelta(hours=8))


class IdleMixin(DecisionEngineBase):
    def _bayesian_block_confidence(self) -> float:
        return self.config.get("bayesian", {}).get("min_confidence_for_block", 0.5)

    def _emit_idle(self, reason: str, now, user_state, data_warning: bool,
                   save_failed: bool = False) -> dict:
        if not save_failed:
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
        if not save_failed and not self.state.save():
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
        silent_h = self.state.cooldown.silent_hours(now)
        daily_max = self.config.get("cooldown", {}).get(
            "max_daily_active", 4) if silent_h < 8 else self.config.get("cooldown", {}).get("max_daily_silent", 2)
        if self.state.cooldown.messages_today >= daily_max:
            try:
                if not self.state.can_send(now, quiet_ok=quiet_ok):
                    return "daily_limit"
            except Exception:
                return "daily_limit"
        min_interval = self.config.get("cooldown", {}).get("min_interval_minutes", 30)
        mins_since = self.state.cooldown.minutes_since_last_message(now)
        if mins_since is not None and mins_since < min_interval:
            return "min_interval"
        if self.state.emotion.energy < 12:
            emo_cfg = self.config.get("emotion", {})
            override_ok = (
                emo_cfg.get("rate_energy_override", False)
                and self.state.emotion.loneliness_rate > emo_cfg.get("rate_energy_threshold", 5.0)
                and self.state.emotion.energy >= emo_cfg.get("rate_energy_min", 5)
            )
            if not override_ok:
                return "low_energy"
        if not quiet_ok:
            qs, qe = self.state.cooldown.quiet_window()
            if in_quiet_window(now, qs, qe):
                return "quiet_hours"
        if self.state.cooldown.is_busy_suppressed(now):
            return "busy_suppressed"
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
        cfg_emo = self.config.get("emotion", {})
        cfg_cooldown = self.config.get("cooldown", {})
        if idle_reason == "min_interval":
            min_int = cfg_cooldown.get("min_interval_minutes", 30)
            if self.state.cooldown.last_message_at:
                try:
                    last = datetime.fromisoformat(self.state.cooldown.last_message_at)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=CST)
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
                nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, qe, 2, tzinfo=CST)
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
            return (now + timedelta(seconds=3600 + random.uniform(0, 3600))).isoformat()
        elif idle_reason == "sleeping_guard":
            return None
        return None
