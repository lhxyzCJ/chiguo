"""state.emotion — 情绪推进/基线域（AUD-001）。"""

import math
import random
import re
from datetime import datetime, timedelta

from chiguo_math import elastic_recover, apply_interaction_matrix, ou_step, noise_cap, baseline_shift_of
from chiguo_state_models import ChiguoEmotion, BASELINE_DEFAULTS
from chiguo_time import CST
from dataclasses import asdict


class EmotionMixin:
    def _tick_loneliness(self, cur: float, hours: float, silent_h: float, cfg: dict) -> float:
        """纯 helper：孤独向 baseline 弹性恢复，静默>24h 半衰期×0.6。"""
        hl = cfg.get("loneliness_gain_half_life", 40.0)
        if silent_h > 24:
            hl *= 0.6
        return elastic_recover(cur, self.emotion.baseline_loneliness, hours, hl, cfg.get("elastic_baseline", 100.0))

    def _tick_anxiety(self, cur: float, hours: float, now: datetime, cfg: dict) -> float:
        """纯 helper：不安向 baseline 恢复，节假日/课表调节半衰期。"""
        hl = cfg.get("anxiety_gain_half_life", 30.0)
        if self.holiday_parser is not None and self.holiday_parser.is_holiday(now):
            hl *= 2.5
        elif self.holiday_parser is not None and not self.holiday_parser.is_school_day(now):
            hl *= 2.0
        else:
            try:
                sch = self.schedule_status(now)
                if sch and sch["in_class"]:
                    hl *= 1.8
                elif sch and sch.get("class_load") == "heavy":
                    hl *= 1.4
            except (ValueError, TypeError, OSError):
                logging.debug("anxiety 半衰期调制失败: %s", __import__('traceback').format_exc(), exc_info=False)
        return elastic_recover(cur, self.emotion.baseline_anxiety, hours, hl, cfg.get("elastic_baseline", 100.0))

    def _tick_affection(self, cur: float, hours: float, silent_h: float, cfg: dict) -> float:
        """纯 helper：好感向 baseline 极慢靠拢，静默>24 才动。"""
        if silent_h <= 24:
            return cur
        ahl = cfg.get("affection_loss_half_life", 500.0)
        return elastic_recover(cur, self.emotion.baseline_affection, hours, ahl, cfg.get("elastic_baseline", 100.0))

    def _tick_energy(self, cur: float, hours: float, cfg: dict) -> float:
        """纯 helper：元气向 100 恢复。"""
        hl = cfg.get("energy_regen_half_life", 8.0)
        return elastic_recover(cur, 100.0, hours, hl, cfg.get("elastic_baseline", 100.0))

    def _tick_tsundere(self, hours: float) -> None:
        if self.emotion.affection > 65:
            self.emotion.tsundere_index -= 0.3 * hours
        if self.emotion.anxiety > 60:
            self.emotion.tsundere_index += 0.2 * hours
        baseline = self.personality.tsundere_intensity
        if self.emotion.tsundere_index != baseline:
            self.emotion.tsundere_index += (baseline - self.emotion.tsundere_index) * (1 - 2.0 ** (-hours / 200.0))

    def _tick_noise(self, old_lo: float, old_anx: float, hours: float, cfg: dict) -> None:
        """OU 噪声：经 closure 传递 _noise_x 私有可变状态，不共享。"""
        if cfg.get("noise_enabled", 0) == 0:
            return
        try:
            theta = float(cfg.get("noise_theta", 0.5))
            lo_sigma = float(cfg.get("noise_loneliness_sigma", 0.3))
            anx_sigma = float(cfg.get("noise_anxiety_sigma", 0.3))
        except (TypeError, ValueError):
            theta, lo_sigma, anx_sigma = 0.5, 0.3, 0.3
        rng = self._noise_rng()
        lo_step = abs(self.emotion.loneliness - old_lo)
        anx_step = abs(self.emotion.anxiety - old_anx)
        nx = self._noise_x
        prev_lo, prev_anx = nx["loneliness"], nx["anxiety"]
        x_lo = ou_step(prev_lo, 0.0, theta, lo_sigma, hours, rng)
        x_anx = ou_step(prev_anx, 0.0, theta, anx_sigma, hours, rng)
        nx["loneliness"], nx["anxiety"] = x_lo, x_anx
        self.emotion.loneliness += noise_cap(lo_step, x_lo - prev_lo)
        self.emotion.anxiety += noise_cap(anx_step, x_anx - prev_anx)

    def _tick_baseline_forget(self, hours: float, cfg: dict) -> None:
        try:
            hl = float(cfg.get("baseline_forget_half_life", 720.0))
        except (TypeError, ValueError):
            hl = 720.0
        if hl <= 0 or hours <= 0:
            return
        for dim, dflt in BASELINE_DEFAULTS.items():
            key = f"baseline_{dim}"
            cur = getattr(self.emotion, key)
            if cur != dflt:
                setattr(self.emotion, key, cur + (dflt - cur) * (1 - 2.0 ** (-hours / hl)))

    def tick(self, hours: float, now: datetime):
        """推进时间：4 delta helpers + 交互矩阵 + OU + 基线淡忘。"""
        cfg = self.config.get("emotion", {})
        silent_h = self.cooldown.silent_hours(now)
        old_lo, old_anx = self.emotion.loneliness, self.emotion.anxiety
        self.emotion.loneliness = self._tick_loneliness(old_lo, hours, silent_h, cfg)
        self.emotion.anxiety = self._tick_anxiety(old_anx, hours, now, cfg)
        if hours > 0.01:
            self.emotion.loneliness_rate = (self.emotion.loneliness - old_lo) / hours
            self.emotion.anxiety_rate = (self.emotion.anxiety - old_anx) / hours
        self.emotion.affection = self._tick_affection(self.emotion.affection, hours, silent_h, cfg)
        self._tick_tsundere(hours)
        self.emotion.energy = self._tick_energy(self.emotion.energy, hours, cfg)
        new_vals = apply_interaction_matrix(asdict(self.emotion), cfg)
        for k, v in new_vals.items():
            setattr(self.emotion, k, v)
        self._tick_noise(old_lo, old_anx, hours, cfg)
        self._tick_baseline_forget(hours, cfg)
        self._finalize(now)

    def update_emotion_baseline(self, interaction: dict):
        """v1.11 ④: 事件驱动情绪基线漂移（关系动力学）。
        与 adapt_personality 并列调用（输入复用同一 interaction dict）。
        - baseline_drift_rate=0（默认）→ 恒等关闭（灰度先例）
        - 每次事件漂移 = rate × baseline_shift_<dim>（默认 0.15）
        - 有界钳位 [全局默认 ± baseline_max_drift]（默认 20，防极端化）
        职责边界：只漂移 loneliness/anxiety/affection 三个关系感受维度，
        tsundere 全部归人格层（避免双重回归打架）。"""
        cfg = self.config.get("emotion", {})
        try:
            rate = float(cfg.get("baseline_drift_rate", 0.0))
        except (TypeError, ValueError):
            rate = 0.0
        if rate <= 0:
            return
        try:
            max_drift = float(cfg.get("baseline_max_drift", 20.0))
        except (TypeError, ValueError):
            max_drift = 20.0
        shift = baseline_shift_of(interaction)
        for dim, d in shift.items():
            if d == 0:
                continue
            try:
                step = float(cfg.get(f"baseline_shift_{dim}", 0.15))
            except (TypeError, ValueError):
                step = 0.15
            key = f"baseline_{dim}"
            cur = getattr(self.emotion, key) + d * rate * step
            dflt = BASELINE_DEFAULTS[dim]
            setattr(self.emotion, key, max(dflt - max_drift, min(dflt + max_drift, cur)))

    def _noise_rng(self):
        """②: 惰性创建独立 random.Random 实例（非 dataclass 字段，不序列化）。
        种子来自 [emotion].noise_seed——与全局 random.seed(42) 序列完全隔离。"""
        rng = getattr(self, "_noise_rng_instance", None)
        if rng is None:
            try:
                seed = int(self.config.get("emotion", {}).get("noise_seed", 42))
            except (TypeError, ValueError):
                seed = 42
            rng = random.Random(seed)
            self._noise_rng_instance = rng
        if not hasattr(self, "_noise_x"):
            self._noise_x = {"loneliness": 0.0, "anxiety": 0.0}
        return rng
