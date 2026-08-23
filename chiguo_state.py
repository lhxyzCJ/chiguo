
import json
import os
import logging
import random
import re
import math
import hashlib
import shutil
import sys
import types
import time as time_module
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from pathlib import Path
from contextlib import contextmanager

from chiguo_math import (
    sigmoid, decay, elastic_recover,
    dynamic_lambda, hawkes_intensity, longing_decay,
    in_quiet_window,
    apply_interaction_matrix, drop_damp, impact_inertia,
    user_mood_impact, MOOD_DELTA, ou_step, noise_cap, baseline_shift_of,
    mood_fresh,
)
from chiguo_personality import (
    PersonalityTraits, PersonalityDelta, PersonalityDeltas,
    personality_to_dict, personality_from_dict,
)
from memory import create_backend
from chiguo_circadian import CircadianTracker, bucket_for
from datetime import date as date_type
from chiguo_time import CST
import chiguo_locks as locks
from chiguo_atomic import atomic_write
from trigger_types import TriggerType

from chiguo_state_models import (  # noqa: F401 — re-export for compat
    ChiguoEmotion,
    CooldownState,
    BASELINE_DEFAULTS,
    EVENT_DELTA,
    EVENT_TYPE_SYNONYMS,
    emotion_tag_snapshot,
    REFUND_FIFO_MAX,
    _MEMORY_MARKER_KEYS,
    _coerce_dataclass_fields,
    _memory_dedup_key,
)
from chiguo_pending import (  # T10 补充：pending 纯逻辑薄包装
    pending_add,
    pending_resolve,
    pending_mark_attempted,
    pending_prune,
)

from state.ownership import _OWNER_PLACEHOLDER, _config_owner, _is_placeholder_owner, _check_owner_mismatch

from state.persistence import StatePersistence

from state.schedule import ScheduleMixin
from state.emotion import EmotionMixin
from state.interaction import InteractionMixin

class ChiguoState(ScheduleMixin, EmotionMixin, InteractionMixin):
    """迟菓全局状态管理 v2"""

    @staticmethod
    def _build_personality(config: dict) -> PersonalityTraits:
        """从 config 构造初始人格（Big Five + 角色维度）。

        __init__ 与热重载 _reapply_personality_config 共用单一构造点，
        防人格字段/默认值后续变更时双点失同步。
        """
        pers_cfg = config.get("personality", {})
        emo_cfg = config.get("emotion", {})
        return PersonalityTraits(
            openness=pers_cfg.get("openness", 55.0),
            conscientiousness=pers_cfg.get("conscientiousness", 65.0),
            extraversion=pers_cfg.get("extraversion", 60.0),
            agreeableness=pers_cfg.get("agreeableness", 65.0),
            neuroticism=pers_cfg.get("neuroticism", 60.0),
            tsundere_intensity=pers_cfg.get("tsundere_intensity",
                emo_cfg.get("tsundere_index", 75.0)),
            playfulness=pers_cfg.get("playfulness", 55.0),
            attachment_style=pers_cfg.get("attachment_style", 60.0),
        )

    def __init__(self, config: dict):
        self.config = config
        self._persistence = StatePersistence(config, self)
        emo_cfg = config.get("emotion", {})
        self.emotion = ChiguoEmotion(
            loneliness=emo_cfg.get("loneliness", 15.0),
            affection=emo_cfg.get("affection", 55.0),
            anxiety=emo_cfg.get("anxiety", 40.0),
            energy=emo_cfg.get("energy", 85.0),
        )
        self.cooldown = CooldownState()
        self.circadian = CircadianTracker()
        self._apply_quiet_window()
        self.memories: list[dict] = []
        self._memory_dedup: dict[str, str] = {}
        self.pending_topics: list[dict] = []
        self.tick_seq: int = 0  # v5: 单调递增 tick 计数器，用于检测遗漏
        self._state_owner: str | None = _config_owner(config)

        self.personality = self._build_personality(config)
        self._personality_initial_baseline = dict(self.personality._baseline)
        self.personality_history: list[dict] = []

        self._bayesian_estimator = None
        self._bayesian_restored: dict | None = None

        sched = config.get("schedule", {})
        xlsx_path = sched.get("xlsx_path", "data/xskb.xlsx")
        sem_start_str = sched.get("semester_start", "")
        sem_end_str = sched.get("semester_end", "")
        try:
            sem_start = date_type.fromisoformat(sem_start_str)
        except (ValueError, TypeError):
            sem_start = date_type(2026, 2, 23)
            print(f"[warn] [schedule].semester_start 缺失/非法（{sem_start_str!r}），回退默认 {sem_start}；请更新 chiguo_proactive.toml",
                  file=sys.stderr)
        self.semester_start = sem_start
        self.semester_end = None
        if sem_end_str:
            try:
                self.semester_end = date_type.fromisoformat(sem_end_str)
            except (ValueError, TypeError):
                pass
        # AUD-009: schedule helpers lazy-imported (no top-level from schedule.*)
        from schedule.parser import refresh_schedule_cache as _refresh
        refresh_schedule_cache = _refresh  # local alias for call below
        refresh_schedule_cache(
            str(self._anchored(xlsx_path)),
            str(self._anchored("schedule_cache.json")),
            semester_start=sem_start,
            enabled=bool(sched.get("enabled", True)),
        )

        from schedule.holiday import HolidayParser as _HP
        try:
            self.holiday_parser = _HP(
                data_path=str(self._anchored("holidays.json"))
            )
        except Exception as exc:
            print(f"[warn] HolidayParser 构造失败，节假日判断降级: {exc}", file=sys.stderr)
            self.holiday_parser = None

        base_dir = str(self._anchored("."))
        mem_cfg = config.get("memory", {})
        self.memory_bridge = create_backend(mem_cfg, base_dir=base_dir)
        from schedule.anniversary import AnniversaryManager as _AM
        from schedule.override_store import OverrideStore as _OS
        from schedule.plan_store import PlanStore as _PS
        self.anniversary_mgr = _AM(base_dir)
        self.override_store = _OS(base_dir)
        self.plan_store = _PS(base_dir)
        self._rc_cache: dict = {}   # {date_str: resolved_classes}(availability/schedule_status 共享)
        self._scale_cache: dict = {}   # {date_str: trigger_scale}(计划修饰参数,每 tick 按日期缓存)

        self.mono_anchor: float | None = None
        self.wall_anchor: str | None = None

        self._load()

    def _anchored(self, *parts: str) -> Path:
        """v6: 路径锚定（委托到持久化单类）。运行时文件基于 _base_dir 解析。"""
        return self._persistence.anchored(*parts)

    def _apply_quiet_window(self):
        """v6: 从 config [schedule] 注入睡眠窗口到 cooldown（替代硬编码 0-8）。"""
        s = self.config.get("schedule", {})
        self.cooldown.set_quiet_window(
            s.get("quiet_start", 0), s.get("quiet_end", 8),
        )

    def _current_bucket(self, now: datetime) -> str:
        """v8: 按当前时刻判定作息桶（weekday/weekend），配合假日/调休。"""
        if self.holiday_parser is None:   # 构造失败降级 → 纯周几启发式
            return "weekday" if now.weekday() < 5 else "weekend"
        return bucket_for(now, self.holiday_parser.is_holiday,
                          self.holiday_parser.is_makeup_workday)

    def _sync_quiet_window(self, now: datetime | None = None):
        """v8: 按当前时刻分桶选窗口;置信度达标 → 学习窗口,否则回退配置默认。
        兼容字段(quiet_start/end/confidence)同步为当前生效桶快照,门禁经 quiet_window() 读取不变。
        类型漂移防护:桶字段可能为字符串(手改/旧数据)→ 强转,失败回退默认 (0,8,0.0)。"""
        if now is None:
            now = datetime.now(CST)
        cfg = self.config.get("circadian", {})
        start, end, conf = self.circadian.bucket_window(self._current_bucket(now))
        try:
            start, end, conf = int(start), int(end), float(conf)
        except (ValueError, TypeError):
            start, end, conf = 0, 8, 0.0
        if not (0 <= start <= 23 and 0 <= end <= 23):
            start, end = 0, 8
        self.circadian.set_active_bucket(self._current_bucket(now), start, end, conf)
        if conf >= cfg.get("min_confidence", 0.5):
            self.cooldown.set_quiet_window(start, end)
        else:
            self._apply_quiet_window()

    def sync_quiet_window(self, now: datetime | None = None):
        """T11·Q1 公开 API：同步当前生效睡眠窗口（daemon 等外部经此调用，不直触私有）。"""
        self._sync_quiet_window(now)

    def _relearn_windows(self, now: datetime):
        """单源：重算生物钟学习窗口并同步门禁。reply/active 记账由各自调用方先行。

        Q30「circadian 双源」收敛——recompute + _sync_quiet_window 曾在
        on_user_message（回复）与 daemon._apply_play_proof（听歌活跃）两处重复，
        且 [circadian] 4 参数默认值被复制两遍。一律经此门面重算+同步，
        [circadian] 参数默认只在此维护一份（行为不变）。
        T11 协调：同步经公开 sync_quiet_window（避免双 API 并存）。"""
        cfg = self.config.get("circadian", {})
        self.circadian.recompute(
            min_sample_days=cfg.get("min_sample_days", 7),
            history_days=cfg.get("history_days", 14),
            min_width=cfg.get("min_width", 5),
            max_width=cfg.get("max_width", 12),
        )
        self.sync_quiet_window(now)

    def reload_config(self, new_config: dict):
        """热重载：替换 config 引用并重应用 config 派生组件（--loop 模式用）。

        补全热重载重建集合（Q19）：personality 初始基线 / holiday_parser 随新 config
        重建；cooldown 静默窗口经 _sync_quiet_window 重建（置信度达标用学习窗口,否则
        回退新 config [schedule] 默认）。

        调用方契约（防误用）：此方法重应用的是 config 驱动初始部分，live personality
        会被切到新 config 值；真实 evaluate 流中调用方须在随后执行 _load() 以状态文件
        为准覆盖（带持久化人格演变的状态 → 演变保留）。本方法的持久价值在于
        _personality_initial_baseline（回归目标）与 holiday_parser 随 config 刷新。
        """
        self.config = new_config
        self._reapply_personality_config()
        self._reapply_holiday_parser()
        self._sync_quiet_window()
        sched = new_config.get("schedule", {})
        try:
            self.semester_start = date_type.fromisoformat(sched.get("semester_start", ""))
        except (ValueError, TypeError):
            pass
        self.semester_end = None
        se_str = sched.get("semester_end", "")
        if se_str:
            try:
                self.semester_end = date_type.fromisoformat(se_str)
            except (ValueError, TypeError):
                pass
        self._rc_cache = {}
        self._scale_cache = {}

    def _reapply_personality_config(self):
        """按新 config [personality] 重建人格初始值与初始基线（回归目标）。
        构造统一走 _build_personality（与 __init__ 单一构造点）。
        注意：此处重建的是 config 驱动初始值；带持久化人格演变的运行时值，
        由调用方在随后执行 _load() 以状态文件为准覆盖。"""
        self.personality = self._build_personality(self.config)
        self._personality_initial_baseline = dict(self.personality._baseline)

    def _reapply_holiday_parser(self):
        """按 base_dir 下的 holidays.json 重启 holiday_parser（可能运行时已更新）。"""
        try:
            self.holiday_parser = HolidayParser(
                data_path=str(self._anchored("holidays.json"))
            )
        except Exception as exc:
            print(f"[warn] HolidayParser 构造失败，节假日判断降级: {exc}", file=sys.stderr)
            self.holiday_parser = None

    @property
    def state_path(self) -> Path:
        return self._persistence.state_path

    @property
    def memories_path(self) -> Path:
        return self._persistence.memories_path

    def _load(self):
        """私有加载（测试白盒沿用），委托持久化单类。"""
        self._persistence.load()

    def load(self):
        """公开加载（T11·Q1：daemon 等外部走公开 API，不强闯私有）。"""
        self._persistence.load()

    def _apply_memory_dedup(self):
        """把本进程已持久化的 reminder 去重标记（self._memory_dedup）回写到
        self.memories 对应条目上，使跨进程（cron 每 15 分钟新进程）不再重复触发。
        内容键匹配：记忆文件仍是内容唯一事实源，此处仅补回 last_triggered_at。"""
        if not self._memory_dedup:
            return
        for mem in self.memories:
            if not isinstance(mem, dict):
                continue
            key = _memory_dedup_key(mem)
            marked = self._memory_dedup.get(key)
            if marked:
                mem["last_triggered_at"] = marked

    def mark_memory_triggered(self, mem: dict, now: datetime | None = None):
        """公开 API：标记一条 reminder 记忆已触发（写 last_triggered_at）。与
        self.memories 共享对象引用，save() 扫描自会落盘 memory_dedup 字段；
        跨进程（cron）由该字段读回后经 _apply_memory_dedup 防重复触发。

        `mem` 必须是 self.memories 列表内的 dict（trigger 层 data['memory'] 持有的
        正是同一对象引用，原地标记即时对 evaluate 子路径生效）。"""
        if not isinstance(mem, dict):
            return
        if now is None:
            now = datetime.now(CST)
        mem["last_triggered_at"] = now.isoformat()

    def attach_memory_marker_to_event(self, msg_id: str, mem: dict):
        """F-A5-01（#314 R9）：把一条 reminder 记忆的内容键记到对应在途 Hawkes
        事件上（若该 msg_id 在事件列表内）。供发送失败 refund_send 回滚定位——
        否则跨进程（cron：evaluate 在 A 进程标记、--send-result 在 B 进程退款）
        无法知道该 msg_id 对应哪条 reminder，失败后 last_triggered_at 无从回滚、
        reminder 永久不再触发。事件随 cooldown 落盘，契约键名 memory_marker。"""
        if not isinstance(mem, dict) or not msg_id:
            return
        key = _memory_dedup_key(mem)
        for ev in self.cooldown.event_timestamps:
            if isinstance(ev, dict) and ev.get("msg_id") == msg_id:
                ev["memory_marker"] = key
                break

    def _unmark_memory_by_key(self, key: str):
        """F-A5-01：按记忆内容键清除 reminder 触发标记（last_triggered_at +
        去重缓存），供 refund_send 发送失败回滚。key 由 Hawkes 事件 memory_marker
        携带。无该键 → no-op（非 reminder 事件/旧事件）。"""
        if not key:
            return
        self._memory_dedup.pop(key, None)
        for mem in self.memories:
            if isinstance(mem, dict) and _memory_dedup_key(mem) == key:
                mem.pop("last_triggered_at", None)
                break

    def _migrate_personality_baseline(self, data: dict):
        """v10 迁移：恢复持久化人格基线（回归目标）；旧状态无持久化基线 →
        回退到 toml 构造函数初始基线（_personality_initial_baseline）。

        等价前提：原实现把该恢复放在 `if pers_data:` 分支内（仅当 state 有
        personality 时）；此处无条件执行 —— 因为加载路径总是先经
        _apply_loaded_data 构造 `self.personality`（有 pers_data 用
        personality_from_dict，无则用 toml 初始值），且 _personality_initial_baseline
        恒记录构造值，无人中途改写 _baseline，故与原行为严格等价。

        边界分支（save 不可达，防御语义）：若 data 含 `personality_baseline`
        但完全无 `personality` 字段——原代码走 else 分支用 toml 构造
        tsundere，不会触发 reset（此处却无条件执行 reset_baseline）。当前
        _personality_initial_baseline 记录的就是 toml 构造值，因此即便该分支
        触发也退化为恒等，不改变任何结果；仅当未来有人改动加载时
        _personality_initial_baseline 的赋值源才产生语义差异，故显式注明。
        """
        saved_base = data.get("personality_baseline")
        if isinstance(saved_base, dict) and saved_base:
            self.personality.reset_baseline(saved_base)
        else:
            self.personality.reset_baseline(dict(self._personality_initial_baseline))

    def _audit(self, event: str, detail: str = ""):
        """私有审计（白盒测试沿用），委托持久化单类。"""
        self._persistence.audit(event, detail)

    def audit(self, event: str, detail: str = ""):
        """公开审计入口（T11·Q1：daemon 等外部走公开 API）。"""
        self._persistence.audit(event, detail)

    STATE_VERSION = 10  # v8: 双作息(circadian 分桶学习 + 迁移); v9: cooldown.recv_dedup; v10: personality_baseline + personality_history

    def _lock_acquire(self, lock_path: str) -> bool:
        return self._persistence.lock_acquire(lock_path)

    def _lock_release(self, lock_path: str):
        self._persistence.lock_release(lock_path)

    @contextmanager
    def state_lock(self):
        """持有 state 文件的跨进程独占锁（chiguo_state.json.lock），委托持久化单类。

        F-A16-01 (#309): 透传持久化单类的 acquired（本次是否真正获锁）。超时
        降级无锁时调用方可据此告警/预防 lost update。
        """
        with self._persistence.state_lock() as acquired:
            yield acquired

    def _in_lock(self) -> bool:
        """当前进程是否已持有 state 锁（供 daemon 判断重入场景）。"""
        return self._persistence.in_lock()

    def save(self, _backup: bool = True, _increment_tick: bool = True) -> bool:
        """原子写盘（委托 StatePersistence）：.tmp→os.replace + 备份 + fsync + 校验和。
        返回 bool（成功 True / 失败 False），失败不抛异常。"""
        return self._persistence.save(_backup=_backup, _increment_tick=_increment_tick)

    def monotonic_anchor(self) -> tuple[float | None, str | None]:
        """返回持久化的单调锚点对 (mono, wall)，缺失/损坏为 None。"""
        return self._persistence.monotonic_anchor_pair()

    @property
    def bayesian_estimator(self):
        """延迟导入 Bayesian 推断器，避免循环依赖。"""
        if self._bayesian_estimator is None:
            from chiguo_bayesian import UserStateEstimator
            self._bayesian_estimator = UserStateEstimator(
                self.config.get("bayesian", {})
            )
            if self._bayesian_restored:
                self._bayesian_estimator.restore_state_dict(self._bayesian_restored)
        return self._bayesian_estimator

    def reset_bayesian_estimator(self):
        """T11·Q1 公开 API：热重载时强制重建 Bayesian 推断器（清缓存，下次惰性重初始化）。"""
        self._bayesian_estimator = None

    def infer_user_state(self, now: datetime = None, msg_length: int = None) -> dict:
        """
        推断当前用户状态。融合 Bayesian 推断 + 课表/假期信息。
        从未交互过 → 返回默认中性状态（避免误判为 sleeping）。
        """
        if now is None:
            now = datetime.now(CST)

        silent_h = self.cooldown.silent_hours(now, wall=True)
        if silent_h > 720:  # 30 天从未交互
            default_posterior = {"chatting": 0.05, "browsing": 0.50, "busy": 0.10,
                                 "sleeping": 0.05, "away": 0.25, "needs_care": 0.05}
            result = {
                "posterior": dict(default_posterior),
                "most_likely": "browsing",
                "confidence": 0.50,
                "utility": 0.53,
                "should_send_bayesian": True,
                "state_description": "未知（从未交互）",
            }
            b_cfg = self.config.get("bayesian", {})
            try:
                ig_thr = float(b_cfg.get("info_gain_threshold", 0.0) or 0.0)
            except (TypeError, ValueError):
                ig_thr = 0.0  # 非法阈值 → 关闭（恒等，与常规 A3 门控对称）
            if b_cfg.get("transition_enabled", False) or ig_thr > 0:
                entropy = -sum(p * math.log2(p) for p in default_posterior.values() if p > 0)
                result["entropy"] = round(entropy, 4)
                result["prev_posterior"] = dict(default_posterior)
            return result

        last_latency = None
        if self.cooldown.reply_latencies:
            last_latency = self.cooldown.reply_latencies[-1]

        last_msg_len = None
        if self.cooldown.last_user_message_at:
            last_msg_len = msg_length if msg_length is not None else (
                self.cooldown.last_user_msg_length if self.cooldown.last_user_msg_length is not None else 10
            )

        in_class = False
        try:
            sch = self.schedule_status(now)
            in_class = bool(sch and sch.get("in_class"))
        except Exception:
            logging.debug("schedule_status 获取失败: %s", __import__('traceback').format_exc(), exc_info=False)

        observations = {
            "reply_latency": last_latency,
            "msg_length": last_msg_len,
            "silence_hours": self.cooldown.silent_hours(now, wall=True),
            "in_class": in_class,
            "is_weekend": now.weekday() >= 5,
        }

        result = self.bayesian_estimator.infer(observations, now)

        b_cfg = self.config.get("bayesian", {})
        try:
            ig_threshold = float(b_cfg.get("info_gain_threshold", 0.0) or 0.0)
        except (TypeError, ValueError):
            ig_threshold = 0.0  # 非法阈值 → 关闭（恒等，防 daemon 崩溃）
        if ig_threshold > 0 and result.get("entropy", 0.0) >= ig_threshold:
            try:
                ig_bonus = float(b_cfg.get("info_gain_utility_bonus", 0.1))
            except (TypeError, ValueError):
                ig_bonus = 0.1
            result["utility"] = round(result.get("utility", 0.0) + ig_bonus, 4)
            result["should_send_bayesian"] = True
            result["info_gain_boost"] = True
        return result

    def snapshot(self, now: datetime, user_state: dict = None) -> dict:
        sch = self.schedule_status(now)
        hq = self.holiday_parser.query(now) if self.holiday_parser else None

        if user_state is None:
            try:
                user_state = self.infer_user_state(now)
            except Exception:
                logging.debug("infer_user_state 失败: %s", __import__('traceback').format_exc(), exc_info=False)

        snap = {
            "emotion": asdict(self.emotion),
            "dominant_layer": self.emotion.dominant_layer,
            "neediness": round(self.emotion.neediness, 1),
            "poisson_lambda": round(self.current_lambda(now), 4),
            "availability": round(self.availability(now, user_state), 2),
            "holiday": {
                "is_holiday": bool(hq and hq["is_holiday"]),
                "name": (hq or {}).get("holiday_name"),
                "is_weekend": bool(hq and hq["is_weekend"]),
                "is_makeup_workday": bool(hq and hq["is_makeup_workday"]),
            },
            "schedule": {
                "in_class": sch["in_class"] if sch else None,
                "class_load": sch.get("class_load", "?") if sch else "no_data",
                "current_course": sch["current_course"]["course"] if (sch and sch.get("current_course")) else None,
                "remaining_classes": sch.get("remaining_classes", 0) if sch else 0,
                "holiday": sch.get("holiday") if sch else None,
                "makeup_day": sch.get("makeup_day", False) if sch else False,
                "on_break": sch.get("on_break", False) if sch else False,
                "break_reason": sch.get("break_reason") if sch else None,
                "breaks": sch.get("breaks", []) if sch else [],
            } if sch else None,
            "cooldown": {
                "messages_today": self.cooldown.messages_today,
                "messages_without_reply": self.cooldown.messages_without_reply,
                "silent_hours": round(self.cooldown.silent_hours(now), 1),
                "minutes_since_last": (lambda m: round(m, 1) if m is not None else None)(
                    self.cooldown.minutes_since_last_message(now)),
                "can_send": self.can_send(now),
            },
            "personality": {
                "profile": self.personality.dominant_profile(),
                "tsundere_intensity": round(self.personality.tsundere_intensity, 1),
                "extraversion": round(self.personality.extraversion, 1),
                "neuroticism": round(self.personality.neuroticism, 1),
                "agreeableness": round(self.personality.agreeableness, 1),
            },
            "user_state": user_state,
            "time": now.strftime("%Y-%m-%d %H:%M"),
        }
        from schedule.sources import load_sources
        from schedule.attention import build_attention
        src, _rc = self._resolved_for(now)
        if "attention" not in self._rc_cache:
            self._rc_cache["attention"] = build_attention(src, now.date())
        snap["attention"] = self._rc_cache["attention"]
        return snap
