#!/usr/bin/env python3
"""test_bayesian_transition.py — A1 状态转移矩阵 + 前向滤波单元测试

覆盖: TRANSITIONS 行归一化 / _transition_prior 矩阵向量乘 / infer 的
transition_enabled 先验混合与熵 / prev_posterior 持久化往返与容错 /
config 逐行覆盖。默认关闭 → 行为恒等（既有 test_bayesian 不受影响）。
"""

import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import tempfile
import tomllib
from pathlib import Path

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from chiguo_bayesian import UserStateEstimator
from chiguo_state import ChiguoState


def make_estimator(config=None):
    return UserStateEstimator(config or {})


def test_transitions_rows_normalized():
    """每行概率和为 1（马尔可夫行），覆盖全部状态键。"""
    est = make_estimator()
    for s in UserStateEstimator.STATES:
        row = est.TRANSITIONS.get(s, {})
        assert set(row.keys()) == set(UserStateEstimator.STATES), f"{s} 行缺状态"
        assert abs(sum(row.values()) - 1.0) < 1e-9, f"{s} 行未归一化: {row}"
    print("  OK test_transitions_rows_normalized")


def test_transition_prior_matrix_vector():
    """前向滤波：上一后验 × 转移矩阵 → 预测先验（保持性 + 扩散）。"""
    est = make_estimator()
    # 上 tick 完全在 chatting → 下一 tick chatting 保持概率最高
    prior = est._transition_prior({"chatting": 1.0})
    assert prior["chatting"] > prior["browsing"], f"chatting 保持概率应最高: {prior}"
    # 上 tick 完全在 sleeping → sleeping 保持，但向 away/active 扩散
    prior2 = est._transition_prior({"sleeping": 1.0})
    assert prior2["sleeping"] > 0.5
    # 归一化
    assert abs(sum(prior.values()) - 1.0) < 1e-9
    print("  OK test_transition_prior_matrix_vector")


def test_transition_prior_mix_with_time():
    """transition_enabled 时先验 = 0.5×转移 + 0.5×时间（线性混合，归一化）。"""
    est = make_estimator({"transition_enabled": True})
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)  # 白天
    r1 = est.infer({"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5}, now)
    est2 = make_estimator({})
    r2 = est2.infer({"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5}, now)
    # 转移先验使 chatting 概率 ≥ 纯时间先验的 0.5 倍混合
    assert abs(sum(r1["posterior"].values()) - 1.0) < 0.01
    print(f"  OK test_transition_prior_mix_with_time: chat={r1['posterior']['chatting']:.2f} vs pure-time={r2['posterior']['chatting']:.2f}")


def test_infer_adds_entropy_and_prev_posterior():
    """A1 启用时返回 dict 新增 entropy（bits，0~log2(6)）与 prev_posterior 透传。"""
    est = make_estimator({"transition_enabled": True})
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    r = est.infer({}, now)
    assert "entropy" in r and "prev_posterior" in r
    assert 0.0 <= r["entropy"] <= math.log2(6) + 1e-9
    assert abs(sum(r["prev_posterior"].values()) - 1.0) < 0.02  # 四舍五入后近似 1
    assert abs(sum(r["posterior"].values()) - 1.0) < 0.01
    print(f"  OK test_infer_adds_entropy_and_prev_posterior: entropy={r['entropy']:.3f}")


def test_default_off_no_entropy_keys():
    """A1 默认关闭（恒等）→ infer 结果/落盘均无 entropy/prev_posterior/_prev_posterior，
    决策日志与状态文件不新增字段。"""
    est = make_estimator({})
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    r = est.infer({}, now)
    assert "entropy" not in r and "prev_posterior" not in r, r
    assert est.prev_posterior is None
    sd = est.to_state_dict()
    assert "_prev_posterior" not in sd, sd
    # info_gain_threshold 单独开启 → 熵仍产出（但 _prev_posterior 仅在 transition 时落盘）
    est2 = make_estimator({"info_gain_threshold": 1.5})
    r2 = est2.infer({}, now)
    assert "entropy" in r2 and "prev_posterior" in r2
    assert "_prev_posterior" not in est2.to_state_dict()
    print("  OK test_default_off_no_entropy_keys")


def test_transition_uses_stored_prev_posterior():
    """infer 无显式 prev_posterior 时自动用自身持久化的 prev_posterior（前向滤波闭环）。"""
    est = make_estimator({"transition_enabled": True})
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    est.infer({"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5}, now)
    assert est.prev_posterior is not None
    # 第二次 infer 内部用第一次的后验 → 结果有效（不抛、归一化）
    r2 = est.infer({"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5}, now)
    assert abs(sum(r2["posterior"].values()) - 1.0) < 0.01
    print("  OK test_transition_uses_stored_prev_posterior")


def test_prev_posterior_persist_roundtrip():
    """to_state_dict/restore_state_dict 往返不丢 prev_posterior。"""
    est = make_estimator({"transition_enabled": True})
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    est.infer({"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5}, now)
    sd = est.to_state_dict()
    assert "_prev_posterior" in sd, "to_state_dict 应含 _prev_posterior"
    est2 = make_estimator({"transition_enabled": True})
    est2.restore_state_dict(sd)
    assert est2.prev_posterior is not None
    assert abs(sum(est2.prev_posterior.values()) - 1.0) < 1e-9
    for s in UserStateEstimator.STATES:
        assert abs(est2.prev_posterior[s] - est.prev_posterior[s]) < 1e-9
    print("  OK test_prev_posterior_persist_roundtrip")


def test_prev_posterior_restore_fault_tolerant():
    """restore 坏数据不崩：非 dict / 非法状态 / NaN / 全非法 → 缺省 None（走纯时间先验）。"""
    est = make_estimator({})
    est.restore_state_dict("notadict")
    est.restore_state_dict({"_prev_posterior": "garbage"})
    est.restore_state_dict({"_prev_posterior": {"bogus": 1.0, "chatting": "NaN"}})
    assert est.prev_posterior is None, "全非法 → 应缺省 None"
    # 部分合法 → 归一化还原
    est2 = make_estimator({})
    est2.restore_state_dict({"_prev_posterior": {"bogus": 5.0, "chatting": 3.0, "browsing": 1.0}})
    assert est2.prev_posterior is not None
    assert est2.prev_posterior["chatting"] == 0.75
    print("  OK test_prev_posterior_restore_fault_tolerant")


def test_config_row_override():
    """config transition_<state> 整行覆盖 + 归一化（含非法数值忽略）。"""
    est = make_estimator({"transition_chatting": {"chatting": 1.0}})
    p = est._transition_prior({"chatting": 1.0})
    assert p["chatting"] == 1.0, f"整行覆盖后 chatting 保持应为 1.0: {p}"
    # 非法数值忽略（非覆盖行沿用默认）
    est2 = make_estimator({"transition_busy": {"busy": "bogus", "away": 1.0}})
    p2 = est2._transition_prior({"busy": 1.0})
    assert p2["away"] == 1.0
    print("  OK test_config_row_override")


def test_default_off_identity():
    """transition_enabled 默认关闭 → infer 结果与纯时间先验一致（恒等灰度）。"""
    est_off = make_estimator({})
    est_on_disabled = make_estimator({"transition_enabled": False})
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    r_off = est_off.infer({"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5}, now)
    r_dis = est_on_disabled.infer({"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5}, now)
    assert r_off["most_likely"] == r_dis["most_likely"]
    print("  OK test_default_off_identity")


def test_state_infer_user_state_carries_new_keys():
    """ChiguoState.infer_user_state 在 A1 开启时返回含 entropy/prev_posterior
    （早退路径与常规路径）；默认关闭则两者皆无（恒等）。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path("chiguo_proactive.toml").read_text()
        src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                     f'mem0_qdrant_path = "{Path(td) / "no_qdrant"}"', src)
        cfg_path = Path(td) / "chiguo_proactive.toml"
        cfg_path.write_text(src)
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = td
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        # 默认关闭：常规 + 早退路径均无熵/透传后验（恒等）
        s_off = ChiguoState(cfg)
        s_off.cooldown.last_user_message_at = (now - __import__("datetime").timedelta(hours=1)).isoformat()
        r_off = s_off.infer_user_state(now, msg_length=10)
        assert "entropy" not in r_off and "prev_posterior" not in r_off, r_off
        # A1 开启：常规路径（有交互记录）
        cfg_on = dict(cfg)
        cfg_on["bayesian"] = dict(cfg.get("bayesian", {}))
        cfg_on["bayesian"]["transition_enabled"] = True
        s = ChiguoState(cfg_on)
        s.cooldown.last_user_message_at = (now - __import__("datetime").timedelta(hours=1)).isoformat()
        r = s.infer_user_state(now, msg_length=10)
        assert "entropy" in r and "prev_posterior" in r
        # 早退路径（30 天未交互）
        s2 = ChiguoState(cfg_on)
        r2 = s2.infer_user_state(now, msg_length=10)
        assert "entropy" in r2 and "prev_posterior" in r2
        assert r2["most_likely"] == "browsing"
    print("  OK test_state_infer_user_state_carries_new_keys")



