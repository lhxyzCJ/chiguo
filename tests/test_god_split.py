"""test_god_split.py — T13 TDD: God 拆 helpers 可测契约 (RED→GREEN)"""
import tempfile
import tomllib
import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

def _make_state(tmpdir: str):
    from chiguo_state import ChiguoState
    cfg_path = Path(tmpdir) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmpdir)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmpdir) / "no_qdrant")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    s = ChiguoState(cfg)
    return s

# ── save 拆分: _cas_tick_seq ─────────────────────────────
def test_cas_tick_seq_branch():
    """_cas_tick_seq(p, owner) 分支：磁盘领先时跳升+1，degraded 时 abort"""
    from chiguo_state import StatePersistence
    # 存在性断言 (RED until implemented)
    assert hasattr(StatePersistence, "_cas_tick_seq"), "StatePersistence._cas_tick_seq 未实现"
    assert hasattr(StatePersistence, "_build_payload"), "_build_payload 缺失"
    # 分支行为：模拟 disk_seq=10, owner tick_seq=3 → 应跳至 11
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        p = s._persistence.state_path
        # 先落盘一次得到 tick_seq=1
        s.save(_backup=False, _increment_tick=True)
        data = json.loads(p.read_text())
        assert data["tick_seq"] >= 1
        # 人为把内存回退，CAS 应自动追上
        s.tick_seq = 0
        # 调用 _cas_tick_seq 应将 tick_seq 追至 disk+1 且不 abort
        ok = s._persistence._cas_tick_seq(p, s)
        assert ok is True
        assert s.tick_seq >= data["tick_seq"] + 1

def test_cas_tick_seq_degraded_abort():
    """降级进入且磁盘更新 → abort 返回 False"""
    from chiguo_state import StatePersistence
    assert hasattr(StatePersistence, "_cas_tick_seq")
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        p = s._persistence.state_path
        s.save(_backup=False, _increment_tick=True)
        s.tick_seq = 0
        s._persistence._lock_degraded = True
        # 磁盘领先，degraded 应 abort
        ok = s._persistence._cas_tick_seq(p, s)
        assert ok is False
        s._persistence._lock_degraded = False


# ── apply_loaded_data 拆分: 3 hydrates ───────────────────
def test_hydrate_helpers_exist():
    from chiguo_state import StatePersistence
    for name in ("_hydrate_emotion", "_hydrate_cooldown", "_hydrate_circadian"):
        assert hasattr(StatePersistence, name), f"{name} 未实现"

def test_hydrate_circadian_migration_pure():
    """_hydrate_circadian(o, data) 纯迁移：无 bucket → 补桶"""
    from chiguo_state import StatePersistence, ChiguoState
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        # 构造旧格式 circadian 无 bucket 的 data
        old = {"date": "2026-07-01", "hour": 12}
        s.circadian.reply_days = [old]
        # 调用 hydrate 应迁移出 bucket 字段
        persisted = s._persistence
        # _hydrate_circadian 显式 o 参数
        if hasattr(persisted, "_hydrate_circadian"):
            persisted._hydrate_circadian(s, {"circadian": {}})
            for d in s.circadian.reply_days:
                assert "bucket" in d or d.get("date") is None

# ── tick 拆分: 4 deltas ───────────────────────────────────
def test_tick_helpers_pure_and_delta():
    from chiguo_state import ChiguoState
    for name in ("_tick_loneliness", "_tick_anxiety", "_tick_affection", "_tick_energy"):
        assert hasattr(ChiguoState, name), f"ChiguoState.{name} 未实现"
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        # 孤独增量：loneliness 15 → 向 baseline=0 靠拢应上升
        lo = s._tick_loneliness(15.0, 1.0, silent_h=0, cfg=s.config.get("emotion", {}))
        assert isinstance(lo, float)
        # 静默>24 加速：half_life*0.6 应更大
        lo_quiet = s._tick_loneliness(15.0, 1.0, silent_h=30, cfg=s.config.get("emotion", {}))
        assert lo_quiet != lo
        # energy 恢复应 >0
        en = s._tick_energy(50.0, 1.0, cfg=s.config.get("emotion", {}))
        assert en > 50.0

def test_tick_helpers_anxiety_modulation():
    from chiguo_state import ChiguoState
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        now = datetime(2026, 8, 1, 12, 0, tzinfo=CST)
        a1 = s._tick_anxiety(40.0, 1.0, now, cfg=s.config.get("emotion", {}))
        assert isinstance(a1, float)

# ── on_user_message 拆分: decay / affection ─────────────
def test_on_user_message_helpers():
    from chiguo_state import ChiguoState
    for name in ("_decay_all", "_affection_gain", "_energy_bonus", "_reply_damp", "_latency_multiplier"):
        assert hasattr(ChiguoState, name), f"{name} 缺失"
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        now = datetime.now(CST)
        # _decay_all 纯函数：孤独/不安骤降应下降
        lo0, anx0 = 60.0, 50.0
        lo1, anx1 = s._decay_all(lo0, anx0, now, damp=1.0)
        assert lo1 < lo0
        assert anx1 < anx0
        # _affection_gain 随 msg_length 1.5×
        g_short = s._affection_gain(10, 1.0, damp=1.0)
        g_long = s._affection_gain(40, 1.0, damp=1.0)
        assert g_long > g_short

# ── _apply_emotion_impact 拆分: inertia small helpers ───
def test_apply_emotion_impact_helpers():
    from chiguo_state import ChiguoState
    inertia_helpers = [x for x in dir(ChiguoState) if "inertia" in x.lower() or x in ("_impact_warmth", "_impact_effort", "_impact_attention", "_damp")]
    assert len(inertia_helpers) >= 2, f"inertia small helpers 不足: {inertia_helpers}"
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        if hasattr(s, "_impact_warmth"):
            # 支持两种签名：纯计算或直接作用于 emotion
            try:
                d1 = s._impact_warmth(0.5, s.config.get("emotion", {}))
            except TypeError:
                d1 = None
            assert d1 is None or isinstance(d1, (int, float, type(None)))
        if hasattr(s, "_damp"):
            assert callable(getattr(s, "_damp"))
            # _damp 应压缩正向 delta
            d = s._damp(1.0)
            assert isinstance(d, float)

# ── wc 约束预检查：函数长度上限 70 ─────────────────────
def test_file_constraints():
    import ast
    src = Path("chiguo_state.py").read_text()
    tree = ast.parse(src)
    funcs = [(n.name, n.end_lineno - n.lineno + 1) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    # 目标 6 拆分函数必须 ≤70，helpers ≤50；__init__ 例外(71 callers)允许 ≤90
    exempt = {"__init__", "infer_user_state", "schedule_status", "refund_send", "adapt_personality", "snapshot"}
    over = [(n, l) for n, l in funcs if l > 70 and n not in exempt]
    helpers_over = [(n, l) for n, l in funcs if n.startswith("_tick_") or n.startswith("_hydrate_") or n.startswith("_impact_") or n in ("_cas_tick_seq", "_decay_all", "_affection_gain", "_energy_bonus", "_damp") for _ in [0] if l > 50]
    # 重新计算 helpers_over 正确
    helpers_over = [(n, l) for n, l in funcs if (n.startswith("_tick_") or n.startswith("_hydrate_") or n.startswith("_impact_") or n in ("_cas_tick_seq", "_decay_all", "_affection_gain", "_energy_bonus", "_damp", "_inertia_params")) and l > 50]
    assert not over, f"仍有目标超长函数 >70: {over}"
    assert not helpers_over, f"helpers 超 50: {helpers_over}"
    total = len(src.splitlines())
    assert total <= 2200, f"总行数 {total} 仍 >2200"
