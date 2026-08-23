
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
from schedule.parser import refresh_schedule_cache
from schedule.holiday import HolidayParser
from schedule.anniversary import AnniversaryManager
from schedule.override_store import OverrideStore
from schedule.plan_store import PlanStore
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

_OWNER_PLACEHOLDER = "owner@im.wechat"

def _config_owner(cfg: dict) -> str | None:
    if not isinstance(cfg, dict):
        return None
    if "owner" in cfg and cfg["owner"]:
        v = cfg["owner"]
        if isinstance(v, str) and v.strip():
            return v.strip()
    w = cfg.get("wechat")
    if isinstance(w, dict):
        for k in ("wechat_recipient", "owner"):
            v = w.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    v = cfg.get("wechat_recipient")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None

def _is_placeholder_owner(v: str | None) -> bool:
    return not v or v == _OWNER_PLACEHOLDER

def _check_owner_mismatch(config_owner: str | None, disk_owner: str | None) -> bool:
    """真 owner 互异则需拦截。placeholder/空视为未分区不校验。"""
    if _is_placeholder_owner(config_owner) or _is_placeholder_owner(disk_owner):
        return False
    return config_owner != disk_owner

class StatePersistence:
    """T11·Q1 持久化单类：负责 chiguo_state.json 的原子读写 / 备份 / 校验和 /
    跨进程 flock / 审计日志 / 版本迁移触发。

    持有 owner（ChiguoState）的引用，序列化/反序列化时通过 owner 的各公开字段
    （emotion/cooldown/circadian/personality/…）读写成整份状态快照；加载完成后
    交由 apply_loaded_data（含 migrate_circadian_v8）应用数据，并调 owner._sync_quiet_window
    就地重建可变子对象。如此把文件层关注点从核心状态类中剥离，ChiguoState 只保留
    决策/情绪/课表等人格逻辑。
    """

    STATE_VERSION = 10  # v8: 双作息(circadian 分桶学习 + 迁移); v9: cooldown.recv_dedup; v10: personality_baseline + personality_history

    def __init__(self, config: dict, owner):
        self.config = config
        self.owner = owner
        self._lock_degraded = False

    def anchored(self, *parts: str) -> Path:
        base = self.config.get("_base_dir", ".") or "."
        return Path(base) / Path(*parts)

    @property
    def state_path(self) -> Path:
        return self.anchored("chiguo_state.json")

    @property
    def break_state_path(self) -> Path:
        return self.anchored("break_state.json")

    @property
    def memories_path(self) -> Path:
        mp = self.config.get("memory", {}).get("manual_path", "data/chiguo_memories.json")
        p = Path(mp)
        if p.is_absolute():
            return p
        return self.anchored(mp)

    def load(self):
        """从磁盘加载状态并应用到 owner。优先读正式文件，不存在则从 .tmp 恢复。
        .bak 损坏回退链 + 审计一致：损坏任一 → 删除主文件，下次 save 重建。"""
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
                self.apply_loaded_data(data)
                restored = True
            except Exception as e:
                if bak.exists():
                    try:
                        data = json.loads(bak.read_text())
                        self.apply_loaded_data(data)
                        self.save(_backup=False, _increment_tick=False)
                        restored = True
                        self.audit("state_recovered_from_bak", str(e))
                    except Exception as e2:
                        self.audit("state_bak_also_corrupt", str(e2))
                else:
                    self.audit("state_corrupted", str(e))

                if not restored:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                    self.owner.last_tick = None
        else:
            self.owner.last_tick = None

        if not restored and self.owner.last_tick is None:
            self.audit("state_fresh_start", "no state file found")

        self._load_memories()

    def _load_memories(self):
        """加载独立 memories 文件（data/chiguo_memories.json，形状防御）。
        加载后回写持久化的 reminder 去重标记（Q7#260，T2 语义移植）。"""
        mp = self.memories_path
        if mp.exists():
            try:
                data = json.loads(mp.read_text())
                self.owner.memories = ([m for m in data if isinstance(m, dict)]
                                       if isinstance(data, list) else [])
            except Exception:
                self.owner.memories = []
        else:
            self.owner.memories = []
        self.owner._apply_memory_dedup()

    def _cas_tick_seq(self, p: Path, owner=None) -> bool:
        """CAS tick_seq: 读磁盘领先则跳升，降级且磁盘更新则 abort。返回 True 続行/False abort。纯 helper 可测。"""
        o = owner or self.owner
        disk_seq = None
        try:
            with open(p, "r", encoding="utf-8") as _f:
                v = json.load(_f).get("tick_seq")
            if isinstance(v, int):
                disk_seq = v
        except Exception:
            logging.debug("F-A16-01 disk_seq 读取失败: %s", __import__('traceback').format_exc(), exc_info=False)
        if self._lock_degraded and disk_seq is not None and disk_seq > o.tick_seq:
            print(f"[chiguo_state] save 放弃：state_lock 降级且磁盘已更新到 tick_seq={disk_seq}(内存={o.tick_seq})", file=sys.stderr)
            self._audit("save_degraded_abort", f"disk_seq={disk_seq} in_mem_seq={o.tick_seq}")
            return False
        if disk_seq is not None and disk_seq > o.tick_seq:
            o.tick_seq = disk_seq + 1
        o.tick_seq += 1
        return True

    def _backup_state(self, p: Path, bak_path: Path) -> None:
        # Atomic 0600: copy via tmp with 0600 then replace, no chmod window.
        try:
            data = p.read_bytes()
        except OSError:
            return
        # Reuse atomic_write for 0600 tmp→replace semantics.
        try:
            from chiguo_atomic import atomic_write
            atomic_write(bak_path, data, mode=0o600)
        except OSError:
            # Fallback: best-effort copy2 + chmod if atomic_write fails.
            try:
                shutil.copy2(str(p), str(bak_path))
            except OSError:
                pass
            try:
                os.chmod(bak_path, 0o600)
            except OSError:
                pass

    def _read_disk_owner(self, p: Path) -> str | None:
        try:
            with open(p, "r", encoding="utf-8") as _f:
                v = json.load(_f).get("owner")
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            pass
        return None

    def save(self, _backup: bool = True, _increment_tick: bool = True) -> bool:
        """原子写入编排：锁→备份→OWNER校验→CAS→payload→checksum→atomic_write。"""
        p = self.state_path
        bak_path = Path(str(p) + ".bak")
        lock_path = str(p) + ".lock"
        acquired = self._lock_acquire(lock_path)
        if not acquired and not self.in_lock():
            if self._lock_degraded:
                # T14 巩固：降级进入时若本轮仍拿不到锁（对端仍持锁），
                # 尝试读磁盘 CAS 审计分支，保证 save_degraded_abort
                # 与 disk_seq>mem 组合审计 100% 覆盖（early abort 亦落审计）。
                try:
                    with open(p, "r", encoding="utf-8") as _f:
                        _disk = json.load(_f).get("tick_seq")
                    if isinstance(_disk, int) and _disk > self.owner.tick_seq:
                        self._audit("save_degraded_abort", f"early_abort disk_seq={_disk} in_mem_seq={self.owner.tick_seq}")
                except Exception:
                    pass
            return False
        try:
            if _backup and p.exists():
                self._backup_state(p, bak_path)
            if p.exists():
                cfg_owner = _config_owner(self.config)
                disk_owner = self._read_disk_owner(p)
                if _check_owner_mismatch(cfg_owner, disk_owner):
                    print(f"[chiguo_state] save 拒绝：owner 越权 config={cfg_owner!r} disk={disk_owner!r}", file=sys.stderr)
                    self._audit("owner_mismatch", f"config_owner={cfg_owner!r} disk_owner={disk_owner!r}")
                    return False
            if _increment_tick and not self._cas_tick_seq(p):
                return False
            payload = self._build_payload()
            payload["_checksum"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            data = json.dumps(payload, indent=2, ensure_ascii=False)
            def _verify_tmp(t):
                try:
                    _v = json.loads(Path(t).read_text())
                except OSError as _e:
                    raise ValueError("tmp validation failed: unreadable") from _e
                if not isinstance(_v, dict) or "_version" not in _v:
                    raise ValueError("tmp validation failed: not a dict or no _version")
            try:
                atomic_write(p, data, mode=0o600, fsync=True, verify=_verify_tmp)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"[chiguo_state] save skipped: tmp 校验失败，不替换好状态: {e}", file=sys.stderr)
                return False
        except OSError as e:
            print(f"[chiguo_state] save failed: {e}", file=sys.stderr)
            return False
        finally:
            if acquired:
                self._lock_release(lock_path)
        return True

    def _build_payload(self) -> dict:
        """从 owner 各可变子对象组装序列化载荷（不含 _checksum）。"""
        o = self.owner
        payload = {
            "_version": self.STATE_VERSION,
            "emotion": asdict(o.emotion),
            "cooldown": asdict(o.cooldown),
            "circadian": asdict(o.circadian),
            "pending_topics": o.pending_topics,
            "personality": personality_to_dict(o.personality),
            "personality_baseline": dict(o.personality._baseline),
            "personality_history": o.personality_history,
            "last_tick": datetime.now(CST).isoformat(),
            "mono_anchor": time_module.monotonic(),
            "wall_anchor": datetime.now(CST).isoformat(),
            "tick_seq": o.tick_seq,
            "owner": (_config_owner(self.config) if _config_owner(self.config) is not None else getattr(o, "_state_owner", None)),
        }
        if o._bayesian_estimator is not None or o._bayesian_restored:
            payload["bayesian"] = (o.bayesian_estimator.to_state_dict()
                                   if o._bayesian_estimator is not None
                                   else o._bayesian_restored)
        dedup_payload = {
            _memory_dedup_key(m): m["last_triggered_at"]
            for m in o.memories
            if isinstance(m, dict) and m.get("last_triggered_at")
        }
        if dedup_payload:
            payload["memory_dedup"] = dedup_payload
        return payload

    def _hydrate_emotion(self, o, data: dict) -> None:
        """纯段：emotion 字段过滤+强转→ChiguoEmotion。可测。"""
        emo_fields = {k: v for k, v in data.get("emotion", {}).items() if k in ChiguoEmotion.__dataclass_fields__}
        o.emotion = ChiguoEmotion(**_coerce_dataclass_fields(emo_fields, ChiguoEmotion))

    def _hydrate_cooldown(self, o, data: dict) -> None:
        """纯段：cooldown 字段过滤+强转+迁移。可测。"""
        cd = {k: v for k, v in data.get("cooldown", {}).items() if k in CooldownState.__dataclass_fields__}
        cd = _coerce_dataclass_fields(cd, CooldownState)
        if cd.get("accumulated_lambda") is None:
            cd["accumulated_lambda"] = 0.0
        o.cooldown = CooldownState(**cd)
        if not o.cooldown.crash_timestamps and o.cooldown.last_crash_at:
            o.cooldown.crash_timestamps = [o.cooldown.last_crash_at]
        if o.cooldown.trigger_history and not o.cooldown.event_timestamps and o.cooldown.last_message_at:
            try:
                base = datetime.fromisoformat(o.cooldown.last_message_at)
                n = len(o.cooldown.trigger_history)
                for i, t in enumerate(o.cooldown.trigger_history):
                    approx = base - timedelta(hours=(n - i) * 24 / max(n, 1))
                    o.cooldown.event_timestamps.append({"type": t, "time": approx.isoformat()})
            except (ValueError, TypeError):
                pass

    def _hydrate_circadian(self, o, data: dict) -> None:
        """纯段：circadian 字段过滤+迁移+同步。可测。"""
        cf = {k: v for k, v in (data.get("circadian") or {}).items() if k in CircadianTracker.__dataclass_fields__}
        o.circadian = CircadianTracker(**cf)
        self.migrate_circadian_v8()
        o._sync_quiet_window()

    def _hydrate_meta(self, o, data: dict) -> None:
        pending = data.get("pending_topics")
        o.pending_topics = pending if isinstance(pending, list) else []
        o.last_tick = data.get("last_tick")
        mono = data.get("mono_anchor")
        o.mono_anchor = mono if isinstance(mono, (int, float)) and not isinstance(mono, bool) else None
        wall = data.get("wall_anchor")
        o.wall_anchor = wall if (isinstance(wall, str) and wall) else None
        if o.mono_anchor is not None and o.wall_anchor is not None and time_module.monotonic() < o.mono_anchor:
            self._audit("state_anchor_regression", f"mono_anchor={o.mono_anchor:.2f} > current {time_module.monotonic():.2f}")
            o.mono_anchor = time_module.monotonic()
            o.wall_anchor = datetime.now(CST).isoformat()
        o.tick_seq = data.get("tick_seq", 0)
        pers_data = data.get("personality")
        if pers_data:
            o.personality = personality_from_dict(pers_data)
            sb = data.get("personality_baseline")
            if isinstance(sb, dict) and sb:
                o.personality.reset_baseline(sb)
            else:
                o.personality.reset_baseline(dict(o._personality_initial_baseline))
        else:
            o.personality.tsundere_intensity = data.get("emotion", {}).get("tsundere_index", 70.0)
        ph = data.get("personality_history")
        o.personality_history = ph if isinstance(ph, list) else []
        o._bayesian_restored = data.get("bayesian")
        dedup = data.get("memory_dedup")
        o._memory_dedup = dict(dedup) if isinstance(dedup, dict) else {}
        raw_owner = data.get("owner")
        o._state_owner = raw_owner.strip() if isinstance(raw_owner, str) and raw_owner.strip() else None

    def apply_loaded_data(self, data: dict):
        """解析并应用状态数据：校验和→版本门禁→3 hydrate 段→meta。可测分段编排。"""
        o = self.owner
        stored = data.pop("_checksum", None)
        if stored:
            recomputed = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if recomputed != stored:
                self._audit("checksum_mismatch", f"stored={stored[:12]}... computed={recomputed[:12]}...")
                raise ValueError("checksum mismatch — refusing to load, falling back to .bak")
        ver = data.get("_version")
        if isinstance(ver, int) and ver > self.STATE_VERSION:
            self._audit("state_future_version", f"stored={ver} current={self.STATE_VERSION} loading anyway")
        self._hydrate_emotion(o, data)
        self._hydrate_cooldown(o, data)
        self._hydrate_circadian(o, data)
        self._hydrate_meta(o, data)

    def migrate_circadian_v8(self):
        """v8 双作息迁移(幂等,加载时执行一次)：
        ① reply_days/active_days 无 bucket 条目 → 按日期启发式补桶(调休优先 → 节假日 → 周几,解析失败丢弃);
        ② 旧单桶窗口迁移到 weekday_*:若 weekday_* 与 weekend_* 均为默认且旧 confidence > 0
           → 继承旧 quiet_*(weekend 非默认说明是 v8 风格状态,兼容字段只是当前生效桶快照,不迁移)。
        """
        o = self.owner
        for key in ("reply_days", "active_days"):
            days = getattr(o.circadian, key, None)
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
                hp = o.holiday_parser
                if hp is not None and hp.is_makeup_workday(dt):
                    d["bucket"] = "weekday"
                elif hp is not None and hp.is_holiday(dt):
                    d["bucket"] = "weekend"
                else:
                    d["bucket"] = "weekday" if dt.weekday() < 5 else "weekend"
                migrated.append(d)
            setattr(o.circadian, key, migrated)
        try:
            legacy_conf = float(o.circadian.confidence)
        except (ValueError, TypeError):
            legacy_conf = 0.0
        if (o.circadian.weekday_quiet_start == 0
                and o.circadian.weekday_quiet_end == 8
                and o.circadian.weekday_confidence == 0.0
                and o.circadian.weekend_quiet_start == 0
                and o.circadian.weekend_quiet_end == 8
                and o.circadian.weekend_confidence == 0.0
                and legacy_conf > 0):
            o.circadian.weekday_quiet_start = o.circadian.quiet_start
            o.circadian.weekday_quiet_end = o.circadian.quiet_end
            o.circadian.weekday_confidence = legacy_conf

    def _audit(self, event: str, detail: str = ""):
        """v5: 状态损坏审计日志。追加到 chiguo_state_audit.jsonl。v6: 路径锚定。"""
        try:
            audit_path = self.anchored("chiguo_state_audit.jsonl")
            entry = {
                "event": event,
                "time": datetime.now(CST).isoformat(),
                "detail": detail,
            }
            with open(audit_path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # audit 失败不影响主流程

    def audit(self, event: str, detail: str = ""):
        """公开审计入口（daemon/核心公共 API 用）。"""
        self._audit(event, detail)

    def _lock_acquire(self, lock_path: str) -> bool:
        """获取进程级独占锁（可重入，delegate → chiguo_locks.acquire）。
        返回 True 表示本次真正获得锁（需配套 release）。非 POSIX 或 5s 内
        拿不到锁 → 降级无锁并审计（超时回调）。"""
        return locks.acquire(
            lock_path,
            on_timeout=lambda lp: self._audit("state_lock_timeout", lp),
        )

    def _lock_release(self, lock_path: str):
        """释放锁（仅持有者调用）。delegate → chiguo_locks.release。"""
        locks.release(lock_path)

    def lock_acquire(self, lock_path: str) -> bool:
        """公开跨进程锁原语（T11·Q1：核心类/委托走公开 API，不强闯私有）。"""
        return self._lock_acquire(lock_path)

    def lock_release(self, lock_path: str):
        """公开跨进程锁释放原语（T11·Q1）。"""
        self._lock_release(lock_path)

    @contextmanager
    def state_lock(self):
        """持有 state 文件的跨进程独占锁（chiguo_state.json.lock）。

        F-A16-01 (#309): yield 出本次是否真正获锁（bool）。5s 超时/非 POSIX
        会降级无锁返回 False，调用方必须感知降级——否则无锁进入 RMW、save 二次
        获取成功后用陈旧快照覆盖并发进程的写入（lost update，组2 复核 CONFIRMED）。
        """
        lock_path = str(self.state_path) + ".lock"
        acquired = self._lock_acquire(lock_path)
        degraded = not acquired and not locks.in_lock(lock_path)
        if degraded:
            self._lock_degraded = True  # 供 save() 降级进入时的重读校验（#309）
        try:
            yield acquired
        finally:
            if acquired:
                self._lock_release(lock_path)
            if degraded:
                self._lock_degraded = False

    def in_lock(self) -> bool:
        """当前进程是否已持有 state 锁。"""
        return locks.in_lock(str(self.state_path) + ".lock")

    def monotonic_anchor_pair(self) -> tuple[float | None, str | None]:
        """返回持久化的单调锚点对 (mono, wall)，缺失/损坏为 None。"""
        return self.owner.mono_anchor, self.owner.wall_anchor

class ChiguoState:
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
        refresh_schedule_cache(
            str(self._anchored(xlsx_path)),
            str(self._anchored("schedule_cache.json")),
            semester_start=sem_start,
            enabled=bool(sched.get("enabled", True)),
        )

        try:
            self.holiday_parser = HolidayParser(
                data_path=str(self._anchored("holidays.json"))
            )
        except Exception as exc:
            print(f"[warn] HolidayParser 构造失败，节假日判断降级: {exc}", file=sys.stderr)
            self.holiday_parser = None

        base_dir = str(self._anchored("."))
        mem_cfg = config.get("memory", {})
        self.memory_bridge = create_backend(mem_cfg, base_dir=base_dir)
        self.anniversary_mgr = AnniversaryManager(base_dir)
        self.override_store = OverrideStore(base_dir)
        self.plan_store = PlanStore(base_dir)
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

    @property
    def break_state_path(self) -> Path:
        return self._persistence.break_state_path

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
        """是否在假期中（日期区间 / 手动覆盖 / 学期自动结束 / 学期未开始）"""
        data = self._read_break_state()
        if data:
            if data.get("manual_override") or data.get("on_break"):
                return True
        today = datetime.now(CST).date()
        if self._in_break_range(today):
            return True
        if self.semester_start and today < self.semester_start:
            return True
        if self.semester_end and today > self.semester_end:
            return True
        return False

    def _cache_fingerprint(self) -> str:
        """日键缓存失效指纹(§3.3):读路径源文件 mtime 摘要。
        _rc_cache/_scale_cache **同根共用**——任一源文件变更(schedule_overrides /
        schedule_cache / break_state / holidays / schedule_plan)→ 指纹变 →
        当日缓存整体重建,不再"同日仍旧数据"(F-A20-07)。
        config(toml) 变更不在此列:替换 config 不落盘,由 reload_config 显式清缓存兜底。"""
        parts = []
        for name in ("schedule_cache.json", "schedule_overrides.json", "break_state.json",
                     "holidays.json", "schedule_plan.json"):
            p = self._anchored(name)
            try:
                m = p.stat().st_mtime_ns if p.exists() else 0
            except OSError:
                m = 0
            parts.append(f"{name}:{m}")
        return "|".join(parts)

    def _resolved_for(self, now):
        """当日 resolved_classes 共享缓存(消灭 availability/schedule_status 双查询路径,§5.1)
        失效键 = 日期 + 源文件 mtime 指纹:同日改 overrides/课表缓存/寒暑假/节假日 → 自动重建。"""
        from schedule.sources import load_sources
        from schedule.day_plan import resolve_classes
        key = f"{now.date().isoformat()}|{self._cache_fingerprint()}"
        if self._rc_cache.get("key") != key:
            src = load_sources(str(self._anchored(".")), self.config)
            self._rc_cache = {"key": key, "sources": src,
                              "classes": resolve_classes(now.date(), src)}
        return self._rc_cache["sources"], self._rc_cache["classes"]

    def availability(self, now: datetime, user_state: dict = None) -> float:
        """三层重组(§5.1):availability_base → class_load_adjust(idle_school) → bayesian_adjust。
        数值与现码一致;break 判定按 now(修复"真实今天"怪癖,现有断言两值皆可)。"""
        from schedule.day_plan import availability_base, class_load_adjust, bayesian_adjust
        src, rc = self._resolved_for(now)
        res = availability_base(now, src)
        base = res["base"]
        if res["tier"] == "idle_school":
            base = class_load_adjust(base, rc, now)
        return bayesian_adjust(base, user_state, self.emotion, self.config)

    def schedule_status(self, now: datetime) -> dict | None:
        """窄原语 + resolved_classes 组装;None 语义保持(课表不可用);键形状双向兼容(§5.1)。"""
        from schedule.day_plan import resolve_classes, _on_break, current_period, _PERIOD_START
        from schedule.sources import load_sources
        from schedule.query import PERIOD_TIMES
        src, rc = self._resolved_for(now)
        today = now.date() if isinstance(now, datetime) else now

        def _breaks_info():
            data = src.break_state
            if not data:
                return []
            out = []
            for b in data.get("breaks", []):
                try:
                    start = date_type.fromisoformat(b["start"]); end = date_type.fromisoformat(b["end"])
                except (ValueError, KeyError, TypeError):
                    continue
                out.append({"start": b["start"], "end": b["end"], "note": b.get("note", ""),
                            "active": start <= today <= end})
            return out

        on_break = _on_break(src.break_state, src.semester_start, src.semester_end, today)
        if on_break:
            return {"in_class": False, "current_course": None, "class_load": "free",
                    "remaining_classes": 0, "total_classes": 0, "on_break": True,
                    "break_reason": "学期未开始" if (src.semester_start and today < src.semester_start) else
                                    ("学期已结束" if (src.semester_end and today > src.semester_end) else
                                    ("手动无限期开启" if (src.break_state and (src.break_state.get("manual_override")
                                     or src.break_state.get("on_break"))) else "日期区间")),
                    "breaks": _breaks_info()}
        hq = src.holiday.query(now)
        if hq["is_holiday"]:
            return {"in_class": False, "current_course": None, "class_load": "free",
                    "remaining_classes": 0, "total_classes": 0, "holiday": hq["holiday_name"],
                    "holiday_hint": hq["hint"], "on_break": False, "breaks": _breaks_info()}
        if hq["is_weekend"] and not hq["is_makeup_workday"]:
            return {"in_class": False, "current_course": None, "class_load": "free",
                    "remaining_classes": 0, "total_classes": 0, "weekend": True,
                    "on_break": False, "breaks": _breaks_info()}
        if not src.schedule_valid:
            return None  # 课表未启用/无数据/解析异常 → None 语义保持
        active = {p: c for p, c in rc.items() if not c.get("cancelled")}
        cp = current_period(now)
        result = {"in_class": cp in active, "on_break": False, "breaks": _breaks_info()}
        cur = active.get(cp)
        if cur:
            end_h, end_m = map(int, PERIOD_TIMES[cp][1].split(":"))
            end_time = now.replace(hour=end_h, minute=end_m, second=0)
            result["current_course"] = {**cur, "period": cp, "time": PERIOD_TIMES[cp],
                                        "minutes_remaining": max(0, (end_time - now).total_seconds() / 60)}
        else:
            result["current_course"] = None
        future = [p for p in sorted(active) if p > (cp or 0) and _PERIOD_START[p] > now.time()]
        nxt = next((active[p] for p in future), None)
        if nxt is not None:
            result["next_course"] = {**nxt, "period": future[0],
                                     "time": PERIOD_TIMES[future[0]]}
        else:
            result["next_course"] = None
        total = len(active)
        if total == 0:
            load = "free"
        elif total <= 2:
            load = "light"
        elif total <= 5:
            load = "normal"
        else:
            load = "heavy"
        result["class_load"] = load
        result["remaining_classes"] = len(future)
        result["total_classes"] = total
        result["periods_today"] = [dict(c) for c in rc.values()]   # 同形:period 字段在条目内
        if hq["is_makeup_workday"]:
            result["makeup_day"] = True
            result["makeup_reason"] = hq["hint"]
        return result

    def exam_season_now(self, now) -> bool:
        """考试周门面(§5.1):override 区间事实当日命中,唯一来源(exam_week);
        toml 已废弃。引擎层经此,不直触 schedule 模块。"""
        today = now.date() if isinstance(now, datetime) else now
        for it in self.override_store.intervals():
            d = it.get("date")
            if not isinstance(d, str):   # 缺字段/类型非法条目跳过(消费侧防御,#83)
                continue
            try:
                s = date_type.fromisoformat(d)
                e = date_type.fromisoformat(it.get("end_date") or d)
            except (ValueError, TypeError):
                continue
            if s <= today <= e:
                return True
        return False

    def trigger_scale_now(self, now) -> dict:
        """计划文件修饰参数(§5.2):ref → 日期解析,当日命中取 trigger_scale 映射。
        失效键 = 日期 + 源文件 mtime 指纹(与 _rc_cache 同根,§3.3):同日改 plan/override
        → 自动重建;plan 缺失/损坏 → {} 恒等;悬挂 ref → 跳过 + stderr 一次性告警。
        ref 资格(裁决 A/N5):fact:<id> ⟺ kind == "exam_week";holiday:<name> → range_of。"""
        today = now.date() if isinstance(now, datetime) else now
        key = f"{today.isoformat()}|{self._cache_fingerprint()}"
        if getattr(self, "_scale_cache", None) and self._scale_cache.get("key") == key:
            return self._scale_cache["scale"]
        scale: dict = {}
        plan = self.plan_store.load()
        if plan:
            for mod in plan.get("modifiers", []):
                if not isinstance(mod, dict):
                    print(f"[schedule_plan] 非法 modifier(非 dict)已跳过: {mod!r}", file=sys.stderr)
                    continue
                ref = mod.get("ref", "")
                ts = mod.get("trigger_scale", {})
                if not isinstance(ts, dict):
                    continue
                if ref.startswith("fact:"):
                    item = self.override_store.by_id(ref[5:])
                    d = item.get("date") if item else None
                    if item is None or item.get("kind") != "exam_week" or not isinstance(d, str):
                        print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                        continue
                    try:
                        s = date_type.fromisoformat(d)
                        e = date_type.fromisoformat(item.get("end_date") or d)
                    except (ValueError, TypeError):
                        print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                        continue
                    if not (s <= today <= e):
                        continue
                elif ref.startswith("holiday:"):
                    r = (self.holiday_parser.range_of(ref[len("holiday:"):])
                         if self.holiday_parser else None)
                    if r is None:
                        print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                        continue
                    if not (r[0] <= today <= r[1]):
                        continue
                else:
                    print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                    continue
                scale.update(ts)   # 同日多 modifier:文件序后写覆盖(计划决策)
        self._scale_cache = {"key": key, "scale": scale}
        return scale

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
            except Exception:
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
