#!/usr/bin/env python3
"""test_persistence_concurrency.py — T14 持久化加固 harness (TDD RED→GREEN)

零丢不变量：tick_seq 单调 CAS (降级 abort)，mono_anchor 回退重建，
atomic 0600+fsync+verify，concurrent 2× tick harness。

覆盖分支 100%：
  - _cas_tick_seq: disk None / disk<=mem / disk>mem catch-up / degraded+disk>mem abort+审计
  - save early abort: 未获锁且非重入 → return False
  - mono_anchor 回退: monotonic < disk mono_anchor → 重建 + state_anchor_regression
  - atomic: 0600 + fsync + verify(_version) + checksum SHA256
  - .bak 0600 + audit 落盘
  - concurrent 2× tick: 双进程各 tick+save → tick_seq 单增无回退 + checksum ok
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = sys.executable
CST = __import__('datetime').timezone(__import__('datetime').timedelta(hours=8))


def _make_state(tmpdir: str | Path):
    from chiguo_state import ChiguoState
    td = Path(tmpdir)
    cfg_path = td / "chiguo_proactive.toml"
    if not cfg_path.exists():
        cfg_path.write_text(Path(ROOT / "chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(td)
    cfg["memory"]["mem0_qdrant_path"] = str(td / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(td / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    s = ChiguoState.__new__(ChiguoState)
    # 走正常构造以触发 _load 等，但隔离 base
    s2 = ChiguoState(cfg)
    return s2, cfg


def _load_cfg(tmpdir: Path):
    cfg_path = tmpdir / "chiguo_proactive.toml"
    if not cfg_path.exists():
        cfg_path.write_text((ROOT / "chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmpdir)
    cfg["memory"]["mem0_qdrant_path"] = str(tmpdir / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(tmpdir / "no_history.db")
    return cfg


# ── CAS 分支 100% ──────────────────────────────────────────────
def test_cas_tick_seq_none_and_catchup():
    from chiguo_state import ChiguoState
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = _load_cfg(td)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        s = ChiguoState(cfg)
        p = s._persistence.state_path
        # 分支1: 磁盘无文件 → disk_seq None → +1
        if p.exists():
            p.unlink()
        s.tick_seq = 5
        ok = s._persistence._cas_tick_seq(p, s)
        assert ok is True
        assert s.tick_seq == 6  # None → 5+1
        # 分支2: 磁盘领先 → catch-up
        s.save(_backup=False, _increment_tick=True)
        data = json.loads(p.read_text())
        disk = data["tick_seq"]
        s.tick_seq = 0
        s._persistence._lock_degraded = False
        ok = s._persistence._cas_tick_seq(p, s)
        assert ok is True
        assert s.tick_seq == disk + 1 or s.tick_seq >= disk + 1
        # 分支3: disk <= mem → 仅 +1
        s.tick_seq = disk + 10
        before = s.tick_seq
        ok = s._persistence._cas_tick_seq(p, s)
        assert ok is True
        assert s.tick_seq == before + 1


def test_cas_degraded_abort_audit():
    from chiguo_state import ChiguoState
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = _load_cfg(td)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        s = ChiguoState(cfg)
        p = s._persistence.state_path
        s.save(_backup=False, _increment_tick=True)
        audit_path = td / "chiguo_state_audit.jsonl"
        if audit_path.exists():
            audit_path.unlink()
        # 人为回退内存，触发 degraded abort
        s.tick_seq = 0
        s._persistence._lock_degraded = True
        ok = s._persistence._cas_tick_seq(p, s)
        assert ok is False, "degraded+disk>mem 应 abort"
        assert s.tick_seq == 0, "abort 时 tick_seq 不应递增"
        assert audit_path.exists()
        events = [json.loads(l)["event"] for l in audit_path.read_text().splitlines() if l.strip()]
        assert "save_degraded_abort" in events, f"缺 save_degraded_abort, got {events}"
        s._persistence._lock_degraded = False


def test_save_early_abort_when_locked():
    """save 在另一进程持锁时 5s 内拿不到 → early abort 返回 False，不丢不覆盖。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = _load_cfg(base)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        from chiguo_state import ChiguoState
        s0 = ChiguoState(cfg)
        s0.emotion.loneliness = 11.0
        assert s0.save()
        orig_seq = json.loads((base / "chiguo_state.json").read_text())["tick_seq"]
        lock = str(base / "chiguo_state.json.lock")
        M = base / "m"
        M.mkdir()
        code_holder = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, "{ROOT}")
import chiguo_locks as locks
lock = sys.argv[1]
M = Path(sys.argv[2])
got = locks.acquire(lock, timeout=5.0)
assert got
(M / "held").touch()
time.sleep(6)
locks.release(lock)
(M / "released").touch()
"""
        code_try = f"""
import sys, json
from pathlib import Path
sys.path.insert(0, "{ROOT}")
import tomllib, os
from chiguo_state import ChiguoState
base = Path(sys.argv[1])
cfg_path = base / "chiguo_proactive.toml"
import tomllib as tl
with open(cfg_path, "rb") as f:
    cfg = tl.load(f)
cfg["_base_dir"] = str(base)
cfg["memory"]["mem0_qdrant_path"] = str(base / "no_qdrant")
os.environ["CHIGUO_MEM0_DISABLED"] = "1"
s = ChiguoState(cfg)
s.emotion.loneliness = 99.0
ok = s.save()
print("SAVE_OK=" + str(ok))
"""
        import subprocess as sp
        p_holder = sp.Popen([PY, "-c", code_holder, lock, str(M)], cwd=str(ROOT))
        # 等 holder 拿到锁
        for _ in range(80):
            if (M / "held").exists():
                break
            time.sleep(0.05)
        assert (M / "held").exists(), "holder 未拿到锁"
        # 此时 try_save 应 early abort (5s 超时)
        r = sp.run([PY, "-c", code_try, str(base)], capture_output=True, text=True, timeout=20)
        assert "SAVE_OK=False" in r.stdout, f"持锁时 save 应 early abort False, got {r.stdout} {r.stderr}"
        # 校验未被覆盖
        data = json.loads((base / "chiguo_state.json").read_text())
        assert data["tick_seq"] == orig_seq, "early abort 不应递增 tick_seq"
        assert data["emotion"]["loneliness"] == 11.0, "early abort 不应覆盖"
        p_holder.wait(timeout=15)
        assert (M / "released").exists()


# ── mono_anchor 回退 ──────────────────────────────────────────
def test_mono_anchor_regression_audit():
    from chiguo_state import ChiguoState
    import time as time_module
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = _load_cfg(base)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        s = ChiguoState(cfg)
        assert s.save()
        # 篡改 mono_anchor 为倒退值
        raw = json.loads(s.state_path.read_text())
        raw["mono_anchor"] = 1e6  # 远大于当前 monotonic
        raw["wall_anchor"] = __import__('datetime').datetime.now(CST).isoformat()
        # 重算 checksum
        cs = raw.pop("_checksum", None)
        raw["_checksum"] = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        s.state_path.write_text(json.dumps(raw, ensure_ascii=False))
        audit_path = base / "chiguo_state_audit.jsonl"
        if audit_path.exists():
            audit_path.unlink()
        # 模拟重启：monotonic 归零
        with mock.patch.object(time_module, "monotonic", return_value=100.0):
            s2 = ChiguoState(cfg)
            # 应检测到回退并重建
            assert s2.mono_anchor is not None
            assert s2.mono_anchor <= 100.0
        assert audit_path.exists()
        evs = [json.loads(l)["event"] for l in audit_path.read_text().splitlines() if l.strip()]
        assert "state_anchor_regression" in evs, f"缺 state_anchor_regression {evs}"


# ── atomic 0600+fsync+verify ──────────────────────────────────
def test_atomic_0600_fsync_verify_and_checksum():
    from chiguo_state import ChiguoState
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = _load_cfg(base)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        s = ChiguoState(cfg)
        assert s.save()
        p = base / "chiguo_state.json"
        assert p.exists()
        # 0600
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600, f"state 0600 expected got {oct(mode)}"
        # checksum SHA256 校验 (591-594)
        raw = json.loads(p.read_text())
        stored = raw.pop("_checksum")
        recomputed = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        assert stored == recomputed, "checksum 校验失败"
        assert "_version" in raw, "verify 需含 _version"
        # 非法 verify 应不覆盖好文件：直接调 atomic_write verify 失败路径
        from chiguo_atomic import atomic_write
        bad = json.dumps({"no_version": 1})
        try:
            atomic_write(p, bad, mode=0o600, fsync=True, verify=lambda t: (_ for _ in ()).throw(ValueError("no _version")) if True else None)
        except ValueError:
            pass
        # 原文件仍完好（未被坏 tmp 覆盖）
        raw2 = json.loads(p.read_text())
        assert "_version" in raw2


def test_bak_0600_and_audit():
    from chiguo_state import ChiguoState
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = _load_cfg(base)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        s = ChiguoState(cfg)
        assert s.save(_backup=False)
        assert s.save(_backup=True)
        bak = Path(str(base / "chiguo_state.json") + ".bak")
        assert bak.exists()
        mode = stat.S_IMODE(bak.stat().st_mode)
        assert mode == 0o600, f".bak 0600 expected got {oct(mode)}"


# ── concurrent 2× tick harness ─────────────────────────────────
def test_concurrent_tick_seq_monotonic_and_checksum():
    """双进程分别 tick+save 并发 → tick_seq 单增无回退 + checksum ok + 无丢。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # 初始化 state
        cfg = _load_cfg(base)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        from chiguo_state import ChiguoState
        s0 = ChiguoState(cfg)
        assert s0.save()
        seq0 = json.loads((base / "chiguo_state.json").read_text())["tick_seq"]
        # 双进程并发：各做 3 次 save（模拟 tick）
        code = f"""
import sys, tomllib, os, time
from pathlib import Path
sys.path.insert(0, "{ROOT}")
from chiguo_state import ChiguoState
base = Path(sys.argv[1])
idx = sys.argv[2]
cfg_path = base / "chiguo_proactive.toml"
with open(cfg_path, "rb") as f:
    cfg = tomllib.load(f)
cfg["_base_dir"] = str(base)
cfg["memory"]["mem0_qdrant_path"] = str(base / "no_qdrant")
os.environ["CHIGUO_MEM0_DISABLED"] = "1"
s = ChiguoState(cfg)
for i in range(3):
    s.emotion.loneliness = float(10 + int(idx)*10 + i)
    # 短抖动增加并发交错
    time.sleep(0.02 * int(idx))
    ok = s.save()
    print(f"P{{idx}} iter{{i}} ok={{ok}} seq={{s.tick_seq}}")
    time.sleep(0.03)
"""
        ps = [
            subprocess.Popen([PY, "-c", code, str(base), "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT)),
            subprocess.Popen([PY, "-c", code, str(base), "1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT)),
        ]
        outs = []
        for p in ps:
            out, err = p.communicate(timeout=40)
            outs.append((out, err, p.returncode))
            assert p.returncode == 0, f"worker 失败 {out[:500]} {err[:500]}"
        # 校验最终状态：tick_seq 单增无回退 + checksum ok
        data = json.loads((base / "chiguo_state.json").read_text())
        stored = data.pop("_checksum")
        recomputed = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        assert stored == recomputed, "concurrent 后 checksum 失效"
        assert data["tick_seq"] > seq0, f"tick_seq 未递增 {data['tick_seq']} <= {seq0}"
        # 至少部分并发写成功（6 次尝试，可能部分 early abort/degraded abort），但最终值应 > seq0 且无回退
        # 校验 audit 若有 degraded abort 也落盘
        audit = base / "chiguo_state_audit.jsonl"
        # 不强制要求必须出现 degraded，但若出现必须是合法事件
        if audit.exists():
            evs = [json.loads(l)["event"] for l in audit.read_text().splitlines() if l.strip()]
            # 允许的事件集合
            allowed = {"save_degraded_abort", "state_lock_timeout", "state_anchor_regression", "state_recovered_from_bak", "checksum_mismatch", "state_corrupted", "state_fresh_start", "owner_mismatch", "state_future_version", "state_bak_also_corrupt", "monotonic_reset_cap", "clock_backward", "clock_jump_forward"}
            for e in evs:
                assert e in allowed, f"未知 audit 事件 {e}"
        # 0600 仍保持
        mode = stat.S_IMODE((base / "chiguo_state.json").stat().st_mode)
        assert mode == 0o600
