#!/usr/bin/env python3
"""test_daemon_fixes.py — chiguo_daemon.py 两个回归 bug 的测试（TDD）

Bug 1: send 决策里「3.5 记录触发历史」代码块复制粘贴残留执行两次，
      cooldown.trigger_history 对同一 trigger.type 追加两次 → 话题去重/多样性
      失真、_build_context 的 recent_lonely 计数翻倍（force_topic_threshold
      实际 1.5 次 lonely 触发就强制注入话题）。
Bug 2: _tick cron 模式情绪全量重放——推进基准只用「最后消息时间」，未用
      state 已持久化的 last_tick；cron 每 15 分钟起新进程（_monotonic_at_save=0
      单调防护失效）→ 每轮都按自最后消息以来的全量 elapsed 调用非幂等
      state.tick(hours)，情绪以设计速率 ~33 倍重复累积。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

import decision.core  # noqa: E402  # T10·Q2: daemon 拆包后 evaluate/tick 时钟在 decision.core
from chiguo_daemon import DecisionEngine
from chiguo_math import decay, elastic_recover

TMP_DIR: Path | None = None


def setup() -> Path:
    """复制 toml 到临时目录（隔离 state/log），返回 cfg 路径。"""
    global TMP_DIR
    TMP_DIR = Path(tempfile.mkdtemp(prefix="chiguo_test_daemon_fixes_"))
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{TMP_DIR / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{TMP_DIR / "no_history.db"}"', src)
    cfg_path = TMP_DIR / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return cfg_path


def teardown():
    global TMP_DIR
    if TMP_DIR is not None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR = None


@pytest.fixture(scope="module")
def cfg_path():
    """Q26 迁移：setup()/teardown() 逻辑改为 pytest 模块级 fixture（原 __main__ 注入）。"""
    path = setup()
    yield path
    teardown()


def make_engine(cfg_path: Path) -> DecisionEngine:
    return DecisionEngine(str(cfg_path), str(cfg_path.parent / "chiguo_decisions.jsonl"))


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=CST)


def _fixed_now(now: datetime):
    """把 daemon 模块内 datetime.now 固定为 now（其余行为继承），
    使 evaluate 全链路确定性（避开静默窗口/跨日等真实时钟依赖）。"""

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: N805
            return now

    return mock.patch.object(decision.core, "datetime", _FixedNow)


# ═══════════════════════════════════════════════════════
# Bug 1: trigger_history 双追加
# ═══════════════════════════════════════════════════════

def test_bug1_trigger_history_appended_once(cfg_path: Path):
    """一次 send 决策 → trigger_history 对同一 trigger.type 只追加一次。

    逃生阀路径（确定性触发）：高焦虑 ≥ block_th + 墙钟沉默 ≥72h + 冷却期外 →
    evaluate_triggers 必返 Trigger("longing", escape_valve)；逃生阀在
    can_send=False 时也能放行（289-290 行），且从未交互豁免不适用
    （last_user_message_at 有值）。escape_valve_sleep_block 抬高到 >1，
    防 Bayesian sleeping 高置信度门控干扰（与本次回归无关）。
    """
    eng = make_engine(cfg_path)
    eng.netease_service.enabled = False  # 听歌反证与本 bug 无关，跳过网络
    eng.config.setdefault("bayesian", {})
    eng.config["bayesian"]["escape_valve_sleep_block"] = 1.01

    now = dt(2026, 6, 15, 14, 0)  # 周一白天，静默窗口(0-8)外
    s = eng.state
    s.emotion.anxiety = 90.0
    s.emotion.loneliness = 90.0
    s.emotion.energy = 85.0
    s.cooldown.last_message_at = (now - timedelta(hours=80)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=80)).isoformat()
    s.cooldown.last_longing_break_at = None
    s.cooldown.messages_today = 0
    s.cooldown.messages_without_reply = 0
    s.cooldown.busy_until = None
    s.cooldown.event_timestamps = []
    s.cooldown.trigger_history = []

    with _fixed_now(now):
        d = eng.evaluate()

    assert d.get("action") == "send", f"expect send, got action={d.get('action')!r}"
    trg = d.get("trigger")
    n = s.cooldown.trigger_history.count(trg)
    assert n == 1, (
        f"trigger {trg!r} appended {n} times in one send decision "
        f"(expect exactly 1): {s.cooldown.trigger_history}")
    assert len(s.cooldown.trigger_history) == 1, s.cooldown.trigger_history
    print("  OK test_bug1_trigger_history_appended_once")


# ═══════════════════════════════════════════════════════
# Bug 2: _tick 用持久化 last_tick 做推进基准
# ═══════════════════════════════════════════════════════

def test_bug2_tick_uses_persisted_last_tick(cfg_path: Path):
    """cron 两次进程：第二次只推进 last_tick 以来的 15 分钟，而非消息时间全量。

    模拟：进程1 _tick(now)（last_tick=None → 按消息时间全量 10h 推进）；
    进程1 save 后进程2 重新加载（last_tick=进程1 时刻、_monotonic_at_save=0），
    _tick(now+15min) 应只推进 0.25h 增量（而非再次全量 10h）。
    """
    eng = make_engine(cfg_path)
    now = dt(2026, 6, 15, 14, 0)
    s = eng.state
    s.emotion.loneliness = 10.0
    s.cooldown.last_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.last_tick = None
    # 清除持久化锚点：本测试验证 last_tick 重放，须排除锚点封顶干扰
    # （先前测试 save 写盘的锚点对会把 elapsed 按单调流逝封顶到 ~0）
    s.mono_anchor = None
    s.wall_anchor = None

    # cron 进程 1：last_tick=None → 回退消息时间全量推进（10h）
    eng._tick(now)
    lo1 = s.emotion.loneliness
    full_increment = lo1 - 10.0
    expected_full = elastic_recover(10.0, 100.0, 10.0, 40.0, 100.0)
    assert abs(lo1 - expected_full) < 0.01, (
        f"first tick (last_tick=None) should replay full 10h from message time: "
        f"{lo1:.4f} vs {expected_full:.4f}")

    # cron 进程 2：last_tick=进程1 时刻（save 落盘值），新进程单调防护失效
    s.last_tick = now.isoformat()
    eng._monotonic_at_save = 0.0
    eng._tick(now + timedelta(minutes=15))
    lo2 = s.emotion.loneliness
    inc2 = lo2 - lo1
    assert inc2 < full_increment / 10, (
        f"second tick replayed full {10}h increment: inc2={inc2:.4f} "
        f"full={full_increment:.4f} (expect ~15min increment)")
    expected_15m = elastic_recover(lo1, 100.0, 0.25, 40.0, 100.0) - lo1
    assert abs(inc2 - expected_15m) < 0.1, (
        f"second tick increment {inc2:.4f} != 15min elastic {expected_15m:.4f}")
    print(f"  OK test_bug2_tick_uses_persisted_last_tick: inc2={inc2:.4f} < full/10={full_increment / 10:.4f}")


def test_bug2_tick_save_reload_roundtrip(cfg_path: Path):
    """真实 cron 链路：进程1 _tick + save（落盘 last_tick）→ 进程2 重新加载后
    只推进 last_tick 以来的增量（而非消息时间全量）。"""
    eng = make_engine(cfg_path)
    now = datetime.now(CST)  # 真实时钟：save() 内部写 datetime.now(CST)
    s = eng.state
    # 全新状态（同 cfg 的先前测试可能已落盘 last_tick）
    for p in (s.state_path, Path(str(s.state_path) + ".bak"),
              Path(str(s.state_path) + ".tmp")):
        p.unlink(missing_ok=True)
    s.last_tick = None
    s.emotion.loneliness = 10.0
    s.cooldown.last_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    # 清除构造时从磁盘加载的锚点对（先前测试写盘，单调流逝≈0 会把 10h 封顶成 0）
    s.mono_anchor = None
    s.wall_anchor = None

    # 进程 1：last_tick=None → 全量推进；随后 save 落盘 last_tick
    eng._tick(now)
    full_inc = s.emotion.loneliness - 10.0
    assert s.last_tick is None, "before save, last_tick must be None (fresh state)"
    assert eng.state.save(), "save() should persist last_tick"

    # 进程 2：新 engine 从同一状态文件 _load → 恢复 last_tick 与情绪
    eng2 = make_engine(cfg_path)
    # 进程1 的 save 写入了新锚点对（进程2 加载到的是「刚刚」写入的锚点，
    # 单调流逝≈0 → 会把 15min 增量封顶成 0）；本测试验证 last_tick 重放，
    # 清除锚点对以保持 15min 壁钟口径（锚点封顶由 test_tick_*_anchor 覆盖）
    eng2.state.mono_anchor = None
    eng2.state.wall_anchor = None
    lo_start2 = eng2.state.emotion.loneliness
    assert eng2.state.last_tick, "last_tick must survive save/reload roundtrip"
    assert abs(lo_start2 - (10.0 + full_inc)) < 0.01, \
        f"emotion should persist too: {lo_start2:.4f} vs {10.0 + full_inc:.4f}"
    eng2._tick(now + timedelta(minutes=15))
    inc2 = eng2.state.emotion.loneliness - lo_start2
    assert 0 < inc2 < full_inc / 10, (
        f"reload roundtrip should advance only ~15min: inc2={inc2:.4f} "
        f"full={full_inc:.4f}")
    print(f"  OK test_bug2_tick_save_reload_roundtrip: inc2={inc2:.4f} < full/10={full_inc / 10:.4f}")


def test_bug2_tick_future_last_tick_no_advance(cfg_path: Path):
    """last_tick 在未来（时钟倒退语义）→ 不推进、不崩溃。"""
    eng = make_engine(cfg_path)
    now = dt(2026, 6, 15, 14, 0)
    s = eng.state
    s.emotion.loneliness = 10.0
    s.cooldown.last_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.last_tick = (now + timedelta(hours=1)).isoformat()

    eng._tick(now)
    assert s.emotion.loneliness == 10.0, "future last_tick → elapsed<0 → no advance"
    print("  OK test_bug2_tick_future_last_tick_no_advance")


def test_bug2_tick_corrupt_last_tick_falls_back(cfg_path: Path):
    """last_tick 不可解析 → 回退消息时间全量推进（不崩溃、不零推进）。"""
    eng = make_engine(cfg_path)
    now = dt(2026, 6, 15, 14, 0)
    s = eng.state
    s.emotion.loneliness = 10.0
    s.cooldown.last_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.last_tick = "not-a-timestamp"
    # 清除持久化锚点：本测试验证 corrupt last_tick 回退，须排除锚点封顶干扰
    s.mono_anchor = None
    s.wall_anchor = None

    eng._tick(now)
    expected = elastic_recover(10.0, 100.0, 10.0, 40.0, 100.0)
    assert abs(s.emotion.loneliness - expected) < 0.01, (
        f"corrupt last_tick should fall back to message-time full progress: "
        f"{s.emotion.loneliness:.4f} vs {expected:.4f}")
    print("  OK test_bug2_tick_corrupt_last_tick_falls_back")


def test_bug2_tick_no_messages_still_noop(cfg_path: Path):
    """无任何消息时间（即使 last_tick 存在）→ 不推进（原语义保留）。"""
    eng = make_engine(cfg_path)
    now = dt(2026, 6, 15, 14, 0)
    s = eng.state
    s.cooldown.last_message_at = None
    s.cooldown.last_user_message_at = None
    s.last_tick = (now - timedelta(minutes=15)).isoformat()
    s.emotion.loneliness = 10.0

    eng._tick(now)
    assert s.emotion.loneliness == 10.0, "no message timestamps → tick(0) noop"
    print("  OK test_bug2_tick_no_messages_still_noop")


# ═══════════════════════════════════════════════════════
# Bug 3: 收件人未配置 → 消息假发送且不退款
# ═══════════════════════════════════════════════════════

def _send_decision(msg_id: str = "m1") -> dict:
    return {"action": "send", "msg_id": msg_id, "trigger": "lonely_mid",
            "intensity": "medium", "version": "1.11", "context": {"layer": "shell"}}


def test_bug3_no_recipient_no_fake_send(cfg_path: Path):
    """wechat_recipient 为空 → 不写 send 归档（A9 查重数据源不被污染），
    且走 failed 退款闭环（Hawkes 事件清账 + 状态回滚），send_error 置说明。

    消息文本经 fake spawn（AGENT_RUN_SCRIPT）生成，bridge 不可达 → RPC 失败
    回退 spawn 成功，避免引入 HTTP server。
    """
    fake_runner = Path(cfg_path).parent / "fake-agent-run.mjs"
    fake_runner.write_text(
        "process.stdout.write(JSON.stringify({ ok: true, text: '无收件人测试文本' }))")
    old_runner = os.environ.get("AGENT_RUN_SCRIPT")
    os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
    try:
        eng = make_engine(cfg_path)
        eng.config.setdefault("wechat", {})
        eng.config["wechat"]["wechat_recipient"] = ""  # 未配置收件人
        s = eng.state
        # 在途 Hawkes 事件（msg_id 可定位 → refund_send 应执行）
        s.cooldown.event_timestamps = [{"msg_id": "m1", "time": "t"}]
        s.emotion.energy = 50.0
        s.cooldown.messages_today = 1
        s.cooldown.messages_without_reply = 1
        # 真实流程:loop 主循环 evaluate 各出口无条件 save 后才调 _loop_send;
        # record_send_result 锁内 _load 重载磁盘最新状态(v12-R2),故修改后须落盘
        assert s.save()

        loop_cfg = {"bridge_url": "http://127.0.0.1:1"}  # 不可达 → RPC 失败 → spawn 兜底
        import contextlib
        import io
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            out = eng._loop_send(_send_decision("m1"), loop_cfg)

        assert out["generated"] is True, out
        assert out["sent"] is False, out
        assert "recipient" in (out.get("send_error") or "").lower(), \
            f"send_error should explain missing recipient, got {out.get('send_error')!r}"
        # ④ stderr 告警
        assert "wechat_recipient not configured" in stderr_buf.getvalue(), stderr_buf.getvalue()

        # ① 未发送文本不写 chiguo_messages.jsonl（recent_sent_texts 查重源不被污染）
        msgs_path = cfg_path.parent / "chiguo_messages.jsonl"
        content = msgs_path.read_text() if msgs_path.exists() else ""
        assert "无收件人测试文本" not in content, \
            "unsent text must NOT be archived as sent (A9 dedup pollution)"
        assert '"direction": "send"' not in content, content

        # ② failed 退款闭环：能量回滚 + 日计数回滚 + 事件清账
        assert s.emotion.energy == 70.0, f"refund should restore energy, got {s.emotion.energy}"
        assert s.cooldown.messages_today == 0, s.cooldown.messages_today
        assert s.cooldown.event_timestamps == [], s.cooldown.event_timestamps

        # ③ decisions.jsonl 有 send_result failed 记录
        logs_path = cfg_path.parent / "chiguo_decisions.jsonl"
        logs = logs_path.read_text() if logs_path.exists() else ""
        assert '"action": "send_result"' in logs and '"status": "failed"' in logs, logs
    finally:
        if old_runner is None:
            os.environ.pop("AGENT_RUN_SCRIPT", None)
        else:
            os.environ["AGENT_RUN_SCRIPT"] = old_runner
    print("  OK test_bug3_no_recipient_no_fake_send")


# ═══════════════════════════════════════════════════════
# Bug 4: record_send_result TOCTOU（检查在锁外 → 并发双退款）
# ═══════════════════════════════════════════════════════

# 子进程 worker：把 _has_send_result 检查后的窗口拉大到 1.5s（测试专用卡点）。
# 修复前检查在锁外：两个进程的检查都先完成（都固定 already_reported=False）
# → 依次进锁各自退款 → 双退款；
# 修复后检查在锁内：第一个进程持锁完成退款+写日志后释放，第二个进程才
# 能进锁（flock 互斥），重查可见第一条日志 → duplicate=true 不退款。
# 卡点时长设计：两个 worker 的检查（或持锁）串行 ≤3s < flock 5s 超时。
_WORKER_SRC = r'''
import json, os, sys, time
from pathlib import Path
ROOT = {root}
os.chdir(ROOT)
sys.path.insert(0, ROOT)
from chiguo_daemon import DecisionEngine
CFG, LOG, msg_id, marker_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
orig = DecisionEngine._has_send_result
def slow_check(self, mid):
    r = orig(self, mid)
    Path(marker_path).write_text("done")
    time.sleep(1.5)  # 卡点：检查后停顿，放大并发窗口
    return r
DecisionEngine._has_send_result = slow_check
eng = DecisionEngine(CFG, LOG)
res = eng.record_send_result(msg_id, "failed", "concurrent")
print(json.dumps(res), flush=True)
'''


def test_bug4_concurrent_send_result_single_refund(cfg_path: Path):
    """两个进程并发上报同一 msg_id=failed → 只退款一次、只记一次 refunded。

    真实并发用两个子进程（跨进程 flock 才互斥；单进程线程因锁可重入不互斥）。
    卡点保证两个进程的 _has_send_result 检查都发生在任何日志写入之前：
    修复前（检查在锁外）→ 都读到未上报 → 双退款；
    修复后（检查在锁内）→ 第二个进程被锁阻塞后重查 → duplicate=true 不退款。
    """
    # 准备：在途事件 + 可区分的日计数（2 → 单退款 1 / 双退款 0）
    # 用独立 msg_id 并清空日志：避免此前测试（Bug 3 的 m1）污染去重判定
    bug4_msg = "bug4m1"
    eng = make_engine(cfg_path)
    s = eng.state
    for p in (s.state_path, Path(str(s.state_path) + ".bak"),
              Path(str(s.state_path) + ".tmp")):
        p.unlink(missing_ok=True)
    (cfg_path.parent / "chiguo_decisions.jsonl").unlink(missing_ok=True)
    s.cooldown.event_timestamps = [{"msg_id": bug4_msg, "time": "t"}]
    s.cooldown.messages_today = 2
    s.cooldown.messages_without_reply = 2
    s.emotion.energy = 50.0
    s.last_tick = None
    assert eng.state.save()

    worker = cfg_path.parent / "_bug4_worker.py"
    worker.write_text(_WORKER_SRC.format(root=repr(str(Path(__file__).resolve().parent.parent)),
                                         cfg=repr(str(cfg_path)),
                                         log=repr(str(cfg_path.parent / "chiguo_decisions.jsonl"))))
    markers = [cfg_path.parent / f"bug4_marker_{i}" for i in range(2)]
    procs = [subprocess.Popen(
        [sys.executable, str(worker), str(cfg_path),
         str(cfg_path.parent / "chiguo_decisions.jsonl"), bug4_msg, str(m)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for m in markers]
    # 等两个 worker 都到达检查点（卡点前）
    deadline = time.time() + 15
    while time.time() < deadline and not all(m.exists() for m in markers):
        time.sleep(0.05)
    assert all(m.exists() for m in markers), "workers did not reach check point"
    outs, errs = [], []
    for p in procs:
        o, e = p.communicate(timeout=60)
        outs.append(o.strip())
        errs.append(e)
    results = [json.loads(o) for o in outs]
    assert len(results) == 2, (outs, errs)

    refunded = [r for r in results if r["refunded"]]
    dup = [r for r in results if not r["refunded"]]
    assert len(refunded) == 1, f"expect exactly 1 refund, got {results}"
    assert len(dup) == 1 and dup[0]["duplicate"] is True, f"expect duplicate=true, got {results}"

    # 状态只回滚一次：energy 50 → 70（双退款会 50 → 90）。
    # （messages_today 不可用：current_date 初始为空，退款时跨日重置归零，
    # 单/双退款无法区分；energy 不受跨日重置影响。）
    state_data = json.loads(s.state_path.read_text())
    assert state_data["emotion"]["energy"] == 70.0, state_data["emotion"]["energy"]
    assert state_data["cooldown"]["event_timestamps"] == [], state_data["cooldown"]["event_timestamps"]

    # 日志：两条 send_result，refunded:true 恰一条
    logs = (cfg_path.parent / "chiguo_decisions.jsonl").read_text()
    entries = [json.loads(l) for l in logs.splitlines()
               if json.loads(l).get("action") == "send_result"]
    assert len(entries) == 2, entries
    assert sum(1 for e in entries if e["refunded"]) == 1, entries
    print(f"  OK test_bug4_concurrent_send_result_single_refund: {results}")


# ═══════════════════════════════════════════════════════
# Bug 5: record_user_message 锁内不 _load → 覆盖 evaluate 已落盘推进（#139）
# ═══════════════════════════════════════════════════════

def test_bug5_record_user_message_reloads_disk_state(cfg_path: Path):
    """构造加载 S0 后，另一进程（cron evaluate）已把 S1 落盘 → --user-msg
    record_user_message 若基于陈旧 S0 快照 RMW+save，S0→S1 的情绪推进会被
    整体回滚覆盖（tick_seq CAS 只防序列回退，不保护 emotion/cooldown）。

    v1.14 修复（与 record_send_result v12-R2 一致）：state_lock 内先 _load()
    重载磁盘最新状态再处理 → 保存后磁盘 = S1 + 本消息 delta（而非 S0 + delta）。
    """
    # 先清残留再构造：共享 temp 目录中前序测试（如 bug4）可能留下 last_message_at/
    # drop_events 等 cooldown 字段，engine 构造会加载它们污染 S0 → 必须先删状态文件。
    for name in ("chiguo_state.json", "chiguo_state.json.bak", "chiguo_state.json.tmp",
                 "chiguo_decisions.jsonl"):
        (cfg_path.parent / name).unlink(missing_ok=True)
    eng = make_engine(cfg_path)
    s = eng.state

    # ── S0：构造时加载的基础状态（无 last_message_at → 无回复延迟倍率）──
    s.emotion.loneliness = 50.0
    s.emotion.anxiety = 50.0
    s.emotion.affection = 10.0
    s.emotion.energy = 50.0
    s.emotion.tsundere_index = 50.0
    s.cooldown.messages_without_reply = 2
    assert s.save()

    # ── S1：另一进程（cron evaluate）已落盘的推进后状态 ──
    eng2 = make_engine(cfg_path)  # 从磁盘加载 S0，模拟独立 cron 进程
    s2 = eng2.state
    s2.emotion.loneliness = 80.0
    s2.emotion.anxiety = 70.0
    s2.emotion.affection = 12.0
    s2.emotion.energy = 60.0
    s2.cooldown.messages_without_reply = 3
    assert s2.save()

    # ── --user-msg：本进程仍持 S0 陈旧内存快照 → 进锁处理 ──
    eng.record_user_message("bug5test")

    # 修复后磁盘 = S1 + 本消息 delta（on_user_message 单次完整效果，damp=1）
    data = json.loads(s.state_path.read_text())
    emo_cfg = eng.config.get("emotion", {})
    lo_decay = decay(1.0, 1.0, float(emo_cfg.get("loneliness_decay_on_reply", 0.35)))
    anx_decay = decay(1.0, 1.0, float(emo_cfg.get("anxiety_decay_on_reply", 0.5)))
    aff_gain = float(emo_cfg.get("affection_gain_per_interaction", 0.8))
    energy_bonus = float(emo_cfg.get("energy_bonus_on_reply", 10.0))

    # 各维度的「S1 推进后 + delta」期望值（回滚到 S0 再 + delta 则为回退值）
    exp_energy = 60.0 + energy_bonus      # 保留 S1 的 60；回滚则 50+10=60 ≠ 70
    assert abs(data["emotion"]["energy"] - exp_energy) < 1e-3, (
        f"energy 应保留 S1 推进（60+{energy_bonus}={exp_energy:.1f}），"
        f"实得 {data['emotion']['energy']:.1f}")
    assert abs(data["emotion"]["affection"] - (12.0 + aff_gain)) < 1e-3, (
        f"affection 应保留 S1 推进（12+{aff_gain}={12.0 + aff_gain:.2f}），"
        f"实得 {data['emotion']['affection']:.2f}（回滚到 S0 则为 {10.0 + aff_gain:.2f}）")
    assert abs(data["emotion"]["loneliness"] - (80.0 * lo_decay)) < 1e-3, (
        f"loneliness 应保留 S1 推进（80×{lo_decay:.4f}={80.0 * lo_decay:.3f}），"
        f"实得 {data['emotion']['loneliness']:.3f}（回滚到 S0 则为 {50.0 * lo_decay:.3f}）")
    assert abs(data["emotion"]["anxiety"] - (70.0 * anx_decay)) < 1e-3, (
        f"anxiety 应保留 S1 推进（70×{anx_decay:.4f}={70.0 * anx_decay:.3f}），"
        f"实得 {data['emotion']['anxiety']:.3f}（回滚到 S0 则为 {50.0 * anx_decay:.3f}）")
    print("  OK test_bug5_record_user_message_reloads_disk_state")


def test_bug5_record_user_message_upgrade_reloads_disk_state(cfg_path: Path):
    """recv_dedup 升级分支同样锁内 _load：standing order 补 --analysis 时，
    若另一进程已把 S1 落盘，升级分析也基于最新磁盘状态（不基于构造时 S0）。"""
    for name in ("chiguo_state.json", "chiguo_state.json.bak", "chiguo_state.json.tmp",
                 "chiguo_decisions.jsonl"):
        (cfg_path.parent / name).unlink(missing_ok=True)
    eng = make_engine(cfg_path)
    s = eng.state

    now = datetime.now(CST)
    text = "哥哥在吗"
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    dedup_entry = {"text_sha": text_sha, "at": now.isoformat(), "analysis": False}

    # ── S0：bridge 首趟已记录（无分析），基础情绪 ──
    s.cooldown.recv_dedup = dict(dedup_entry)
    s.emotion.energy = 50.0
    s.emotion.affection = 10.0
    assert s.save()

    # ── S1：另一进程 evaluate 已推进并落盘 ──
    eng2 = make_engine(cfg_path)  # 从磁盘加载 S0，模拟独立进程
    s2 = eng2.state
    s2.cooldown.recv_dedup = dict(dedup_entry)
    s2.emotion.energy = 60.0
    s2.emotion.affection = 12.0
    assert s2.save()

    # ── standing order 补分析（本进程仍持 S0 陈旧快照）→ 升级路径 ──
    eng.record_user_message(text, '{"warmth": 1.0}')

    data = json.loads(s.state_path.read_text())
    emo_cfg = eng.config.get("emotion", {})
    energy_warmth = float(emo_cfg.get("energy_warmth_factor", 4.0))
    aff_warmth = float(emo_cfg.get("affection_warmth_factor", 1.5))
    exp_energy = 60.0 + energy_warmth   # 升级只叠加分析维度（不动基础回复效果）
    exp_aff = 12.0 + aff_warmth
    assert abs(data["emotion"]["energy"] - exp_energy) < 1e-3, (
        f"upgrade energy 应保留 S1 推进（60+{energy_warmth}={exp_energy:.1f}），"
        f"实得 {data['emotion']['energy']:.1f}（回滚到 S0 则为 {50.0 + energy_warmth:.1f}）")
    assert abs(data["emotion"]["affection"] - exp_aff) < 1e-3, (
        f"upgrade affection 应保留 S1 推进（12+{aff_warmth}={exp_aff:.1f}），"
        f"实得 {data['emotion']['affection']:.1f}（回滚到 S0 则为 {10.0 + aff_warmth:.1f}）")
    assert data["cooldown"]["recv_dedup"].get("analysis") is True, \
        "升级后去重标记应持久化为 analysis=True"
    print("  OK test_bug5_record_user_message_upgrade_reloads_disk_state")


# ═══════════════════════════════════════════════════════
# --memory-search CLI（v1.15 回复侧记忆检索，mem0 唯一后端）
# ═══════════════════════════════════════════════════════

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_SCRIPT = os.path.join(_REPO_ROOT, "chiguo_daemon.py")


def test_memory_search_disabled_mem0_soft_degrade(cfg_path: Path):
    """CHIGUO_MEM0_DISABLED=1（mem0 不可用）→ --memory-search 软降级：
    ok:true + memories:[] + exit 0（检索失败不阻塞调用方）。"""
    env = dict(os.environ, CHIGUO_MEM0_DISABLED="1")
    p = subprocess.run([sys.executable, _DAEMON_SCRIPT, "--memory-search", "咖啡"],
                       capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
    assert p.returncode == 0, f"exit {p.returncode}: {p.stdout} {p.stderr}"
    out = json.loads(p.stdout)
    assert out["action"] == "memory_search"
    assert out["ok"] is True
    assert out["query"] == "咖啡"
    assert out["count"] == 0
    assert out["memories"] == []
    print("  OK test_memory_search_disabled_mem0_soft_degrade")


def test_memory_search_bad_backend_config(cfg_path: Path):
    """临时 toml [memory] backend='x.y'（坏配置）→ ok:false + exit 1
    （子进程真实退出码；坏配置明确报错而非静默空结果）。"""
    with tempfile.TemporaryDirectory(prefix="chiguo_test_daemon_fixes_") as td:
        bad_cfg = Path(td) / "chiguo_proactive.toml"
        bad_cfg.write_text('[memory]\nbackend = "x.y"\n')
        code = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import chiguo_daemon as m\n"
            "m._cmd_memory_search(sys.argv[3], config_path=sys.argv[2])\n"
        )
        p = subprocess.run([sys.executable, "-c", code, _REPO_ROOT, str(bad_cfg), "咖啡"],
                           capture_output=True, text=True, cwd=_REPO_ROOT)
        assert p.returncode == 1, (
            f"bad backend config should exit 1, got {p.returncode}: {p.stdout} {p.stderr}")
        out = json.loads(p.stdout)
        assert out["action"] == "memory_search"
        assert out["ok"] is False
        assert "--memory-search 失败" in p.stderr
    print("  OK test_memory_search_bad_backend_config")


# ═══════════════════════════════════════════════════════
# 单调锚点对（mono_anchor/wall_anchor 持久化）
# ═══════════════════════════════════════════════════════

def test_state_monotonic_anchor_persist_roundtrip(cfg_path: Path):
    """save → 重新 load：mono_anchor 为 float、wall_anchor 为 CST ISO 字符串，
    checksum 校验通过（往返不触发 checksum_mismatch 回退）。"""
    eng = make_engine(cfg_path)
    s = eng.state
    for p in (s.state_path, Path(str(s.state_path) + ".bak"),
              Path(str(s.state_path) + ".tmp")):
        p.unlink(missing_ok=True)
    assert s.save(), "save() should persist mono_anchor/wall_anchor"

    # 新进程重新加载 → _apply_loaded_data 恢复锚点对（checksum 通过才会走到）
    eng2 = make_engine(cfg_path)
    s2 = eng2.state
    mono, wall = s2.monotonic_anchor()
    assert isinstance(mono, (int, float)) and not isinstance(mono, bool), (
        f"mono_anchor should be a number, got {mono!r}")
    assert isinstance(wall, str) and wall, f"wall_anchor should be a non-empty str, got {wall!r}"
    try:
        wall_dt = datetime.fromisoformat(wall)
    except ValueError:
        raise AssertionError(f"wall_anchor is not a valid ISO timestamp: {wall!r}")
    assert wall_dt.utcoffset() is not None and wall_dt.utcoffset().total_seconds() == 8 * 3600, (
        f"wall_anchor should be CST (+08:00): {wall!r}")

    # 落盘 payload 顶层也应含这两个字段
    raw = json.loads(s2.state_path.read_text())
    assert isinstance(raw.get("mono_anchor"), (int, float)) and \
        not isinstance(raw.get("mono_anchor"), bool), raw.get("mono_anchor")
    assert isinstance(raw.get("wall_anchor"), str) and raw.get("wall_anchor"), \
        raw.get("wall_anchor")
    print("  OK test_state_monotonic_anchor_persist_roundtrip")


def test_state_monotonic_anchor_missing_defaults(cfg_path: Path):
    """旧 state（无新字段）与非法类型（bool/空串）→ 加载为 None，不抛异常。"""
    eng = make_engine(cfg_path)
    s = eng.state
    for p in (s.state_path, Path(str(s.state_path) + ".bak"),
              Path(str(s.state_path) + ".tmp")):
        p.unlink(missing_ok=True)
    # 旧格式 state：无 mono_anchor/wall_anchor 字段（无 _checksum → 跳过校验）
    old_state = {
        "_version": 10,
        "emotion": {},
        "cooldown": {},
        "circadian": {},
        "last_tick": "2026-06-15T14:00:00+08:00",
        "tick_seq": 3,
    }
    s.state_path.write_text(json.dumps(old_state))

    eng2 = make_engine(cfg_path)
    assert eng2.state.mono_anchor is None, eng2.state.mono_anchor
    assert eng2.state.wall_anchor is None, eng2.state.wall_anchor
    assert eng2.state.monotonic_anchor() == (None, None)

    # 非法类型（bool mono_anchor / 空串 wall_anchor）→ 同样回退 None
    bad_state = dict(old_state, mono_anchor=True, wall_anchor="")
    s.state_path.write_text(json.dumps(bad_state))
    eng3 = make_engine(cfg_path)
    assert eng3.state.mono_anchor is None, eng3.state.mono_anchor
    assert eng3.state.wall_anchor is None, eng3.state.wall_anchor
    print("  OK test_state_monotonic_anchor_missing_defaults")


# ═══════════════════════════════════════════════════════
# Task 2 (#206): _tick 用真实流逝封顶 NTP 前跳（持久化单调锚点）
# ═══════════════════════════════════════════════════════

def test_tick_caps_wall_jump_via_persisted_anchor(cfg_path: Path):
    """NTP 前跳模拟：壁钟 now 距最后消息 6h，但持久化 mono 锚点只过了 15 分钟
    → min() 把 elapsed 收敛到 ~0.25h，情绪推进量对应 0.25h 而非 6h。"""
    eng = make_engine(cfg_path)
    now = dt(2026, 6, 15, 14, 0)
    s = eng.state
    s.emotion.loneliness = 10.0
    s.cooldown.last_message_at = (now - timedelta(hours=6)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=6)).isoformat()
    s.last_tick = None  # 基准只用消息时间 → 壁钟 elapsed = 6h

    MONO = 100000.0
    # mono 锚点：15 分钟前（monotonic - 0.25h）；wall_anchor：6h 前（存在信号）
    s.mono_anchor = MONO - 0.25 * 3600
    s.wall_anchor = (now - timedelta(hours=6)).isoformat()

    with mock.patch.object(time, "monotonic", return_value=MONO):
        eng._tick(now)

    inc = s.emotion.loneliness - 10.0
    cap_15m = elastic_recover(10.0, 100.0, 0.25, 40.0, 100.0) - 10.0
    full_6h = elastic_recover(10.0, 100.0, 6.0, 40.0, 100.0) - 10.0
    assert abs(inc - cap_15m) < 0.01, (
        f"wall jump should cap to ~0.25h anchor elapsed: inc={inc:.4f} "
        f"cap_15m={cap_15m:.4f} full_6h={full_6h:.4f}")
    assert inc < full_6h / 4, (
        f"capped increment must be far below full 6h advance: inc={inc:.4f} full_6h={full_6h:.4f}")
    print(f"  OK test_tick_caps_wall_jump_via_persisted_anchor: inc={inc:.4f} (0.25h cap)")


def test_tick_no_cap_when_anchor_older_than_monotonic(cfg_path: Path):
    """系统重启模拟：time.monotonic() 归零（< mono_anchor）→ 不加封顶，
    elapsed 保持壁钟口径（6h 全量推进，与无锚点行为一致）。"""
    eng = make_engine(cfg_path)
    now = dt(2026, 6, 15, 14, 0)
    s = eng.state
    s.emotion.loneliness = 10.0
    s.cooldown.last_message_at = (now - timedelta(hours=6)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=6)).isoformat()
    s.last_tick = None

    MONO = 100000.0
    # 重启后 monotonic 归零：mono_anchor（重启前读数）> 当前 monotonic → 条件不成立
    s.mono_anchor = MONO + 3600.0
    s.wall_anchor = (now - timedelta(hours=6)).isoformat()

    with mock.patch.object(time, "monotonic", return_value=MONO):
        eng._tick(now)

    inc = s.emotion.loneliness - 10.0
    full_6h = elastic_recover(10.0, 100.0, 6.0, 40.0, 100.0) - 10.0
    assert abs(inc - full_6h) < 0.01, (
        f"reboot (monotonic reset) must keep wall-clock elapsed: inc={inc:.4f} "
        f"full_6h={full_6h:.4f}")
    print(f"  OK test_tick_no_cap_when_anchor_older_than_monotonic: inc={inc:.4f} (wall 6h)")
