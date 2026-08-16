#!/usr/bin/env python3
"""test_state_lock_degradation.py — F-A16-01: state_lock 5s 超时降级无锁 → 双进程 RMW lost update

回归测试围绕 Issue #309（R3 state_lock 降级 lost update + 锁内 IO 收口）：

  机制（组2 复核 CONFIRMED，E1 复现）：
  - chiguo_locks.acquire 5s 超时降级无锁返回 False；
  - state_lock() 的 yield 不透传 acquired → 调用方无感知继续 RMW；
  - A 持锁 >5s 写 loneliness=99，B 的 state_lock 降级读到旧值 50，
    修改后在 A release 后二次 acquire 成功 → 用陈旧快照覆盖 A（lost update）。

  tests/test_state_lock_degradation.py 用两个真实进程（真实 chiguo_locks +
  ChiguoState，tempdir 隔离）复现该时序：

  ✓ 降级回归测试（本文件核心，修复前红）：断言 A 的写入不被降级进入的 B 覆盖
  ✓ 正常持锁路径对照测试（修复前后均绿）：持锁 RMW 正常落盘

  测试时间预算：降级测试需 A 持锁 >5s（> LOCK_TIMEOUT_S=5.0 阈值）故约占 6s，
  全文件 < 10s。全部 tempdir 隔离，不触碰真实运行时文件。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = sys.executable  # pytest 下即 .venv 解释器


def _make_state(base: Path):
    """建立初始状态：loneliness=50, messages_without_reply=0。"""
    from chiguo_state import ChiguoState
    cfg = {"_base_dir": str(base), "emotion": {},
           "memory": {"manual_path": str(base / "m.json")}}
    st = ChiguoState(cfg)
    st.emotion.loneliness = 50.0
    st.cooldown.messages_without_reply = 0
    assert st.save(), "初始 save 失败"


def _env():
    e = dict(os.environ)
    e["PYTHONPATH"] = str(ROOT)
    e.pop("CHIGUO_MEM0_DISABLED", None)
    return e


# 进程 B 的 state_lock 降级进入路径。锁超时用默认 LOCK_TIMEOUT_S=5.0（真实验证
# 5s 降级机制），因此 A 必须持锁 >5s。marker 精确同步保证确定性：
#   a_start   A 已获锁、可进场
#   b_loaded  B 已在降级锁定区内 _load 读到（陈旧的）磁盘快照
#   a_released A 已落盘 99 并释放锁
# 子进程代码模板：用 @@ROOT@@ 占位（子进程体内含 %s 等占位符，不能再用 % 插值外层）
_CODE_A = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, "@@ROOT@@")
base, lock, M = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
import chiguo_locks as locks
from chiguo_state import ChiguoState
cfg = {"_base_dir": str(base), "emotion": {}, "memory": {"manual_path": str(base/"m.json")}}
st = ChiguoState(cfg); st.load()
t0 = time.monotonic()
got = locks.acquire(lock, timeout=5.0)
print("[A] acquired=%s" % got, flush=True)
assert got, "A 拿不到锁"
st._load()
(M/"a_start").touch()                 # B 可进场
# 等 B 在降级锁定区读毕陈旧快照，A 再写——保证 B 读的是 99 落盘前的旧值
while not (M/"b_loaded").exists():
    time.sleep(0.03)
st.emotion.loneliness = 99.0
st.cooldown.messages_without_reply = 3   # A 也更新 cooldown
time.sleep(0.3)                          # 余量：确保 B 已因超时降级
ok = st.save()                           # 同进程重入,仍持锁
speed = time.monotonic() - t0
print("[A] save_ok=%s hold=%.2fs" % (ok, speed), flush=True)
locks.release(lock)
(M/"a_released").touch()
print("[A] released", flush=True)
"""

_CODE_B = r"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, "@@ROOT@@")
base, lock, M = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
from chiguo_state import ChiguoState
cfg = {"_base_dir": str(base), "emotion": {}, "memory": {"manual_path": str(base/"m.json")}}
st = ChiguoState(cfg); st.load()
while not (M/"a_start").exists():
    time.sleep(0.03)
r = {}   # 透传读到的当前值给父进程
t0 = time.monotonic()
with st.state_lock():          # A 持锁中 → 5s 超时降级 acquired=False
    r["wait"] = round(time.monotonic() - t0, 2)
    st._load()                 # A 尚未落盘 → 读旧值
    r["read_lon"] = st.emotion.loneliness
    st.cooldown.messages_without_reply = 7   # B 的 modify（覆写触感）
    (M/"b_loaded").touch()     # A 得知 B 已读毕陈旧快照
    while not (M/"a_released").exists():
        time.sleep(0.03)
    r["save_ok"] = st.save()   # A 已释放 → 二次 acquire 成功 → 陈旧快照覆盖
print("[B] " + json.dumps(r), flush=True)
"""


def _run_pair(base: Path):
    """A/B 两个真实进程并发跑；返回 (a_out, b_out)。"""
    lock = str(base / "chiguo_state.json.lock")
    M = base / "m"; M.mkdir()
    code_a = _CODE_A.replace("@@ROOT@@", str(ROOT))
    code_b = _CODE_B.replace("@@ROOT@@", str(ROOT))
    procs = [
        subprocess.Popen([PY, "-c", code_a, str(base), lock, str(M)],
                         cwd=str(ROOT), env=_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True),
        subprocess.Popen([PY, "-c", code_b, str(base), lock, str(M)],
                         cwd=str(ROOT), env=_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True),
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=40)
        outs.append((out.strip(), err.strip().splitlines()[-2:]))
    return outs


def _final_lon_mwr(base: Path):
    data = json.loads((base / "chiguo_state.json").read_text())
    return data["emotion"]["loneliness"], data.get("cooldown", {}).get("messages_without_reply")


def test_state_lock_degradation_preserves_prior_writer():
    """核心回归：降级进入的 B 不得用陈旧快照覆盖先写者的落盘更新。

    修复前（main）红：B 二次 acquire 成功后用旧快照覆盖 A 的 loneliness=99
    → 断言 loneliness==99 失败。
    修复后绿：save() 感知降级进入 + 重读校验磁盘较新 → 放弃本次写 → 保留 99。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_state(base)
        (a_out, _a_err), (b_out, _b_err) = _run_pair(base)
        # B 的 state_lock 确实经历了超时降级（降级路径读到的是 A 落盘前的旧值）
        assert 'read_lon' in b_out, f"B 未完成降级路径: {b_out!r}"
        lon, mwr = _final_lon_mwr(base)
        # 核心断言：A（先写者）的修改必须保留，不被降级进入的 B 覆盖
        assert lon == 99.0, (
            f"lost update: A 的 loneliness=99 被降级进入的 B 覆盖 → disk={lon}. "
            f"A:{a_out} B:{b_out}"
        )
        assert mwr == 3, (
            f"B 的覆写应被放弃(防 lost update) → messages_without_reply={mwr} "
            f"(应保留 A 写入的 3)。A:{a_out} B:{b_out}"
        )


def test_state_lock_normal_path_persists_change():
    """对照：正常持锁路径行为不变——持锁 RMW 后修改应正常落盘。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_state(base)
        from chiguo_state import ChiguoState
        cfg = {"_base_dir": str(base), "emotion": {},
               "memory": {"manual_path": str(base / "m.json")}}
        st = ChiguoState(cfg)
        st.load()
        # 先进锁区（正常拿到锁），重载最新状态再修改并保存
        with st.state_lock():
            st._load()
            st.emotion.loneliness = 42.0
            ok = st.save()
        assert ok, "正常持锁路径 save 应成功"
        st2 = ChiguoState(cfg)
        st2.load()
        assert st2.emotion.loneliness == 42.0, "正常持锁 RMW 修改应持久化"
        assert st2.cooldown.messages_without_reply == 0, "其余字段应保持不变"


def _main():
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))


if __name__ == "__main__":
    _main()
