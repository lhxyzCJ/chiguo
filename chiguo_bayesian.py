# ============================================================
# chiguo_bayesian.py — 迟菓 Bayesian 用户状态推断引擎 v4
# 参考 revive-companion 的 bayesian/core.py 设计
# 从可观测信号推断用户隐藏状态，填补最大能力缺口
# ============================================================

import math
from datetime import datetime

from chiguo_time import CST  # Q22: 共享时区常量


class UserStateEstimator:
    """
    Bayesian 推断用户当前状态。

    6 种隐藏状态：
    - chatting:  正在聊天/活跃使用微信
    - browsing:  在刷手机/空闲浏览
    - busy:      工作/上课/忙碌中
    - sleeping:  睡觉中
    - away:      离开设备/未看手机
    - needs_care: 需要关心（情绪低落/生病/出事）

    可观测信号：
    - reply_latency:  回复延迟分类 (fast/normal/slow/very_slow/none)
    - msg_length:     消息长度分类 (short/medium/long/none)
    - silence_hours:  自上次用户消息以来的沉默时长
    - hour_of_day:    当前小时 (0-23)
    - is_weekend:     是否周末
    - in_class:       是否在上课

    参考：revive-companion 的 Bayesian state estimator
    """

    STATES = ["chatting", "browsing", "busy", "sleeping", "away", "needs_care"]

    # 合法观测空间（restore_state_dict 校验用）：obs_key → 合法 obs_value 集合。
    # 与 _init_default_likelihoods 的经验参数表保持一致。
    OBS_SPACE = {
        "reply_latency": {"fast", "normal", "slow", "very_slow", "none"},
        "msg_length": {"short", "medium", "long", "none"},
        "silence": {"active", "recent", "moderate", "long"},
    }

    # 状态 → 发送效用（越高越适合发送）
    UTILITY = {
        "chatting": 0.2,
        "browsing": 0.7,
        "busy": 0.1,
        "sleeping": 0.0,
        "away": 0.3,
        "needs_care": 0.9,
    }

    # 状态 → 人类可读描述
    STATE_DESCRIPTIONS = {
        "chatting": "正在聊天",
        "browsing": "在刷手机",
        "busy": "忙",
        "sleeping": "睡觉",
        "away": "离开",
        "needs_care": "需要关心",
    }

    # ── A1: 状态转移矩阵（前向滤波，state → {next_state: prob}）──
    # 行内概率归一化为 1（马尔可夫一行）；经验值：状态保持概率最高，
    # 睡觉/离开有向活跃状态滑落的倾向，needs_care 保持但会缓慢衰减。
    # config 可整体覆盖某一行：`[bayesian].transition_chatting = { chatting = 0.4, ... }`，
    # 覆盖后行内重新归一化。transition_enabled=False 时完全不参与推断（恒等）。
    TRANSITIONS = {
        "chatting":   {"chatting": 0.45, "browsing": 0.25, "busy": 0.10, "sleeping": 0.02, "away": 0.08, "needs_care": 0.10},
        "browsing":   {"chatting": 0.25, "browsing": 0.45, "busy": 0.10, "sleeping": 0.05, "away": 0.10, "needs_care": 0.05},
        "busy":       {"chatting": 0.10, "browsing": 0.10, "busy": 0.45, "sleeping": 0.05, "away": 0.25, "needs_care": 0.05},
        "sleeping":   {"chatting": 0.02, "browsing": 0.05, "busy": 0.03, "sleeping": 0.70, "away": 0.18, "needs_care": 0.02},
        "away":       {"chatting": 0.15, "browsing": 0.25, "busy": 0.10, "sleeping": 0.10, "away": 0.35, "needs_care": 0.05},
        "needs_care": {"chatting": 0.15, "browsing": 0.10, "busy": 0.05, "sleeping": 0.05, "away": 0.20, "needs_care": 0.45},
    }

    def __init__(self, config: dict = None):
        self.config = config or {}

        # ── A1: 上一 tick 后验（前向滤波），随状态落盘（to_state_dict/restore）──
        self.prev_posterior: dict | None = None

        # ── 似然参数表：P(obs_value | state) ──
        # 用经验规则初始化。key: (state, obs_key, obs_value)
        # 由 BayesianLearner 在线调优
        self._likelihood_cache: dict[tuple, float] = {}

        # 初始化默认似然值
        self._init_default_likelihoods()

        # 从配置读取 Bayesian 参数
        self.utility_threshold = float(self.config.get("utility_threshold", 0.4))
        lr = float(self.config.get("learning_rate", 0.05))

        # ── 在线学习器 ──
        self.learner = BayesianLearner(self, learning_rate=lr)


    def _init_default_likelihoods(self):
        """初始化经验似然参数表。"""
        # reply_latency 分布: P(latency_category | state)
        self._set_likelihood("chatting", "reply_latency", "fast", 0.60)
        self._set_likelihood("chatting", "reply_latency", "normal", 0.25)
        self._set_likelihood("chatting", "reply_latency", "slow", 0.10)
        self._set_likelihood("chatting", "reply_latency", "very_slow", 0.03)
        self._set_likelihood("chatting", "reply_latency", "none", 0.02)

        self._set_likelihood("browsing", "reply_latency", "fast", 0.30)
        self._set_likelihood("browsing", "reply_latency", "normal", 0.40)
        self._set_likelihood("browsing", "reply_latency", "slow", 0.20)
        self._set_likelihood("browsing", "reply_latency", "very_slow", 0.07)
        self._set_likelihood("browsing", "reply_latency", "none", 0.03)

        self._set_likelihood("busy", "reply_latency", "fast", 0.05)
        self._set_likelihood("busy", "reply_latency", "normal", 0.15)
        self._set_likelihood("busy", "reply_latency", "slow", 0.40)
        self._set_likelihood("busy", "reply_latency", "very_slow", 0.30)
        self._set_likelihood("busy", "reply_latency", "none", 0.10)

        self._set_likelihood("sleeping", "reply_latency", "fast", 0.01)
        self._set_likelihood("sleeping", "reply_latency", "normal", 0.02)
        self._set_likelihood("sleeping", "reply_latency", "slow", 0.07)
        self._set_likelihood("sleeping", "reply_latency", "very_slow", 0.20)
        self._set_likelihood("sleeping", "reply_latency", "none", 0.70)

        self._set_likelihood("away", "reply_latency", "fast", 0.05)
        self._set_likelihood("away", "reply_latency", "normal", 0.10)
        self._set_likelihood("away", "reply_latency", "slow", 0.30)
        self._set_likelihood("away", "reply_latency", "very_slow", 0.35)
        self._set_likelihood("away", "reply_latency", "none", 0.20)

        self._set_likelihood("needs_care", "reply_latency", "fast", 0.10)
        self._set_likelihood("needs_care", "reply_latency", "normal", 0.15)
        self._set_likelihood("needs_care", "reply_latency", "slow", 0.25)
        self._set_likelihood("needs_care", "reply_latency", "very_slow", 0.30)
        self._set_likelihood("needs_care", "reply_latency", "none", 0.20)

        # msg_length 分布: P(length_category | state)
        self._set_likelihood("chatting", "msg_length", "short", 0.50)
        self._set_likelihood("chatting", "msg_length", "medium", 0.35)
        self._set_likelihood("chatting", "msg_length", "long", 0.10)
        self._set_likelihood("chatting", "msg_length", "none", 0.05)

        self._set_likelihood("browsing", "msg_length", "short", 0.30)
        self._set_likelihood("browsing", "msg_length", "medium", 0.40)
        self._set_likelihood("browsing", "msg_length", "long", 0.25)
        self._set_likelihood("browsing", "msg_length", "none", 0.05)

        self._set_likelihood("busy", "msg_length", "short", 0.60)
        self._set_likelihood("busy", "msg_length", "medium", 0.25)
        self._set_likelihood("busy", "msg_length", "long", 0.05)
        self._set_likelihood("busy", "msg_length", "none", 0.10)

        self._set_likelihood("sleeping", "msg_length", "short", 0.10)
        self._set_likelihood("sleeping", "msg_length", "medium", 0.05)
        self._set_likelihood("sleeping", "msg_length", "long", 0.02)
        self._set_likelihood("sleeping", "msg_length", "none", 0.83)

        self._set_likelihood("away", "msg_length", "short", 0.20)
        self._set_likelihood("away", "msg_length", "medium", 0.15)
        self._set_likelihood("away", "msg_length", "long", 0.05)
        self._set_likelihood("away", "msg_length", "none", 0.60)

        self._set_likelihood("needs_care", "msg_length", "short", 0.25)
        self._set_likelihood("needs_care", "msg_length", "medium", 0.30)
        self._set_likelihood("needs_care", "msg_length", "long", 0.30)
        self._set_likelihood("needs_care", "msg_length", "none", 0.15)

        # silence_hours 分布: P(silence_category | state)
        self._set_likelihood("chatting", "silence", "active", 0.70)
        self._set_likelihood("chatting", "silence", "recent", 0.20)
        self._set_likelihood("chatting", "silence", "moderate", 0.07)
        self._set_likelihood("chatting", "silence", "long", 0.03)

        self._set_likelihood("browsing", "silence", "active", 0.40)
        self._set_likelihood("browsing", "silence", "recent", 0.35)
        self._set_likelihood("browsing", "silence", "moderate", 0.20)
        self._set_likelihood("browsing", "silence", "long", 0.05)

        self._set_likelihood("busy", "silence", "active", 0.10)
        self._set_likelihood("busy", "silence", "recent", 0.25)
        self._set_likelihood("busy", "silence", "moderate", 0.45)
        self._set_likelihood("busy", "silence", "long", 0.20)

        self._set_likelihood("sleeping", "silence", "active", 0.01)
        self._set_likelihood("sleeping", "silence", "recent", 0.04)
        self._set_likelihood("sleeping", "silence", "moderate", 0.25)
        self._set_likelihood("sleeping", "silence", "long", 0.70)

        self._set_likelihood("away", "silence", "active", 0.05)
        self._set_likelihood("away", "silence", "recent", 0.15)
        self._set_likelihood("away", "silence", "moderate", 0.50)
        self._set_likelihood("away", "silence", "long", 0.30)

        self._set_likelihood("needs_care", "silence", "active", 0.15)
        self._set_likelihood("needs_care", "silence", "recent", 0.20)
        self._set_likelihood("needs_care", "silence", "moderate", 0.35)
        self._set_likelihood("needs_care", "silence", "long", 0.30)

    def _set_likelihood(self, state: str, obs_key: str, obs_value: str, prob: float):
        """设置似然值。"""
        self._likelihood_cache[(state, obs_key, obs_value)] = prob

    def _get_likelihood(self, state: str, obs_key: str, obs_value: str) -> float:
        """获取似然值。缺失时返回均匀分布值。"""
        key = (state, obs_key, obs_value)
        if key in self._likelihood_cache:
            return self._likelihood_cache[key]
        return 0.05  # 小概率兜底

    # ── 先验概率 ──────────────────────────────────────────

    def _time_based_prior(self, now: datetime) -> dict[str, float]:
        """
        基于时间的先验概率 P(state)。
        不同时段各状态的基础概率不同。
        """
        h = now.hour
        wd = now.weekday()  # 0=Mon, 6=Sun
        is_weekend = wd >= 5

        if 0 <= h < 7:
            # 深夜：sleeping 先验极高
            return {
                "chatting": 0.02, "browsing": 0.03, "busy": 0.02,
                "sleeping": 0.85, "away": 0.05, "needs_care": 0.03,
            }
        elif 7 <= h < 9:
            # 早晨：刚醒/通勤
            if is_weekend:
                return {
                    "chatting": 0.10, "browsing": 0.30, "busy": 0.05,
                    "sleeping": 0.35, "away": 0.15, "needs_care": 0.05,
                }
            return {
                "chatting": 0.10, "browsing": 0.25, "busy": 0.35,
                "sleeping": 0.10, "away": 0.15, "needs_care": 0.05,
            }
        elif 9 <= h < 12:
            # 上午：忙碌/工作
            if is_weekend:
                return {
                    "chatting": 0.20, "browsing": 0.40, "busy": 0.10,
                    "sleeping": 0.10, "away": 0.15, "needs_care": 0.05,
                }
            return {
                "chatting": 0.15, "browsing": 0.25, "busy": 0.40,
                "sleeping": 0.02, "away": 0.13, "needs_care": 0.05,
            }
        elif 12 <= h < 14:
            # 午休
            return {
                "chatting": 0.25, "browsing": 0.35, "busy": 0.15,
                "sleeping": 0.05, "away": 0.15, "needs_care": 0.05,
            }
        elif 14 <= h < 18:
            # 下午
            if is_weekend:
                return {
                    "chatting": 0.20, "browsing": 0.40, "busy": 0.10,
                    "sleeping": 0.03, "away": 0.22, "needs_care": 0.05,
                }
            return {
                "chatting": 0.15, "browsing": 0.25, "busy": 0.40,
                "sleeping": 0.02, "away": 0.13, "needs_care": 0.05,
            }
        elif 18 <= h < 22:
            # 晚上：自由时间
            return {
                "chatting": 0.30, "browsing": 0.35, "busy": 0.10,
                "sleeping": 0.01, "away": 0.19, "needs_care": 0.05,
            }
        else:  # 22-24
            # 深夜前：准备睡觉/刷手机
            return {
                "chatting": 0.15, "browsing": 0.30, "busy": 0.05,
                "sleeping": 0.35, "away": 0.10, "needs_care": 0.05,
            }

    # ── 信号分类 ──────────────────────────────────────────

    @staticmethod
    def classify_latency(latency_hours: float | None) -> str:
        """回复延迟 → 分类。"""
        if latency_hours is None:
            return "none"
        if latency_hours <= 0.08:      # ≤5 分钟
            return "fast"
        if latency_hours <= 1.0:        # ≤1 小时
            return "normal"
        if latency_hours <= 6.0:        # ≤6 小时
            return "slow"
        return "very_slow"

    @staticmethod
    def classify_msg_length(length: int | None) -> str:
        """消息长度 → 分类。"""
        if length is None or length <= 0:
            return "none"
        if length <= 5:
            return "short"
        if length <= 30:
            return "medium"
        return "long"

    @staticmethod
    def classify_silence(hours: float | None) -> str:
        """沉默时长 → 分类。"""
        if hours is None:
            return "none"
        if hours <= 1:
            return "active"
        if hours <= 6:
            return "recent"
        if hours <= 24:
            return "moderate"
        return "long"

    # ── 核心推断 ──────────────────────────────────────────

    def _transition_prior(self, prev_posterior: dict) -> dict:
        """A1: 前向滤波先验 = prev_posterior × TRANSITIONS（矩阵向量乘）。

        config 支持逐行覆盖：`transition_<state>` 键为 dict 时以该行替换默认行，
        覆盖后行内归一化（防配置配错导致行和 ≠ 1）。返回归一化后的预测分布。
        """
        trans: dict[str, dict] = {}
        for s in self.STATES:
            row = dict(self.TRANSITIONS.get(s, {}))
            override = self.config.get(f"transition_{s}")
            if isinstance(override, dict):
                # 配置行 = 整行覆盖（含 0 值），防漏配状态被默认保持概率吞掉
                row = {}
                for k, v in override.items():
                    if k not in self.STATES:
                        continue
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue  # 非法数值忽略，缺省 0
                    if not math.isfinite(fv) or fv < 0:
                        continue  # NaN/inf/负数忽略：防 NaN 传播进 posterior → JSONL 非法 token
                    row[k] = fv
            total = sum(row.values())
            if total > 0:
                row = {k: v / total for k, v in row.items()}
            trans[s] = row
        prior = {}
        for next_state in self.STATES:
            prior[next_state] = sum(
                prev_posterior.get(s, 0.0) * trans[s].get(next_state, 0.0)
                for s in self.STATES
            )
        total = sum(prior.values())
        if total > 0:
            for s in prior:
                prior[s] /= total
        return prior

    def infer(self, observations: dict, now: datetime = None,
              prev_posterior: dict | None = None) -> dict:
        """
        Bayesian 推断用户当前状态。

        observations:
        - reply_latency: float|None  (最近回复延迟，小时)
        - msg_length: int|None       (最近消息长度，字符)
        - silence_hours: float       (自上次用户消息以来的沉默时长)
        - in_class: bool            (是否在上课)
        - is_weekend: bool          (是否周末)

        prev_posterior: 上一 tick 后验（dict|None，默认取自身持久化的 prev_posterior）。
        配合 [bayesian].transition_enabled 走前向滤波（马尔可夫转移先验）；否则回退
        纯时间先验（恒等，A1 默认关闭）。返回的 prev_posterior 字段为当前后验，
        供调用方落盘下一 tick 用。

        返回:
        {
            "posterior": {state: prob, ...},
            "most_likely": str,
            "confidence": float (最可能状态的概率),
            "utility": float (加权发送效用),
            "should_send_bayesian": bool (仅基于 Bayesian 的建议),
            "state_description": str (人类可读),
            "entropy": float (后验熵，bits；A1 新增，不确定性度量),
            "prev_posterior": {state: prob, ...} (A1 新增，当前后验透传落盘),
        }
        """
        if now is None:
            now = datetime.now(CST)

        if prev_posterior is None:
            prev_posterior = self.prev_posterior

        # ── A1: 前向滤波先验 ──
        # transition_enabled 且存在上一后验时：先验 = 0.5×转移先验 + 0.5×时间先验
        # （线性混合，最简单合理）。转移先验利用状态持续性平滑（上一 tick 后验 → 当前
        # 先验）；时间先验兜底——长沉默/跨时段场景下防陈旧后验完全主导时段分布。
        if self.config.get("transition_enabled", False) and prev_posterior:
            trans_prior = self._transition_prior(prev_posterior)
            time_prior = self._time_based_prior(now)
            prior = {
                s: 0.5 * trans_prior.get(s, 0.05) + 0.5 * time_prior.get(s, 0.05)
                for s in self.STATES
            }
            total = sum(prior.values())
            if total > 0:
                prior = {s: v / total for s, v in prior.items()}
        else:
            prior = self._time_based_prior(now)

        # 提取信号分类
        latency_cat = self.classify_latency(observations.get("reply_latency"))
        length_cat = self.classify_msg_length(observations.get("msg_length"))
        silence_cat = self.classify_silence(observations.get("silence_hours", 0))

        # 上课/周末修正
        in_class = observations.get("in_class", False)
        is_weekend = observations.get("is_weekend", now.weekday() >= 5)

        # ── 计算后验：P(state | obs) ∝ P(state) × Π P(obs_i | state) ──
        posterior = {}
        for state in self.STATES:
            post = prior.get(state, 0.05)

            # 似然乘子
            post *= self._get_likelihood(state, "reply_latency", latency_cat)
            post *= self._get_likelihood(state, "msg_length", length_cat)
            post *= self._get_likelihood(state, "silence", silence_cat)

            # 上课/周末先验修正
            if in_class and state == "busy":
                post *= 2.0  # 上课→忙碌概率大增
            elif in_class and state == "chatting":
                post *= 0.2  # 上课→不可能在聊天
            if is_weekend and state == "busy":
                post *= 0.7  # 周末→忙碌概率降低
            if is_weekend and state == "sleeping":
                post *= 1.5  # 周末→睡觉概率升高

            posterior[state] = post

        # 归一化
        total = sum(posterior.values())
        if total > 0:
            for state in posterior:
                posterior[state] /= total

        # 最可能状态
        most_likely = max(posterior, key=posterior.get)
        confidence = posterior[most_likely]

        # 加权发送效用
        utility = sum(posterior[s] * self.UTILITY[s] for s in self.STATES)

        # ── A1: 当前后验持久化（下一 tick 前向滤波用）+ 后验熵（bits）──
        # 仅 A1 启用（transition_enabled 或 info_gain_threshold>0）时产出熵/透传后验并
        # 更新持久化后验——默认关闭恒等：决策日志不新增字段、状态不落盘 prev_posterior。
        try:
            ig_thr = float(self.config.get("info_gain_threshold", 0.0) or 0.0)
        except (TypeError, ValueError):
            ig_thr = 0.0  # 非法阈值 → 视为关闭（防 infer 崩溃）
        a1_active = (self.config.get("transition_enabled", False) or ig_thr > 0)
        out = {
            "posterior": {s: round(posterior[s], 4) for s in self.STATES},
            "most_likely": most_likely,
            "confidence": round(confidence, 4),
            "utility": round(utility, 4),
            "should_send_bayesian": utility >= self.utility_threshold,
            "state_description": self.STATE_DESCRIPTIONS[most_likely],
        }
        if a1_active:
            self.prev_posterior = {s: float(posterior[s]) for s in self.STATES}
            entropy = -sum(p * math.log2(p) for p in posterior.values() if p > 0)
            out["entropy"] = round(entropy, 4)
            out["prev_posterior"] = {s: round(posterior[s], 4) for s in self.STATES}
        else:
            self.prev_posterior = None
        return out

    def record_observation(self, observations: dict, actual_state: str = None):
        """记录观察用于在线学习。
        actual_state: 已知真实状态（用户明确说了）时触发监督学习。"""
        if actual_state:
            self.learner.update_from_label(observations, actual_state)

    # ── 持久化（在线学习 EMA 调优跨进程保留）──────────────

    def to_state_dict(self) -> dict:
        """序列化似然缓存供状态落盘（tuple key → "state.obs_key.obs_value"）。
        供 ChiguoState.save 写入；含默认值+本次进程学习增量，还原时叠加于默认之上。
        A1: 附加 _prev_posterior（前向滤波上一后验，随状态跨进程保留）。"""
        sd = {
            f"{s}.{ok}.{ov}": float(v)
            for (s, ok, ov), v in self._likelihood_cache.items()
        }
        # A1: 仅 transition_enabled 时 _prev_posterior 随状态跨进程保留（默认关闭恒等）
        if self.config.get("transition_enabled", False) and self.prev_posterior:
            sd["_prev_posterior"] = {
                k: float(v) for k, v in self.prev_posterior.items() if k in self.STATES
            }
        return sd

    def restore_state_dict(self, data: dict) -> None:
        """从持久化数据还原似然缓存。坏键（非 3 段 key / 非法状态 / 非法观测维度或取值 /
        非数值 / 非有限值如 NaN）丢弃，缺键保留默认值 → 还原幂等、不破坏默认参数表。
        A1: _prev_posterior（dict → 归一化 float 值域校验）还原到 prev_posterior，
        坏数据静默丢弃（缺省 None → 走纯时间先验，恒等）。"""
        if not isinstance(data, dict):
            return
        for k, v in data.items():
            parts = str(k).split(".")
            if len(parts) != 3 or parts[0] not in self.STATES:
                continue
            if parts[1] not in self.OBS_SPACE or parts[2] not in self.OBS_SPACE[parts[1]]:
                continue
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(val):
                continue
            if not (0.0 <= val <= 1.0):
                continue
            self._likelihood_cache[(parts[0], parts[1], parts[2])] = val
        pp = data.get("_prev_posterior")
        if isinstance(pp, dict):
            cleaned = {}
            for k, v in pp.items():
                if k not in self.STATES:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(fv) and fv >= 0:
                    cleaned[k] = fv
            total = sum(cleaned.values())
            if total > 0:
                self.prev_posterior = {k: v / total for k, v in cleaned.items()}


class BayesianLearner:
    """
    在线学习器。
    从历史观察中用指数移动平均调整似然参数。

    参考：revive-companion 的 bayesian/learner.py
    """

    def __init__(self, estimator: UserStateEstimator, learning_rate: float = 0.05):
        self.estimator = estimator
        self.lr = learning_rate  # 学习率，越小学得越慢

    def update_from_label(self, observations: dict, true_state: str):
        """
        监督学习：已知真实状态，更新 P(obs | state)。
        EMA: new_param = old_param * (1 - lr) + 1.0 * lr (观测到)
        """
        if true_state not in self.estimator.STATES:
            return

        obs_map = {
            "reply_latency": self.estimator.classify_latency(
                observations.get("reply_latency")
            ),
            "msg_length": self.estimator.classify_msg_length(
                observations.get("msg_length")
            ),
            "silence": self.estimator.classify_silence(
                observations.get("silence_hours", 0)
            ),
        }

        for obs_key, obs_value in obs_map.items():
            key = (true_state, obs_key, obs_value)
            old = self.estimator._likelihood_cache.get(key, 0.05)
            # EMA：强化观察到的，弱化未观察到的
            new_val = old * (1 - self.lr) + 1.0 * self.lr
            self.estimator._likelihood_cache[key] = min(0.99, new_val)

            # 对同一 obs_key 的其他 value，稍微衰减
            all_values = set()
            for (s, ok, ov) in self.estimator._likelihood_cache:
                if s == true_state and ok == obs_key:
                    all_values.add(ov)
            for ov in all_values:
                if ov != obs_value:
                    k2 = (true_state, obs_key, ov)
                    old2 = self.estimator._likelihood_cache.get(k2, 0.05)
                    self.estimator._likelihood_cache[k2] = max(0.01, old2 * (1 - self.lr * 0.5))

            # Normalize: re-normalize all (state, obs_key) values to sum to 1
            group_sum = sum(
                self.estimator._likelihood_cache.get((true_state, obs_key, ov), 0.05)
                for ov in all_values
            )
            if group_sum > 0:
                for ov in all_values:
                    k2 = (true_state, obs_key, ov)
                    old_val = self.estimator._likelihood_cache.get(k2, 0.05)
                    self.estimator._likelihood_cache[k2] = old_val / group_sum  # ponytail: no clamp, let sum=1.0 naturally

