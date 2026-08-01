# ============================================================
# chiguo_state.py — 迟菓情绪状态引擎 v8
# 数学驱动：Sigmoid 概率 + 半衰期衰减 + Hawkes 自激过程
# v4 新增：多维人格、Bayesian 用户状态推断、概率累积、EventBus
# v5 新增：状态备份(.bak)、fsync、tick_seq、损坏审计、tmp验证
# v6 新增：逃生阀、跨进程可重入锁、滑动崩溃窗口、配置化睡眠窗口、校验和强制回退
# v7 新增:生物钟学习(circadian) + 接话茬(pending_topics)
# v8 新增:双作息(circadian 分桶学习/迁移,STATE_VERSION 8)
# ============================================================

import json
import os
import hashlib
import shutil
import sys
import time as time_module
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from pathlib import Path
from contextlib import contextmanager

from chiguo_math import (
    sigmoid, decay, recover,
    dynamic_lambda, hawkes_intensity, longing_decay,
)
from chiguo_personality import (
    PersonalityTraits, PersonalityDelta, PersonalityDeltas,
    personality_to_dict, personality_from_dict,
)
from schedule_parser import ScheduleParser
from holiday_parser import HolidayParser
from memory_bridge import MemoryBridge
from chiguo_circadian import CircadianTracker, bucket_for
from datetime import date as date_type

CST = timezone(timedelta(hours=8))

# ── v6: 跨进程锁的模块级状态。lock_path → fd（持锁中）与重入深度。
# 同进程所有实例/调用共享同一 fd，避免对同一文件二次 open 造成 flock 自死锁。
_LOCK_FDS: dict[str, int] = {}
_LOCK_DEPTH: dict[str, int] = {}


@dataclass
class ChiguoEmotion:
    loneliness: float = 15.0
    affection: float = 55.0
    anxiety: float = 40.0
    energy: float = 85.0
    tsundere_index: float = 70.0
    loneliness_rate: float = 0.0    # Δloneliness/hour
    anxiety_rate: float = 0.0       # Δanxiety/hour

    @property
    def neediness(self) -> float:
        return self.loneliness * (1 - self.tsundere_index / 200) * (self.anxiety / 100)

    @property
    def dominant_layer(self) -> str:
        if self.anxiety > 70 or self.loneliness > 80:
            return "kernel"
        elif self.loneliness > 50:
            return "middle"
        else:
            return "shell"

    def clamp(self):
        self.loneliness = max(0, min(100, self.loneliness))
        self.affection = max(5, min(100, self.affection))
        self.anxiety = max(0, min(100, self.anxiety))
        self.energy = max(0, min(100, self.energy))
        self.tsundere_index = max(10, min(95, self.tsundere_index))


@dataclass
class CooldownState:
    last_message_at: str | None = None
    last_user_message_at: str | None = None
    messages_today: int = 0
    messages_without_reply: int = 0
    current_date: str = ""
    morning_sent: bool = False
    night_sent: bool = False
    trigger_history: list[str] = field(default_factory=list)  # 最近N次发送的触发类型，用于话题多样性
    event_timestamps: list[dict] = field(default_factory=list)  # [{"type":str,"time":str},...] Hawkes 用
    reply_latencies: list[float] = field(default_factory=list)  # 最近N次回复延迟（小时），用于参数校准
    busy_suppress_until: str | None = None  # 忙碌抑制截止时间 ISO
    held_count: int = 0  # v4: 连续抑制计数（概率累积用）
    accumulated_lambda: float = 0.0  # v4: 累积的 longing λ（概率累积机制）
    last_user_msg_length: int | None = None  # v4.2: 最近一次用户消息长度
    last_crash_at: str | None = None  # v4.1: 上次崩溃触发时间 ISO
    crash_count_48h: int = 0  # v4.1: 48h 内崩溃次数（用于安全阀降级）
    crash_timestamps: list[str] = field(default_factory=list)  # v6: 崩溃触发时间戳列表（滑动窗口统计）
    last_longing_break_at: str | None = None  # v6: 上次逃生阀破防时间 ISO（冷却用）

    def __post_init__(self):
        # v6: 睡眠窗口来源配置（非 dataclass 字段，不序列化）。ChiguoState 负责注入。
        self._quiet_start = 0
        self._quiet_end = 8

    def set_quiet_window(self, start: int, end: int):
        """v6: 注入睡眠窗口（来自 config [schedule] quiet_start/quiet_end）。"""
        self._quiet_start = int(start)
        self._quiet_end = int(end)

    def quiet_window(self) -> tuple[int, int]:
        """v7: 当前生效的静默窗口(start, end,含跨午夜语义)。
        来源:生物钟学习(置信度达标)或配置默认——由 _sync_quiet_window 决定。"""
        return self._quiet_start, self._quiet_end

    def silent_hours(self, now: datetime, wall: bool = False) -> float:
        """沉默时间（小时）。默认清醒沉默 = 墙钟 - 静默窗口（睡眠不算真沉默）；
        wall=True 返回墙钟沉默（不减睡眠窗口，Bayesian/分类器用）。
        时间戳缺失/不可解析 → 999.0（与"从未交互"语义一致），不崩溃。"""
        if not self.last_user_message_at:
            return 999.0
        try:
            last = datetime.fromisoformat(self.last_user_message_at)
        except (ValueError, TypeError):
            return 999.0
        if last.tzinfo is None:
            last = last.replace(tzinfo=CST)
        raw = (now - last).total_seconds() / 3600
        if wall:
            return max(0.0, raw)
        sleep_hours = self._sleep_hours_in_range(last, now)
        return max(0.0, raw - sleep_hours)

    # v6/v7: 睡眠窗口由 _sync_quiet_window 决定——生物钟学习置信度达标后
    # 用学习窗口覆盖 config [schedule] quiet_start/quiet_end 默认值。
    def _sleep_hours_in_range(self, start: datetime, end: datetime) -> float:
        """计算 [start, end] 区间内落在睡眠窗口（配置注入，默认 0-8）的小时数。"""
        qs, qe = self._quiet_start, self._quiet_end
        total = 0.0
        cur = start
        guard = 0  # 防御性上限：每轮至少推进一个窗口，4000 轮 ≈ 数千天，杜绝死循环
        while cur < end and guard < 4000:
            day = cur.replace(hour=0, minute=0, second=0, microsecond=0)
            ws = day.replace(hour=qs, minute=0, second=0, microsecond=0)
            we = day.replace(hour=qe, minute=0, second=0, microsecond=0)
            if qe < qs:
                we = we + timedelta(days=1)  # 跨午夜窗口（如 22:00-08:00）
            if we <= cur:
                # 当前时间已在窗口之后 → 跳到下一天窗口起点
                cur = ws + timedelta(days=1)
                guard += 1
                continue
            if ws < end and we > cur:
                overlap_start = max(cur, ws)
                overlap_end = min(end, we)
                total += (overlap_end - overlap_start).total_seconds() / 3600
            cur = we
            guard += 1
        return total

    def minutes_since_last_message(self, now: datetime) -> float | None:
        """距上次主动消息的分钟数。naive 时间戳补 CST；解析失败返回 None；
        未来时间戳返回 0（负值会被 can_send 误判为可发）。"""
        if not self.last_message_at:
            return 999.0
        try:
            last = datetime.fromisoformat(self.last_message_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=CST)
            delta = (now - last).total_seconds() / 60
            return max(0.0, delta)
        except (ValueError, TypeError):
            return None

    def is_busy_suppressed(self, now: datetime) -> bool:
        """检查当前是否处于忙碌抑制期。"""
        if not self.busy_suppress_until:
            return False
        try:
            until = datetime.fromisoformat(self.busy_suppress_until)
            return now < until
        except (ValueError, TypeError):
            return False


class ChiguoState:
    """迟菓全局状态管理 v2"""

    def __init__(self, config: dict):
        self.config = config
        emo_cfg = config.get("emotion", {})
        self.emotion = ChiguoEmotion(
            loneliness=emo_cfg.get("loneliness", 15.0),
            affection=emo_cfg.get("affection", 55.0),
            anxiety=emo_cfg.get("anxiety", 40.0),
            energy=emo_cfg.get("energy", 85.0),
        )
        self.cooldown = CooldownState()
        # ── v7: 生物钟学习器(作息数据 + 学习到的睡眠窗口)──
        self.circadian = CircadianTracker()
        self._apply_quiet_window()
        self.memories: list[dict] = []
        # ── v7: 待接续话题(接话茬)。[{topic, source, created_at, attempted}] ──
        self.pending_topics: list[dict] = []
        self.tick_seq: int = 0  # v5: 单调递增 tick 计数器，用于检测遗漏

        # ── v4: 多维人格 ──
        pers_cfg = config.get("personality", {})
        self.personality = PersonalityTraits(
            openness=pers_cfg.get("openness", 55.0),
            conscientiousness=pers_cfg.get("conscientiousness", 65.0),
            extraversion=pers_cfg.get("extraversion", 45.0),
            agreeableness=pers_cfg.get("agreeableness", 70.0),
            neuroticism=pers_cfg.get("neuroticism", 60.0),
            tsundere_intensity=pers_cfg.get("tsundere_intensity",
                emo_cfg.get("tsundere_index", 70.0)),
            playfulness=pers_cfg.get("playfulness", 55.0),
            attachment_style=pers_cfg.get("attachment_style", 60.0),
        )

        # ── v4: Bayesian 用户状态推断器（延迟初始化，避免循环导入）──
        self._bayesian_estimator = None

        # 课表解析器（v6.1: xlsx/cache 路径锚定 _base_dir，不依赖 cwd）
        sched = config.get("schedule", {})
        xlsx_path = sched.get("xlsx_path", "data/xskb.xlsx")
        sem_start_str = sched.get("semester_start", "2026-02-23")
        sem_end_str = sched.get("semester_end", "")
        try:
            sem_start = date_type.fromisoformat(sem_start_str)
        except (ValueError, TypeError):
            sem_start = date_type(2026, 2, 23)
        self.semester_start = sem_start
        self.semester_end = None
        if sem_end_str:
            try:
                self.semester_end = date_type.fromisoformat(sem_end_str)
            except (ValueError, TypeError):
                pass
        # 考试周范围
        self.exam_ranges: list[tuple[date_type, date_type]] = []
        for r in sched.get("exam_weeks", []) or []:
            parts = r.split(",")
            if len(parts) == 2:
                try:
                    s = date_type.fromisoformat(parts[0].strip())
                    e = date_type.fromisoformat(parts[1].strip())
                    self.exam_ranges.append((s, e))
                except (ValueError, TypeError):
                    pass
        self.schedule_parser = ScheduleParser(
            self._anchored(xlsx_path),
            cache_path=self._anchored("schedule_cache.json"),
            semester_start=sem_start,
        )

        # 节假日判断器（优先级高于课表）
        self.holiday_parser = HolidayParser(
            data_path=str(self._anchored("holidays.json"))
        )

        # 记忆桥接（只读 OpenClaw LanceDB）
        mem_cfg = config.get("memory", {})
        self.memory_bridge = MemoryBridge(
            db_path=mem_cfg.get("lancedb_path"),
            table_name=mem_cfg.get("lancedb_table", "memories"),
            strength=mem_cfg.get("ebbinghaus_strength"),
            min_weight=mem_cfg.get("ebbinghaus_min_weight"),
        )

        self._load()

    # ── v6: 路径锚定。运行时文件基于 _base_dir（config 所在目录）解析，
    # 不依赖 cwd —— 修复 cron 工作目录漂移导致的状态丢失/重建。

    def _anchored(self, *parts: str) -> Path:
        base = self.config.get("_base_dir", ".") or "."
        return Path(base) / Path(*parts)

    def _apply_quiet_window(self):
        """v6: 从 config [schedule] 注入睡眠窗口到 cooldown（替代硬编码 0-8）。"""
        s = self.config.get("schedule", {})
        self.cooldown.set_quiet_window(
            s.get("quiet_start", 0), s.get("quiet_end", 8),
        )

    def _current_bucket(self, now: datetime) -> str:
        """v8: 按当前时刻判定作息桶（weekday/weekend），配合假日/调休。"""
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
        self.circadian.set_active_bucket(self._current_bucket(now), start, end, conf)
        if conf >= cfg.get("min_confidence", 0.5):
            self.cooldown.set_quiet_window(start, end)
        else:
            self._apply_quiet_window()

    @property
    def state_path(self) -> Path:
        return self._anchored("chiguo_state.json")

    @property
    def memories_path(self) -> Path:
        mp = self.config.get("memory", {}).get("manual_path", "data/chiguo_memories.json")
        # 绝对路径（测试/用户指定）原样保留；相对路径锚定到 base_dir
        p = Path(mp)
        if p.is_absolute():
            return p
        return self._anchored(mp)

    # ── 持久化 ──────────────────────────────────────────

    def _load(self):
        # 优先读正式文件，不存在则从 .tmp 恢复（原子写入中途崩溃时）
        p = self.state_path
        tmp = Path(str(p) + ".tmp")
        bak = Path(str(p) + ".bak")
        if not p.exists() and tmp.exists():
            try:
                os.replace(tmp, p)
            except OSError:
                pass

        restored = False
        if p.exists():
            try:
                data = json.loads(p.read_text())
                self._apply_loaded_data(data)
                restored = True
            except Exception as e:
                # ── v5: 先尝试从 .bak 恢复 ──
                if bak.exists():
                    try:
                        data = json.loads(bak.read_text())
                        self._apply_loaded_data(data)
                        # 写入恢复后的主文件
                        self.save(_backup=False, _increment_tick=False)
                        restored = True
                        self._audit("state_recovered_from_bak", str(e))
                    except Exception as e2:
                        self._audit("state_bak_also_corrupt", str(e2))
                else:
                    self._audit("state_corrupted", str(e))

                if not restored:
                    # .bak 也损坏或不存在 → 删除主文件，下次 save 重建
                    try:
                        p.unlink()
                    except OSError:
                        pass
                    self.last_tick = None
        else:
            self.last_tick = None

        if not restored and self.last_tick is None:
            # 从未运行过，或状态全部丢失
            self._audit("state_fresh_start", "no state file found")

        if self.memories_path.exists():
            try:
                self.memories = json.loads(self.memories_path.read_text())
            except Exception:
                pass

    def _apply_loaded_data(self, data: dict):
        """解析并应用已加载的状态数据。v4/v5/v6 兼容。"""
        # ── v5: 校验和验证 ──
        # ── v6: 校验和不匹配 → 强制拒绝加载，走 _load 的 .bak 恢复链。
        # 位翻转/手改后 JSON 可解析但数据损坏，宁可回退 .bak 也不带病运行。
        stored_checksum = data.pop("_checksum", None)
        if stored_checksum:
            recomputed = hashlib.sha256(
                json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            if recomputed != stored_checksum:
                self._audit("checksum_mismatch",
                    f"stored={stored_checksum[:12]}... computed={recomputed[:12]}...")
                raise ValueError("checksum mismatch — refusing to load, falling back to .bak")

        # ── v6: 未来版本状态 → 记录审计并保守继续加载（字段过滤兜底）──
        ver = data.get("_version")
        if isinstance(ver, int) and ver > self.STATE_VERSION:
            self._audit("state_future_version",
                f"stored={ver} current={self.STATE_VERSION} loading anyway")

        # 过滤未知字段，防止未来版本新增字段导致 dataclass __init__ 崩溃
        emo_fields = {k: v for k, v in data.get("emotion", {}).items()
                      if k in ChiguoEmotion.__dataclass_fields__}
        self.emotion = ChiguoEmotion(**emo_fields)
        cd_fields = {k: v for k, v in data.get("cooldown", {}).items()
                     if k in CooldownState.__dataclass_fields__}
        # ── v5: accumulated_lambda null → 0.0 (type drift fix) ──
        if cd_fields.get("accumulated_lambda") is None:
            cd_fields["accumulated_lambda"] = 0.0
        self.cooldown = CooldownState(**cd_fields)
        # ── v6: 旧版本（无 crash_timestamps）迁移：从 last_crash_at 恢复单条记录 ──
        if (not self.cooldown.crash_timestamps and
                self.cooldown.last_crash_at):
            self.cooldown.crash_timestamps = [self.cooldown.last_crash_at]
        # ── v7: 生物钟学习器加载(字段过滤,旧版本无 circadian 字段 → 默认值)──
        circ_fields = {k: v for k, v in (data.get("circadian") or {}).items()
                       if k in CircadianTracker.__dataclass_fields__}
        self.circadian = CircadianTracker(**circ_fields)
        # ── v8: 双作息迁移(旧格式补桶 + 旧单桶窗口 → weekday_*)──
        self._migrate_circadian_v8()
        self._sync_quiet_window()
        # ── v7: 待接续话题加载(普通 list,isinstance 检查兜底)──
        pending = data.get("pending_topics")
        self.pending_topics = pending if isinstance(pending, list) else []
        self.last_tick = data.get("last_tick")
        # ── v5: tick_seq ──
        self.tick_seq = data.get("tick_seq", 0)
        # ── v4: 加载人格 ──
        pers_data = data.get("personality")
        if pers_data:
            self.personality = personality_from_dict(pers_data)
        else:
            emo_data = data.get("emotion", {})
            tsun = emo_data.get("tsundere_index", 70.0)
            self.personality.tsundere_intensity = tsun
        # v2→v3 迁移：trigger_history 有数据但 event_timestamps 空
        if (self.cooldown.trigger_history and
                not self.cooldown.event_timestamps and
                self.cooldown.last_message_at):
            try:
                base_time = datetime.fromisoformat(self.cooldown.last_message_at)
                n = len(self.cooldown.trigger_history)
                for i, t in enumerate(self.cooldown.trigger_history):
                    approx = base_time - timedelta(hours=(n - i) * 24 / max(n, 1))
                    self.cooldown.event_timestamps.append({
                        "type": t,
                        "time": approx.isoformat(),
                    })
            except (ValueError, TypeError):
                pass

    def _migrate_circadian_v8(self):
        """v8 双作息迁移(幂等,加载时执行一次):
        ① reply_days/active_days 无 bucket 条目 → 按日期启发式补桶(调休优先 → 节假日 → 周几,解析失败丢弃);
        ② 旧单桶窗口迁移到 weekday_*:若 weekday_* 与 weekend_* 均为默认且旧 confidence > 0
           → 继承旧 quiet_*(weekend 非默认说明是 v8 风格状态,兼容字段只是当前生效桶快照,不迁移)。
        """
        for key in ("reply_days", "active_days"):
            days = getattr(self.circadian, key, None)
            if not isinstance(days, list):
                continue
            migrated = []
            for d in days:
                if not isinstance(d, dict) or d.get("bucket"):
                    if isinstance(d, dict):
                        migrated.append(d)
                    continue
                try:
                    dt = datetime.fromisoformat(str(d.get("date", "")))
                except (ValueError, TypeError):
                    continue  # 解析失败 → 丢弃
                # 调休优先 → 节假日 → 周几启发式(迁移时 holiday_parser 已就绪)
                if self.holiday_parser.is_makeup_workday(dt):
                    d["bucket"] = "weekday"
                elif self.holiday_parser.is_holiday(dt):
                    d["bucket"] = "weekend"
                else:
                    d["bucket"] = "weekday" if dt.weekday() < 5 else "weekend"
                migrated.append(d)
            setattr(self.circadian, key, migrated)
        # 迁移门控类型漂移防护:旧 confidence 可能为字符串 → 强转,失败视为 0(不继承)
        try:
            legacy_conf = float(self.circadian.confidence)
        except (ValueError, TypeError):
            legacy_conf = 0.0
        if (self.circadian.weekday_quiet_start == 0
                and self.circadian.weekday_quiet_end == 8
                and self.circadian.weekday_confidence == 0.0
                and self.circadian.weekend_quiet_start == 0
                and self.circadian.weekend_quiet_end == 8
                and self.circadian.weekend_confidence == 0.0
                and legacy_conf > 0):
            self.circadian.weekday_quiet_start = self.circadian.quiet_start
            self.circadian.weekday_quiet_end = self.circadian.quiet_end
            self.circadian.weekday_confidence = legacy_conf

    def _audit(self, event: str, detail: str = ""):
        """v5: 状态损坏审计日志。追加到 chiguo_state_audit.jsonl。v6: 路径锚定。"""
        try:
            audit_path = self._anchored("chiguo_state_audit.jsonl")
            entry = {
                "event": event,
                "time": datetime.now(CST).isoformat(),
                "detail": detail,
            }
            with open(audit_path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # audit 失败不影响主流程

    STATE_VERSION = 8  # v8: 双作息(circadian 分桶学习 + 迁移)

    # ── v6: 跨进程写锁（fcntl.flock）。锁文件常驻，os.replace 换 inode 不影响锁。
    # 防止 cron + 手动运行多实例竞争写导致 checksum 不匹配→删重建。
    # 可重入：模块级 _LOCK_FDS/_LOCK_DEPTH 保证同进程共享同一 fd 与深度计数，
    # state_lock 与 save() 混用不阻塞、不提前释放。

    def _lock_acquire(self, lock_path: str) -> bool:
        """获取进程级独占锁（可重入）。返回 True 表示本次真正获得锁（需配套 release）。
        重入（同进程已持有）直接通过且不递增深度——flock 为 fd 级互斥，
        深度只需表达 0/1 持有态。非 POSIX 或 5s 内拿不到锁 → 降级无锁并审计。"""
        if _LOCK_DEPTH.get(lock_path, 0) > 0:
            return False
        fd = _LOCK_FDS.get(lock_path)
        if fd is None:
            try:
                import fcntl
            except ImportError:
                return False  # 非 POSIX → 降级无锁（与 v5 行为一致）
            try:
                fd = open(lock_path, "a+")
            except OSError:
                return False
            try:
                deadline = time_module.monotonic() + 5.0
                while True:
                    try:
                        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time_module.monotonic() >= deadline:
                            self._audit("state_lock_timeout", lock_path)
                            fd.close()
                            return False
                        time_module.sleep(0.1)
            except OSError:
                try:
                    fd.close()
                except OSError:
                    pass
                return False
            _LOCK_FDS[lock_path] = fd
        _LOCK_DEPTH[lock_path] = 1
        return True

    def _lock_release(self, lock_path: str):
        """释放锁（仅持有者调用）。释放 fd 并清空持有标记。"""
        if _LOCK_DEPTH.get(lock_path, 0) <= 0:
            return
        _LOCK_DEPTH.pop(lock_path, None)
        fd = _LOCK_FDS.pop(lock_path, None)
        if fd is not None:
            try:
                import fcntl
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            try:
                fd.close()
            except OSError:
                pass

    @contextmanager
    def state_lock(self):
        """持有 state 文件的跨进程独占锁（chiguo_state.json.lock）。
        同进程重入（同一线程递归调用）直接通过；跨进程互斥；
        5s 内获取失败则降级无锁并记录审计。锁路径 = state_path + '.lock'。
        仅单线程语义，多线程需外部串行化。"""
        lock_path = str(self.state_path) + ".lock"
        acquired = self._lock_acquire(lock_path)
        try:
            yield
        finally:
            if acquired:
                self._lock_release(lock_path)

    def _in_lock(self) -> bool:
        """当前进程是否已持有 state 锁（供 daemon 判断重入场景）。"""
        return _LOCK_DEPTH.get(str(self.state_path) + ".lock", 0) > 0

    def save(self, _backup: bool = True, _increment_tick: bool = True):
        """原子写入：先写 .tmp，再 os.replace（避免写崩损坏正式文件）。

        v5 增强：
        - 写前备份到 .bak（可恢复上次正确状态）
        - fsync 确保落盘
        - 验证 tmp 可读再替换
        - tick_seq 递增
        - OSError 保护
        v6: fcntl.flock 跨进程写锁（写-写竞争防护；读加载不持锁，
        读-写竞争窗口窄且由原子 replace 兜底）。锁可重入：save 在
        state_lock 内被调用时复用同一 fd，不阻塞。
        """
        p = self.state_path
        tmp_path = Path(str(p) + ".tmp")
        bak_path = Path(str(p) + ".bak")

        lock_path = str(p) + ".lock"
        lock_acquired = self._lock_acquire(lock_path)
        try:
            # ── v5: 写前备份 ──
            if _backup and p.exists():
                try:
                    shutil.copy2(str(p), str(bak_path))
                except OSError:
                    pass  # 备份失败不阻塞 save
                try:
                    os.chmod(bak_path, 0o600)  # 备份含隐私状态 → 0600
                except OSError:
                    pass

            if _increment_tick:
                self.tick_seq += 1

            # 构建数据（不含 _checksum，先算哈希再添加）
            payload = {
                "_version": self.STATE_VERSION,
                "emotion": asdict(self.emotion),
                "cooldown": asdict(self.cooldown),
                "circadian": asdict(self.circadian),
                "pending_topics": self.pending_topics,
                "personality": personality_to_dict(self.personality),
                "last_tick": datetime.now(CST).isoformat(),
                "tick_seq": self.tick_seq,
            }
            # ── v5: 校验和（SHA256 of compact JSON，防位翻转）──
            checksum = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            payload["_checksum"] = checksum

            data = json.dumps(payload, indent=2, ensure_ascii=False)

            tmp_path.write_text(data)

            # ── v5: fsync 确保落盘 ──
            try:
                with open(tmp_path, 'rb') as _fsync_f:
                    os.fsync(_fsync_f.fileno())
            except OSError:
                pass  # fd 在某些环境不可用，跳过

            # ── v5: 验证 tmp 是合法 JSON ──
            try:
                _verify = json.loads(tmp_path.read_text())
                if not isinstance(_verify, dict) or "_version" not in _verify:
                    raise ValueError("tmp validation failed: not a dict or no _version")
            except (json.JSONDecodeError, ValueError, OSError):
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return  # 跳过本次 save，不替换好状态

            os.replace(tmp_path, self.state_path)

        except OSError as e:
            # ── v5: 磁盘满/Permission denied → 不崩溃 ──
            print(f"[chiguo_state] save failed: {e}", file=sys.stderr)
        finally:
            if lock_acquired:
                self._lock_release(lock_path)

    # ── v4：Bayesian 用户状态推断器（延迟初始化）────────────

    @property
    def bayesian_estimator(self):
        """延迟导入 Bayesian 推断器，避免循环依赖。"""
        if self._bayesian_estimator is None:
            from chiguo_bayesian import UserStateEstimator
            self._bayesian_estimator = UserStateEstimator(
                self.config.get("bayesian", {})
            )
        return self._bayesian_estimator

    def infer_user_state(self, now: datetime = None, msg_length: int = None) -> dict:
        """
        推断当前用户状态。融合 Bayesian 推断 + 课表/假期信息。
        从未交互过 → 返回默认中性状态（避免误判为 sleeping）。
        """
        if now is None:
            now = datetime.now(CST)

        # 从未交互过 → 返回默认中性状态（墙钟，睡眠也计入天数）
        silent_h = self.cooldown.silent_hours(now, wall=True)
        if silent_h > 720:  # 30 天从未交互
            return {
                "posterior": {"chatting": 0.05, "browsing": 0.50, "busy": 0.10,
                              "sleeping": 0.05, "away": 0.25, "needs_care": 0.05},
                "most_likely": "browsing",
                "confidence": 0.50,
                "utility": 0.53,
                "should_send_bayesian": True,
                "state_description": "未知（从未交互）",
            }

        last_latency = None
        if self.cooldown.reply_latencies:
            last_latency = self.cooldown.reply_latencies[-1]

        last_msg_len = None
        if self.cooldown.last_user_message_at:
            last_msg_len = msg_length if msg_length is not None else (
                self.cooldown.last_user_msg_length if self.cooldown.last_user_msg_length is not None else 10
            )

        # 课表状态
        in_class = False
        try:
            sch = self.schedule_parser.query(now)
            in_class = sch.get("in_class", False)
        except Exception:
            pass

        observations = {
            "reply_latency": last_latency,
            "msg_length": last_msg_len,
            # 墙钟沉默时间：Bayesian classifier 阈值基于墙钟校准
            "silence_hours": self.cooldown.silent_hours(now, wall=True),
            "in_class": in_class,
            "is_weekend": now.weekday() >= 5,
        }

        result = self.bayesian_estimator.infer(observations, now)
        return result

    # ── v4: 人格自适应 ─────────────────────────────────────

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
            warmth = interaction.get("warmth", 0.0)
            lat_cat = interaction.get("latency_category", "normal")
            msg_len = interaction.get("msg_length", 10)

            # 温暖回复
            if warmth > 0.3:
                delta.evolve(PersonalityDeltas.WARM_REPLY)
            # 冷淡回复
            elif warmth < -0.2:
                delta.evolve(PersonalityDeltas.COLD_REPLY)

            # 回复速度
            if lat_cat == "fast":
                delta.evolve(PersonalityDeltas.FAST_REPLY)
            elif lat_cat == "slow":
                delta.evolve(PersonalityDeltas.SLOW_REPLY)
            elif lat_cat == "very_slow":
                delta.evolve(PersonalityDeltas.VERY_SLOW_REPLY)

            # 长消息 → 更开放
            if msg_len > 30:
                delta.evolve(PersonalityDeltas.LONG_MESSAGE)

        elif itype == "character_send":
            prev_send_was_replied = interaction.get("was_replied", False)
            if prev_send_was_replied:
                delta.evolve(PersonalityDeltas.SENT_AND_REPLIED)
            else:
                delta.evolve(PersonalityDeltas.SENT_NO_REPLY)

        self.personality.evolve(delta)

        # 情绪调制：缓存 anxiety_sensitivity 供 _apply_emotion_impact 使用
        self.personality._cached_anxiety_sensitivity = self.personality.anxiety_sensitivity()

    # ── 寒暑假检测 ────────────────────────────────────────

    @property
    def break_state_path(self) -> Path:
        return self._anchored("break_state.json")

    def _read_break_state(self) -> dict | None:
        """读取 break_state.json，不存在或损坏返回 None"""
        bp = self.break_state_path
        if not bp.exists():
            return None
        try:
            return json.loads(bp.read_text())
        except Exception:
            return None

    def _in_break_range(self, today: date_type) -> bool:
        """检查今天是否在存储的假期区间内"""
        data = self._read_break_state()
        if not data:
            return False
        for b in data.get("breaks", []):
            try:
                start = date_type.fromisoformat(b["start"])
                end = date_type.fromisoformat(b["end"])
                if start <= today <= end:
                    return True
            except (ValueError, KeyError):
                continue
        return False

    @property
    def on_break(self) -> bool:
        """是否在假期中（日期区间 / 手动覆盖 / 学期自动结束）"""
        data = self._read_break_state()
        if data:
            # 手动无限期覆盖（兼容旧 on_break 字段）
            if data.get("manual_override") or data.get("on_break"):
                return True
        today = datetime.now(CST).date()
        # 日期区间判定
        if self._in_break_range(today):
            return True
        # 学期自动结束
        if self.semester_end and today > self.semester_end:
            return True
        return False

    # ── 课表查询 ──────────────────────────────────────────

    def availability(self, now: datetime, user_state: dict = None) -> float:
        """
        主人当前可接收消息的程度 [0, 1]。
        寒暑假   → 0.85（手动或学期结束）
        节假日   → 0.85（放假，完全自由）
        上课中(满课)→ 0.05 / 0.08 / 0.12（极低但非零，崩溃边缘可突破）
        课间/剩余课上完 → 0.85 / 0.70 / 0.50（按剩余节数递减）
        空闲     → 0.85
        深夜     → 0.0（硬禁发，由 can_send 处理）

        v4: 集成 Bayesian 用户状态推断。user_sleeping → 0.0, user_busy → ×0.5
        """
        # ── 第零层：寒暑假检测，最高优先级 ──
        if self.on_break:
            base = 0.85
        # ── 第零点半层：考试周 ──
        elif not self.holiday_parser.is_holiday(now):
            today = now.date() if isinstance(now, datetime) else now
            in_exam = False
            for start, end in self.exam_ranges:
                if start <= today <= end:
                    in_exam = True
                    break
            if in_exam:
                base = 0.5
            elif not self.holiday_parser.is_school_day(now):
                base = 0.85
            else:
                # ── 第二层：课表判断 ──
                try:
                    sch = self.schedule_parser.query(now)
                except Exception:
                    base = 0.85
                else:
                    if sch["in_class"]:
                        load = sch.get("class_load", "normal")
                        base = {"heavy": 0.05, "normal": 0.08, "light": 0.12}.get(load, 0.08)
                    else:
                        remaining = sch.get("remaining_classes", 0)
                        if remaining == 0:
                            base = 0.85
                        elif remaining <= 1:
                            base = 0.70
                        else:
                            base = 0.50
        else:
            # 节假日 / 非上学日
            base = 0.85

        # ── v4: Bayesian 用户状态调制 ──
        try:
            if user_state is None:
                user_state = self.infer_user_state(now)
            most_likely = user_state.get("most_likely", "browsing")
            confidence = user_state.get("confidence", 0.0)
            # v6: 只有高置信度的 sleeping 推断才阻塞发送（置信度门槛来自 config
            # [bayesian] min_confidence_for_block，默认 0.5），低置信不误伤
            if most_likely == "sleeping" and confidence > self.config.get("bayesian", {}).get("min_confidence_for_block", 0.5):
                base = 0.0  # 用户很可能在睡觉 → 绝不发送
            elif most_likely == "busy":
                base *= 0.5  # 用户忙 → 降低
            elif most_likely == "needs_care":
                base = min(base * 1.2, 0.95)  # 需要关心 → 略微提高
            # 焦虑阻塞（"生气了不会找你"）
            if self.emotion.anxiety > self.config.get("cooldown", {}).get("anxiety_block_threshold", 70.0):
                base *= 0.3  # 生气时大幅降低
        except Exception:
            pass

        return base

    def schedule_status(self, now: datetime) -> dict | None:
        """获取课表快照，用于展示和 context 注入。寒暑假/节假日优先。"""

        # 始终计算假期区间信息
        def _breaks_info():
            data = self._read_break_state()
            today = now.date() if isinstance(now, datetime) else now
            breaks = []
            if data:
                for b in data.get("breaks", []):
                    try:
                        start = date_type.fromisoformat(b["start"])
                        end = date_type.fromisoformat(b["end"])
                        breaks.append({
                            "start": b["start"], "end": b["end"],
                            "note": b.get("note", ""),
                            "active": start <= today <= end,
                        })
                    except (ValueError, KeyError):
                        continue
            return breaks

        if self.on_break:
            today = datetime.now(CST).date()
            reason = "学期已结束" if (self.semester_end and today > self.semester_end) else None
            data = self._read_break_state()
            if data and (data.get("manual_override") or data.get("on_break")):
                reason = reason or "手动无限期开启"
            return {
                "in_class": False,
                "current_course": None,
                "class_load": "free",
                "remaining_classes": 0,
                "total_classes": 0,
                "on_break": True,
                "break_reason": reason or "日期区间",
                "breaks": _breaks_info(),
            }
        hq = self.holiday_parser.query(now)
        if hq["is_holiday"]:
            return {
                "in_class": False,
                "current_course": None,
                "class_load": "free",
                "remaining_classes": 0,
                "total_classes": 0,
                "holiday": hq["holiday_name"],
                "holiday_hint": hq["hint"],
                "on_break": False,
                "breaks": _breaks_info(),
            }
        if hq["is_weekend"] and not hq["is_makeup_workday"]:
            return {
                "in_class": False,
                "current_course": None,
                "class_load": "free",
                "remaining_classes": 0,
                "total_classes": 0,
                "weekend": True,
                "on_break": False,
                "breaks": _breaks_info(),
            }
        try:
            result = self.schedule_parser.query(now)
            if hq["is_makeup_workday"]:
                result["makeup_day"] = True
                result["makeup_reason"] = hq["hint"]
            result["on_break"] = False
            result["breaks"] = _breaks_info()
            return result
        except Exception:
            return None

    # ── 时间推进（半衰期驱动） ──────────────────────────

    def tick(self, hours: float, now: datetime):
        """
        推进时间。所有情绪变化用半衰期公式，不再线性。
        """
        cfg = self.config.get("emotion", {})
        silent_h = self.cooldown.silent_hours(now)

        # ── 孤独值: 向 100 靠拢，半衰期控制速度 ──
        half_life = cfg.get("loneliness_gain_half_life", 40.0)
        # 长时间沉默，靠拢加速（半衰期 ×0.6 → 恢复速度 ≈ 1.67 倍）
        if silent_h > 24:
            half_life = half_life * 0.6
        old_lo = self.emotion.loneliness
        self.emotion.loneliness = recover(
            self.emotion.loneliness, 100.0, hours, half_life
        )

        # ── 不安值: 向 100 靠拢。知道主人在上课时焦虑减速 ──
        old_anx = self.emotion.anxiety
        anx_hl = cfg.get("anxiety_gain_half_life", 30.0)
        # 节假日/周末：主人休息，焦虑极慢
        if self.holiday_parser.is_holiday(now):
            anx_hl *= 2.5  # 放假，完全放松
        elif not self.holiday_parser.is_school_day(now):
            anx_hl *= 2.0  # 普通周末，焦虑减速
        else:
            # 课表调节：主人在上课/今天满课 → 焦虑涨得慢（已知原因，不那么慌）
            try:
                sch = self.schedule_parser.query(now)
                if sch["in_class"]:
                    anx_hl *= 1.8  # 半衰期延长80% → 焦虑几乎不涨
                elif sch.get("class_load") == "heavy":
                    anx_hl *= 1.4  # 满课日 → 焦虑涨得慢
            except Exception:
                pass
        self.emotion.anxiety = recover(self.emotion.anxiety, 100.0, hours, anx_hl)

        # 记录变化率（urgency 感知：暴涨 vs 缓慢累积）
        if hours > 0.01:
            self.emotion.loneliness_rate = (
                self.emotion.loneliness - old_lo
            ) / hours
            self.emotion.anxiety_rate = (
                self.emotion.anxiety - old_anx
            ) / hours

        # ── 好感值: 向 0 极慢靠拢 ──
        aff_hl = cfg.get("affection_loss_half_life", 500.0)
        if silent_h > 24:
            self.emotion.affection = recover(self.emotion.affection, 0.0, hours, aff_hl)

        # ── 傲娇指数（快变量：情绪波动）──
        if self.emotion.affection > 65:
            self.emotion.tsundere_index -= 0.3 * hours
        if self.emotion.anxiety > 60:
            self.emotion.tsundere_index += 0.2 * hours

        # ── v4: 傲娇指数向人格基线回归 ──
        baseline = self.personality.tsundere_intensity
        if self.emotion.tsundere_index != baseline:
            # 慢速向基线靠拢（半衰期 ~200h）
            self.emotion.tsundere_index += (baseline - self.emotion.tsundere_index) * (1 - 2.0 ** (-hours / 200.0))

        # ── 元气值: 向 100 恢复 ──
        energy_hl = cfg.get("energy_regen_half_life", 8.0)
        self.emotion.energy = recover(self.emotion.energy, 100.0, hours, energy_hl)

        self._finalize(now)

    # ── v7: 接话茬 — 待接续话题管理 ────────────────────

    def add_pending_topic(self, topic: str, now: datetime, source: str = "analysis"):
        """记录待接续话题。同话题视为已接续 → 移除旧条目后重新计时(活跃对话不触发)。
        非字符串/空白 → 忽略。上限 20 条,超出丢弃最旧。"""
        if not isinstance(topic, str) or not topic.strip():
            return
        topic = topic.strip()[:50]
        self.pending_topics = [
            t for t in self.pending_topics
            if not (isinstance(t, dict) and t.get("topic") == topic)
        ]
        self.pending_topics.append({
            "topic": topic,
            "source": source,
            "created_at": now.isoformat(),
            "attempted": False,
        })
        self._cap_pending_topics()

    def resolve_pending_topic(self, topic: str | None, now: datetime):
        """topic_resolved=true → 移除对应话题。未指定 topic → 移除最旧一条。"""
        if isinstance(topic, str) and topic.strip():
            self.pending_topics = [
                t for t in self.pending_topics
                if not (isinstance(t, dict) and t.get("topic") == topic.strip())
            ]
        elif self.pending_topics:
            self.pending_topics.pop(0)

    def mark_pending_topic_attempted(self, topic: str):
        """接话茬触发后标记已尝试(该话题不再重复触发)。"""
        for t in self.pending_topics:
            if not isinstance(t, dict):
                continue
            if t.get("topic") == topic:
                t["attempted"] = True

    def prune_pending_topics(self, now: datetime, max_age_hours: float = 48.0):
        """移除过期/已尝试话题,防状态膨胀。坏时间戳直接丢弃。"""
        kept = []
        for t in self.pending_topics:
            if not isinstance(t, dict):
                continue
            try:
                dt = datetime.fromisoformat(t.get("created_at", ""))
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            age = (now - dt).total_seconds() / 3600
            if age <= max_age_hours and not t.get("attempted"):
                kept.append(t)
        self.pending_topics = kept
        self._cap_pending_topics()

    def _cap_pending_topics(self, cap: int = 20):
        if len(self.pending_topics) > cap:
            self.pending_topics = self.pending_topics[-cap:]

    # ── 事件响应（半衰期衰减） ──────────────────────────

    def on_user_message(self, now: datetime, msg_length: int = 10,
                         analysis: dict | None = None):
        """收到主人消息：情绪骤降，用半衰期建模。
        若提供 LLM 分析结果（analysis），在基础效果之上叠加内容感知微调。
        回复速度影响情感变化量（秒回更开心，很久才回效果打折）。"""
        cfg = self.config.get("emotion", {})

        # ── 先计算回复延迟（在情感变化之前）──
        latency_h: float | None = None
        if self.cooldown.last_message_at:
            try:
                last_send = datetime.fromisoformat(self.cooldown.last_message_at)
                latency_h = (now - last_send).total_seconds() / 3600
                self.cooldown.reply_latencies.append(latency_h)
                if len(self.cooldown.reply_latencies) > 20:
                    self.cooldown.reply_latencies = self.cooldown.reply_latencies[-20:]
            except (ValueError, TypeError):
                pass

        # ── 回复速度倍率 ──
        lat_mult = self._latency_multiplier(latency_h) if latency_h is not None else {}

        # 孤独骤降（半衰期 0.35h ≈ 21分钟减半）
        hl = cfg.get("loneliness_decay_on_reply", 0.35)
        self.emotion.loneliness = decay(self.emotion.loneliness, 1.0, hl)

        # 不安骤降（半衰期 0.5h，很久才回时部分抵消）
        anx_hl = cfg.get("anxiety_decay_on_reply", 0.5)
        self.emotion.anxiety = decay(self.emotion.anxiety, 1.0, anx_hl)
        if lat_mult.get("anxiety_rebound", 0) > 0:
            self.emotion.anxiety += lat_mult["anxiety_rebound"]

        # 好感微增（基础值 × 回复速度倍率）
        gain = cfg.get("affection_gain_per_interaction", 0.8)
        if msg_length > 30:
            gain *= 1.5
        affection_mult = lat_mult.get("affection", 1.0)
        self.emotion.affection += gain * affection_mult

        # 元气奖励（基础值 + 秒回额外奖励）
        bonus = cfg.get("energy_bonus_on_reply", 10.0)
        self.emotion.energy += bonus + lat_mult.get("energy_extra", 0)

        # 傲娇软化（基础值 + 秒回额外）
        tsun_drop = 1.5 + lat_mult.get("tsundere_extra_drop", 0)
        self.emotion.tsundere_index -= tsun_drop

        # ── LLM 内容分析微调（叠加在基础上） ──
        if analysis is not None:
            self._anxiety_before_analysis = self.emotion.anxiety
            self._apply_emotion_impact(analysis, now)

            # ── v7: 接话茬话题摄入 ──
            topic = analysis.get("topic")
            if analysis.get("topic_resolved"):
                self.resolve_pending_topic(topic, now)
            elif topic:
                self.add_pending_topic(topic, now)

        self.cooldown.last_user_message_at = now.isoformat()
        self.cooldown.last_user_msg_length = msg_length
        self.cooldown.messages_without_reply = 0

        # ── v4: 概率累积重置 ──
        self.cooldown.held_count = 0
        if self.cooldown.accumulated_lambda > 0:
            poisson_cfg = self.config.get("poisson", {})
            base = poisson_cfg.get("base_lambda", 0.25)
            self.cooldown.accumulated_lambda = longing_decay(
                self.cooldown.accumulated_lambda, base,
                decay_factor=self.config.get("cooldown", {}).get("longing_decay_factor", 0.5)
            )

        # ── v4: 人格自适应（用户回复类）──
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
            self.adapt_personality({
                "type": "user_reply",
                "warmth": warmth,
                "latency_category": lat_cat,
                "msg_length": msg_length,
            })
        except Exception:
            pass

        # ── v4: Bayesian 在线学习 ──
        try:
            silence_h = self.cooldown.silent_hours(now, wall=True) if now else 0
            obs = {
                "reply_latency": round(latency_h, 3) if latency_h else None,
                "msg_length": msg_length,
                "silence_hours": round(silence_h, 2),
            }
            actual = None
            if latency_h is not None and latency_h < 0.5:
                actual = "chatting"
            self.bayesian_estimator.record_observation(obs, actual_state=actual)
        except Exception:
            pass

        # ── v7/v8: 生物钟学习(每次回复记录小时 + 重算窗口;v8 按当日分桶)──
        circ_cfg = self.config.get("circadian", {})
        self.circadian.record(now, circ_cfg.get("history_days", 14), self._current_bucket(now))
        self.circadian.recompute(
            min_sample_days=circ_cfg.get("min_sample_days", 7),
            history_days=circ_cfg.get("history_days", 14),
            min_width=circ_cfg.get("min_width", 5),
            max_width=circ_cfg.get("max_width", 12),
        )
        # v8: 与 record 使用同一 now(测试注入过去时间时桶选择语义一致)
        self._sync_quiet_window(now)

        self._finalize(now)

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

    def _apply_emotion_impact(self, analysis: dict, now: datetime | None = None):
        """
        根据 OpenClaw LLM 分析结果微调情绪状态。
        三个维度：warmth（温暖度）、effort（用心度）、attention（关注度）。
        所有系数从配置文件注入，可调参。
        """
        cfg = self.config.get("emotion", {})

        # 钳位到有效范围
        warmth = max(-1.0, min(1.0, analysis.get("warmth", 0.0)))
        effort = max(0.0, min(1.0, analysis.get("effort", 0.0)))
        attention = max(0.0, min(1.0, analysis.get("attention", 0.0)))

        # ── 温暖度 → 好感 & 元气 ──
        self.emotion.affection += warmth * cfg.get("affection_warmth_factor", 1.5)
        self.emotion.energy += warmth * cfg.get("energy_warmth_factor", 4.0)

        # 负温暖 → 不安回升（冷淡回复部分抵消 decay 效果）
        if warmth < 0:
            self.emotion.anxiety += abs(warmth) * cfg.get("anxiety_warmth_recovery", 3.0)

        # ── 用心度 → 好感 & 傲娇软化 ──
        self.emotion.affection += effort * cfg.get("affection_effort_factor", 1.0)
        self.emotion.tsundere_index -= effort * cfg.get("tsundere_effort_factor", 2.0)

        # ── 关注度 → 元气 & 被忽视时不安 ──
        self.emotion.energy += attention * cfg.get("energy_attention_factor", 4.0)
        if attention < 0.3:
            self.emotion.anxiety += (0.3 - attention) * cfg.get("anxiety_ignore_factor", 2.0)

        # ── v4: 人格 anxiety_sensitivity 调制不安变化幅度 ──
        anx_sens = getattr(self.personality, '_cached_anxiety_sensitivity', 1.0)
        if anx_sens != 1.0 and hasattr(self, '_anxiety_before_analysis'):
            delta = self.emotion.anxiety - self._anxiety_before_analysis
            if delta != 0:
                self.emotion.anxiety = self._anxiety_before_analysis + delta * anx_sens

        # ── 忙碌抑制（LLM 检测到用户想结束话题）──
        suppress_hours = analysis.get("suppress_hours", 0)
        if suppress_hours > 0 and now is not None:
            until = (now + timedelta(hours=min(suppress_hours, 24))).isoformat()
            # 取两者中较晚的（已设抑制期 → 新值更晚时覆盖延长）
            if self.cooldown.busy_suppress_until:
                try:
                    existing = datetime.fromisoformat(self.cooldown.busy_suppress_until)
                    if now + timedelta(hours=suppress_hours) > existing:
                        self.cooldown.busy_suppress_until = until
                except (ValueError, TypeError):
                    self.cooldown.busy_suppress_until = until
            else:
                self.cooldown.busy_suppress_until = until

    def on_character_message(self, now: datetime, trigger_type: str = "",
                             msg_id: str | None = None):
        """迟菓发出主动消息后。msg_id（v6）写入 Hawkes 事件，供 refund_send 按 id 回滚。"""
        cfg = self.config.get("emotion", {})

        # 消耗元气
        cost = cfg.get("energy_cost_per_message", 20.0)
        self.emotion.energy = max(0, self.emotion.energy - cost)

        # 表达欲满足，孤独缓降（半衰期 2h）
        send_hl = cfg.get("loneliness_decay_on_send", 2.0)
        self.emotion.loneliness = decay(self.emotion.loneliness, 1.0, send_hl)

        # 不安小升（"我是不是太烦了"）
        anx_gain = cfg.get("anxiety_gain_on_send", 2.0)
        self.emotion.anxiety += anx_gain

        self.cooldown.last_message_at = now.isoformat()
        self.cooldown.messages_today += 1
        self.cooldown.messages_without_reply += 1

        # Hawkes 事件记录
        if trigger_type:
            event = {
                "type": trigger_type,
                "time": now.isoformat(),
            }
            if msg_id is not None:
                event["msg_id"] = msg_id  # v6: 供 refund_send 按 msg_id 精确回滚
            self.cooldown.event_timestamps.append(event)
            # 保留最近 50 条
            if len(self.cooldown.event_timestamps) > 50:
                self.cooldown.event_timestamps = self.cooldown.event_timestamps[-50:]

        # ── v5: 发送时重置累积（0.0 而非 None，消除 type drift）──
        self.cooldown.held_count = 0
        self.cooldown.accumulated_lambda = 0.0

        # ── v4.1: 安全阀 — 追踪崩溃触发 ──
        # ── v6: 崩溃记录改为时间戳列表，_prune_crash_history 按 48h 窗口滑动过滤 ──
        crash_types = ("lonely_high", "anxiety")
        if trigger_type in crash_types:
            self.cooldown.last_crash_at = now.isoformat()
            self.cooldown.crash_timestamps.append(now.isoformat())
            if len(self.cooldown.crash_timestamps) > 50:
                self.cooldown.crash_timestamps = self.cooldown.crash_timestamps[-50:]
        self._prune_crash_history(now)

        # ── v4: Bayesian 在线学习 ──
        try:
            silence_h = self.cooldown.silent_hours(now, wall=True) if now else 0
            self.bayesian_estimator.record_observation({
                "reply_latency": None,
                "msg_length": None,
                "silence_hours": round(silence_h, 2),
                "trigger": trigger_type,
                "messages_today": self.cooldown.messages_today,
                "loneliness": round(self.emotion.loneliness, 1),
                "anxiety": round(self.emotion.anxiety, 1),
                "energy": round(self.emotion.energy, 1),
            })
        except Exception:
            pass

        self._finalize(now)

    # ── Poisson 事件率 ──────────────────────────────────

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

        # 课表调节：上课时 λ 降低
        if now:
            lam *= self.availability(now)

        # 无回复退避：每条未回复消息，λ 衰减
        decay_factor = self.config.get("cooldown", {}).get("no_reply_lambda_decay", 0.7)
        n = self.cooldown.messages_without_reply
        lam *= decay_factor ** min(n, 5)

        # ── Hawkes 自激过程（替代原 1+0.15*lonely_count 粗略近似）──
        hawkes_cfg = self.config.get("hawkes", {})
        if hawkes_cfg.get("enabled", True) and self.cooldown.event_timestamps:
            alpha = hawkes_cfg.get("alpha", 0.3)
            beta = hawkes_cfg.get("beta", 0.5)
            window = hawkes_cfg.get("window_hours", 24.0)
            lam = hawkes_intensity(
                lam, self.cooldown.event_timestamps, now,
                alpha, beta, window,
            )

        # ── 变化率加速 ──
        emo_cfg = self.config.get("emotion", {})
        lo_rate = self.emotion.loneliness_rate
        anx_rate = self.emotion.anxiety_rate
        rate_boost = 1.0
        rate_boost += max(0, (lo_rate - 1.0) * emo_cfg.get("lambda_lo_rate_factor", 0.4))
        rate_boost += max(0, (anx_rate - 1.0) * emo_cfg.get("lambda_anx_rate_factor", 0.3))
        lam *= rate_boost

        return lam

    # ── 概率触发（sigmoid 替代硬阈值） ──────────────────

    def trigger_weight(self, trigger_type: str) -> float:
        """
        返回某类触发在当前情绪下的概率权重（0~1）。
        不再 if loneliness > 55，而是平滑概率。
        """
        cfg = self.config.get("sigmoid", {})
        lo = self.emotion.loneliness
        anx = self.emotion.anxiety

        if trigger_type == "lonely_low":
            return sigmoid(lo, cfg.get("loneliness_low_mid", 38),
                           cfg.get("loneliness_low_k", 0.20))
        elif trigger_type == "lonely_mid":
            return sigmoid(lo, cfg.get("loneliness_mid_mid", 55),
                           cfg.get("loneliness_mid_k", 0.18))
        elif trigger_type == "lonely_high":
            return sigmoid(lo, cfg.get("loneliness_high_mid", 78),
                           cfg.get("loneliness_high_k", 0.15))
        elif trigger_type == "anxiety":
            return sigmoid(anx, cfg.get("anxiety_mid", 58),
                           cfg.get("anxiety_k", 0.12))
        return 0.0

    # ── 能否发送 ────────────────────────────────────────

    def is_longing_overflow(self) -> bool:
        """概率累积溢出检查：held_count > 3 且 λ 累积到阈值且焦虑不阻塞。"""
        cfg = self.config.get("cooldown", {})
        base_lambda = self.config.get("poisson", {}).get("base_lambda", 0.25)
        acc_lam = self.cooldown.accumulated_lambda  # v5: type is always float
        return (self.cooldown.held_count > 3
                and acc_lam >= base_lambda * 1.5
                and self.emotion.anxiety < cfg.get("anxiety_block_threshold", 70.0))

    # ── v6: 溢出逃生阀 ────────────────────────────────────
    # 死锁态（anxiety ≥ 阻塞阈值）下 longing 数学自我否决（blocked 不累积，
    # λ 回退值被 availability×0.3 + 退避压到 ~0.008），overflow 永远到不了。
    # 逃生阀改为时间+状态驱动：阻塞态 + 沉默超限 + 冷却期外 → 破防发送。

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

    def refund_send(self, now: datetime, msg_id: str | None = None):
        """发送失败退款（v6 反馈闭环）：退还元气/不安消耗、日计数、未回复计数。
        消息从未真正发出 → 情绪消耗与额度统计全部回滚，下次 tick 可重发。
        - 重置逃生阀冷却：未送达的消息不该白扣 3 天破防机会。
        - held_count/accumulated_lambda 不回滚（每次发送都会清零，重累积即可）。
        - loneliness 缓降不回滚（决策本身已产生释压感，语义合理）。
        - v6: 提供 msg_id 时按 msg_id 精确移除对应 Hawkes 事件（乱序回传不弹错）；
          未提供或未匹配到 → 回退移除最后一条（旧行为，向后兼容）。
        - last_message_at 不还原（设计取舍，保持现状）。"""
        cfg = self.config.get("emotion", {})
        cost = cfg.get("energy_cost_per_message", 20.0)
        self.emotion.energy = min(100.0, self.emotion.energy + cost)
        anx_gain = cfg.get("anxiety_gain_on_send", 2.0)
        self.emotion.anxiety = max(0.0, self.emotion.anxiety - anx_gain)
        self.cooldown.messages_today = max(0, self.cooldown.messages_today - 1)
        self.cooldown.messages_without_reply = max(0, self.cooldown.messages_without_reply - 1)
        # 移除 Hawkes 事件记录（该消息从未发出，不应激发后续 λ）
        if self.cooldown.event_timestamps:
            if msg_id is not None:
                for i, ev in enumerate(self.cooldown.event_timestamps):
                    if ev.get("msg_id") == msg_id:
                        del self.cooldown.event_timestamps[i]
                        break
                else:
                    self.cooldown.event_timestamps.pop()  # 未匹配 → 回退旧行为
            else:
                self.cooldown.event_timestamps.pop()
        self.cooldown.last_longing_break_at = None
        self._finalize(now)

    def can_send(self, now: datetime) -> bool:
        cfg = self.config.get("cooldown", {})
        silent_h = self.cooldown.silent_hours(now)

        # 硬性每日上限（v6: 逃生阀破防同 overflow 可突破日限额）
        daily_max = cfg.get("max_daily_active", 4) if silent_h < 8 else cfg.get("max_daily_silent", 2)
        if self.cooldown.messages_today >= daily_max:
            if not (self.is_longing_overflow() or self.longing_break_eligible(now)):
                return False

        # 最小间隔
        min_interval = cfg.get("min_interval_minutes", 30)
        mins_since = self.cooldown.minutes_since_last_message(now)
        # v6: 解析失败返回 None → 数据可疑但放行（与"从未发过"一致），不误判为过频
        if mins_since is not None and mins_since < min_interval:
            return False

        # 元气检查（孤独暴涨时可覆盖）
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

        # 静默窗口禁止(配置默认 0-8;生物钟学习达标后为学习窗口)
        qs, qe = self.cooldown.quiet_window()
        if qe < qs:
            if now.hour >= qs or now.hour < qe:
                return False
        else:
            if qs <= now.hour < qe:
                return False

        # 忙碌抑制（用户说"忙"/"晚安"等）
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

        # 过期 → 滑动窗口重算（v6: 委托 _prune_crash_history 逐条过期）
        if hours_since > crash_window:
            self._prune_crash_history(now)
            if not self.cooldown.last_crash_at:
                return 0

        # Level 2: 48h 内 ≥ crash_max 次崩溃
        if self.cooldown.crash_count_48h >= crash_max:
            return 2

        # Level 1: 24h 内有过崩溃
        if hours_since < cooldown_hours:
            return 1

        return 0

    def snapshot(self, now: datetime, user_state: dict = None) -> dict:
        sch = self.schedule_status(now)
        hq = self.holiday_parser.query(now)

        # ── v4: Bayesian 用户状态推断 ──
        if user_state is None:
            try:
                user_state = self.infer_user_state(now)
            except Exception:
                pass

        snap = {
            "emotion": asdict(self.emotion),
            "dominant_layer": self.emotion.dominant_layer,
            "neediness": round(self.emotion.neediness, 1),
            "poisson_lambda": round(self.current_lambda(now), 4),
            "availability": round(self.availability(now, user_state), 2),
            "holiday": {
                "is_holiday": hq["is_holiday"],
                "name": hq["holiday_name"],
                "is_weekend": hq["is_weekend"],
                "is_makeup_workday": hq["is_makeup_workday"],
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
            # ── v4: 人格 + 用户状态 ──
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
        return snap
