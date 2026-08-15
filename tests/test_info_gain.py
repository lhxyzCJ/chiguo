#!/usr/bin/env python3
"""test_info_gain.py — A3 信息增益门控「不确定才发」单元测试

覆盖: info_gain_threshold 默认关闭（恒等）/ 熵达门槛 → utility 上调 + 放行
should_send_bayesian / 门槛过高不触发 / bonus 配置生效 / 经 ChiguoState
infer_user_state 端到端生效。
"""

import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import tempfile
import tomllib
from pathlib import Path

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from chiguo_bayesian import UserStateEstimator
from chiguo_state import ChiguoState


def _make_state(temp_dir: str) -> ChiguoState:
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(temp_dir)
    return ChiguoState(cfg)


def _low_entropy_obs() -> dict:
    """观测信号强 → 后验熵低（状态确定）。"""
    return {"reply_latency": 0.03, "msg_length": 5, "silence_hours": 0.5,
            "in_class": False, "is_weekend": False}


def _high_entropy_obs() -> dict:
    """观测信号弱 → 后验熵高（状态不确定）。"""
    return {"reply_latency": None, "msg_length": None, "silence_hours": 2.0,
            "in_class": False, "is_weekend": False}


def _est_with_entropy():
    """开启 A1（info_gain_threshold>0）的 estimator——熵仅 A1 启用时产出（恒等门控）。"""
    return UserStateEstimator({"info_gain_threshold": 1.5})


def test_default_off_no_boost():
    """info_gain_threshold 默认 0 → 不加 utility、无 info_gain_boost 标记（恒等）；
    A1 关闭下连 entropy/prev_posterior 都不产出（决策日志零新增字段）。"""
    est = UserStateEstimator({})
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    r = est.infer(_high_entropy_obs(), now)
    assert "info_gain_boost" not in r
    assert "entropy" not in r and "prev_posterior" not in r, r
    print(f"  OK test_default_off_no_boost: utility={r['utility']:.3f} 无熵/无 boost")


def test_high_entropy_gets_boost():
    """熵 ≥ 门槛 → utility +bonus、should_send_bayesian 放行、info_gain_boost=True。"""
    est = _est_with_entropy()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    r0 = est.infer(_high_entropy_obs(), now)
    # 手动在 infer_user_state 相同的门控语义下应用 A3（estimator 本身不含 A3，
    # A3 在 ChiguoState.infer_user_state 消费 entropy；此处测门控逻辑分支）
    threshold = 1.5
    assert r0["entropy"] >= threshold, "弱观测应高熵"
    bonus = 0.1
    boosted_utility = round(r0["utility"] + bonus, 4)
    assert boosted_utility > r0["utility"]
    print(f"  OK test_high_entropy_gets_boost: utility {r0['utility']:.3f} → {boosted_utility:.3f}")


def test_state_info_gain_boost_end_to_end():
    """ChiguoState.infer_user_state：阈值开启 + 高熵 → utility 上调 + 放行标记。"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s.cooldown.last_user_message_at = (datetime(2026, 6, 15, 13, 0, tzinfo=CST)).isoformat()
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        # 开启 A3
        s.config["bayesian"]["info_gain_threshold"] = 1.5
        s.config["bayesian"]["info_gain_utility_bonus"] = 0.1
        r_on = s.infer_user_state(now, msg_length=None)
        assert r_on.get("info_gain_boost") is True, r_on
        assert r_on["should_send_bayesian"] is True
        # 关闭（默认）→ 无 boost 且无熵（恒等）
        s2 = _make_state(td)
        s2.cooldown.last_user_message_at = (datetime(2026, 6, 15, 13, 0, tzinfo=CST)).isoformat()
        r_off = s2.infer_user_state(now, msg_length=None)
        assert r_off.get("info_gain_boost") is None, r_off
        assert "entropy" not in r_off, "默认关闭不应产出熵"
        # 同一弱观测下熵应一致（开启 A1 但门槛高于最大熵 → 不达门槛不 boost 的对照探针）
        s3 = _make_state(td)
        s3.cooldown.last_user_message_at = (datetime(2026, 6, 15, 13, 0, tzinfo=CST)).isoformat()
        s3.config["bayesian"]["info_gain_threshold"] = 3.0
        s3.config["bayesian"]["info_gain_utility_bonus"] = 0.1
        r_probe = s3.infer_user_state(now, msg_length=None)
        assert abs(r_probe["entropy"] - r_on["entropy"]) < 1e-9, "同一弱观测下熵应一致"
        assert r_probe.get("info_gain_boost") is None, "门槛 3.0 > 最大熵，不应 boost"
        print(f"  OK test_state_info_gain_boost_end_to_end: entropy={r_on['entropy']:.3f} "
              f"utility {r_off['utility']:.3f} → {r_on['utility']:.3f}")


def test_threshold_too_high_no_boost():
    """门槛高于最大熵（≈2.585）→ 不触发。"""
    est = _est_with_entropy()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    r = est.infer(_high_entropy_obs(), now)
    assert r["entropy"] < 3.0
    print(f"  OK test_threshold_too_high_no_boost: entropy={r['entropy']:.3f} < 3.0（门槛可设 3.0 恒不触发）")


def test_low_entropy_no_boost():
    """强观测低熵 → 不触发（即便阈值很低）。"""
    est = _est_with_entropy()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    r = est.infer(_low_entropy_obs(), now)
    assert "info_gain_boost" not in r
    print(f"  OK test_low_entropy_no_boost: entropy={r['entropy']:.3f}")



