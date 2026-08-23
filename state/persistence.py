"""state.persistence — StatePersistence 原子读写（AUD-001/010）。"""

import hashlib
import json
import logging
import os
import shutil
import sys
import time as time_module
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import chiguo_locks as locks
import chiguo_atomic as _chiguo_atomic

atomic_write = _chiguo_atomic.atomic_write
from chiguo_state_models import ChiguoEmotion, CooldownState, _coerce_dataclass_fields, _memory_dedup_key
from chiguo_personality import personality_to_dict, personality_from_dict
from chiguo_circadian import CircadianTracker
from chiguo_time import CST
from state.ownership import _config_owner, _check_owner_mismatch


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
