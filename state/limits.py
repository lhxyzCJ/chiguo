"""state.limits — 冷却限流/退场/日限域（Issue #379 自 state.interaction 拆出）。"""
from datetime import datetime, timedelta
from chiguo_math import sigmoid, dynamic_lambda, hawkes_intensity, longing_decay, in_quiet_window
from chiguo_time import CST
from trigger_types import TriggerType


class LimitsMixin:
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

    def _reset_rate_limit(self) -> None:
        self.cooldown.held_count = 0
        if self.cooldown.accumulated_lambda > 0:
            base = self.config.get("poisson", {}).get("base_lambda", 0.25)
            self.cooldown.accumulated_lambda = longing_decay(self.cooldown.accumulated_lambda, base, decay_factor=self.config.get("cooldown", {}).get("longing_decay_factor", 0.5))

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
