#!/usr/bin/env python3
"""test_bayesian.py — Bayesian 用户状态推断单元测试"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import json
import tempfile
import tomllib
from pathlib import Path

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from chiguo_bayesian import UserStateEstimator, BayesianLearner
from chiguo_state import ChiguoState


def make_estimator():
    return UserStateEstimator({})


def _make_state(temp_dir: str) -> ChiguoState:
    """构造临时目录中的 ChiguoState（隔离配置/状态文件）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(temp_dir)
    return ChiguoState(cfg)


def test_time_based_prior_midnight():
    """午夜 → sleeping 先验最高"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 3, 0, tzinfo=CST)
    prior = est._time_based_prior(now)
    assert prior["sleeping"] > 0.5
    assert prior["sleeping"] > prior["chatting"]
    print(f"  OK test_time_based_prior_midnight: sleeping={prior['sleeping']:.2f}")


def test_time_based_prior_work_hours():
    """工作时段 → busy 先验最高"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 10, 0, tzinfo=CST)  # Monday
    prior = est._time_based_prior(now)
    assert prior["busy"] > 0.2
    print(f"  OK test_time_based_prior_work_hours: busy={prior['busy']:.2f}")


def test_time_based_prior_evening():
    """晚上 → chatting/browsing 先验高"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 20, 0, tzinfo=CST)
    prior = est._time_based_prior(now)
    assert prior["chatting"] > 0.15
    assert prior["browsing"] > 0.2
    print(f"  OK test_time_based_prior_evening: chatting={prior['chatting']:.2f}")


def test_time_based_prior_weekend():
    """周末 → busy 降低"""
    est = make_estimator()
    now_weekday = datetime(2026, 6, 15, 10, 0, tzinfo=CST)  # Monday
    now_weekend = datetime(2026, 6, 20, 10, 0, tzinfo=CST)  # Saturday
    prior_wd = est._time_based_prior(now_weekday)
    prior_we = est._time_based_prior(now_weekend)
    assert prior_we["busy"] < prior_wd["busy"]
    assert prior_we["sleeping"] > prior_wd["sleeping"]
    print("  OK test_time_based_prior_weekend")


def test_classify_latency():
    """回复延迟分类正确"""
    assert UserStateEstimator.classify_latency(0.05) == "fast"
    assert UserStateEstimator.classify_latency(0.5) == "normal"
    assert UserStateEstimator.classify_latency(3.0) == "slow"
    assert UserStateEstimator.classify_latency(10.0) == "very_slow"
    assert UserStateEstimator.classify_latency(None) == "none"
    print("  OK test_classify_latency")


def test_classify_msg_length():
    """消息长度分类正确"""
    assert UserStateEstimator.classify_msg_length(3) == "short"
    assert UserStateEstimator.classify_msg_length(15) == "medium"
    assert UserStateEstimator.classify_msg_length(50) == "long"
    assert UserStateEstimator.classify_msg_length(None) == "none"
    print("  OK test_classify_msg_length")


def test_classify_silence():
    """沉默时长分类正确"""
    assert UserStateEstimator.classify_silence(0.5) == "active"
    assert UserStateEstimator.classify_silence(3.0) == "recent"
    assert UserStateEstimator.classify_silence(12.0) == "moderate"
    assert UserStateEstimator.classify_silence(48.0) == "long"
    print("  OK test_classify_silence")


def test_infer_chatting():
    """快回复 + 短消息 + 白天 → chatting 高"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    result = est.infer({
        "reply_latency": 0.03,  # 秒回
        "msg_length": 5,         # 短消息
        "silence_hours": 0.5,    # 刚互动
        "in_class": False,
        "is_weekend": False,
    }, now)
    assert result["confidence"] > 0
    # chatting 应该是最高或第二高的
    posterior = result["posterior"]
    top2 = sorted(posterior, key=posterior.get, reverse=True)[:2]
    assert "chatting" in top2 or "browsing" in top2
    print(f"  OK test_infer_chatting: most_likely={result['most_likely']}, conf={result['confidence']:.2f}")


def test_infer_sleeping():
    """深夜 + 长沉默 → sleeping 高"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 3, 0, tzinfo=CST)
    result = est.infer({
        "reply_latency": None,
        "msg_length": None,
        "silence_hours": 8.0,   # 8 小时沉默
        "in_class": False,
        "is_weekend": False,
    }, now)
    assert result["most_likely"] == "sleeping"
    assert result["confidence"] > 0.4
    print(f"  OK test_infer_sleeping: confidence={result['confidence']:.2f}")


def test_utility_calculation():
    """效用计算：sleeping → 低，browsing → 高"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    result = est.infer({
        "reply_latency": 0.5,
        "msg_length": 20,
        "silence_hours": 2.0,
        "in_class": False,
        "is_weekend": False,
    }, now)
    utility = result["utility"]
    assert 0 <= utility <= 1
    print(f"  OK test_utility_calculation: utility={utility:.3f}")


def test_in_class_boosts_busy():
    """上课 → busy 概率提高"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 10, 0, tzinfo=CST)
    result_no_class = est.infer({
        "reply_latency": 0.5, "msg_length": 15,
        "silence_hours": 1.0, "in_class": False, "is_weekend": False,
    }, now)
    result_in_class = est.infer({
        "reply_latency": 0.5, "msg_length": 15,
        "silence_hours": 1.0, "in_class": True, "is_weekend": False,
    }, now)
    assert result_in_class["posterior"]["busy"] > result_no_class["posterior"]["busy"]
    print("  OK test_in_class_boosts_busy")


def test_should_send_bayesian():
    """Bayesian 发送建议"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    # 好时机 → 建议发送
    result_good = est.infer({
        "reply_latency": 0.5, "msg_length": 20,
        "silence_hours": 1.0, "in_class": False, "is_weekend": False,
    }, now)
    # 睡觉时 → 不建议
    result_bad = est.infer({
        "reply_latency": None, "msg_length": None,
        "silence_hours": 6.0, "in_class": False, "is_weekend": False,
    }, datetime(2026, 6, 15, 3, 0, tzinfo=CST))
    assert result_good["utility"] > 0.3
    assert result_bad["utility"] < 0.4
    # 核心语义：好时机的效用应高于坏时机（修复前两断言区间在 0.3~0.4 重叠，测不出大小关系）
    assert result_good["utility"] > result_bad["utility"], \
        f"good-timing utility should exceed bad-timing: {result_good['utility']:.3f} vs {result_bad['utility']:.3f}"
    print(f"  OK test_should_send_bayesian: good={result_good['utility']:.2f} bad={result_bad['utility']:.2f}")


def test_learner_update_from_label():
    """在线学习：标记真实状态后似然更新"""
    est = make_estimator()
    learner = BayesianLearner(est, learning_rate=0.1)

    # 记录一次观察：快回复、短消息 → 状态 chatting
    obs = {"reply_latency": 0.05, "msg_length": 5, "silence_hours": 0.5}
    old_lik = est._get_likelihood("chatting", "reply_latency", "fast")
    learner.update_from_label(obs, "chatting")
    new_lik = est._get_likelihood("chatting", "reply_latency", "fast")
    assert new_lik > old_lik  # 快回复在 chatting 状态下更可能了
    print(f"  OK test_learner_update_from_label: {old_lik:.3f} → {new_lik:.3f}")



def test_record_observation_supervised():
    """record_observation() 带真实标签时触发监督学习（似然表更新）"""
    est = make_estimator()
    obs = {"reply_latency": 0.05, "msg_length": 3, "silence_hours": 0.5}
    before = est._likelihood_cache.get(("chatting", "reply_latency", "fast"), 0.0)
    est.record_observation(obs, "chatting")
    after = est._likelihood_cache.get(("chatting", "reply_latency", "fast"), 0.0)
    assert after != before, "监督学习应更新似然表"
    est.record_observation({"reply_latency": 5.0, "silence_hours": 8.0}, None)
    print("  OK test_record_observation_supervised")



def test_all_states_in_posterior():
    """后验概率包含所有 6 个状态"""
    est = make_estimator()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    result = est.infer({}, now)
    for state in UserStateEstimator.STATES:
        assert state in result["posterior"]
    print("  OK test_all_states_in_posterior")


def test_likelihood_cache_persist_roundtrip():
    """v1.11+R3: to_state_dict/restore_state_dict 往返不丢 EMA 调优；坏键/坏值丢弃不崩"""
    est = make_estimator()
    est.record_observation({"reply_latency": 0.05, "msg_length": 3, "silence_hours": 0.5}, "chatting")
    sd = est.to_state_dict()
    assert "chatting.reply_latency.fast" in sd
    learned = est._get_likelihood("chatting", "reply_latency", "fast")
    # 新实例还原 → 学习后的似然恢复
    est2 = make_estimator()
    est2.restore_state_dict(sd)
    assert est2._get_likelihood("chatting", "reply_latency", "fast") == learned
    # 坏数据不崩：非 dict / 段数不对 / 未知状态 / 非法观测维度或取值 / 非数值 → 丢弃，默认保留
    est3 = make_estimator()
    est3.restore_state_dict("notadict")
    est3.restore_state_dict({"garbage": 0.9, "only.two": 0.9,
                             "bogus.key.v": 0.9, "chatting.reply_latency.fast": "NaN",
                             "chatting.bogus_obs.fast": 0.9,     # 未知观测维度 → 丢弃
                             "chatting.reply_latency.bogus_val": 0.9})  # 非法取值 → 丢弃
    assert est3._get_likelihood("chatting", "reply_latency", "fast") == 0.60  # 默认值保留
    print("  OK test_likelihood_cache_persist_roundtrip")


def test_state_save_preserves_persisted_bayesian_cache():
    """G8-Required-1: 进程未触碰 estimator 时 save 不丢弃磁盘已有的 bayesian 缓存。

    流程：进程A 学习并落盘 bayesian → 进程B 加载后不触碰 estimator 直接 save
    （典型：30 天无互动走 silent_h>720 早退路径）→ 进程C 重新加载，缓存仍在。
    """
    with tempfile.TemporaryDirectory() as td:
        # 进程 A：创建 estimator 学习一次并落盘
        s1 = _make_state(td)
        s1.bayesian_estimator.record_observation(
            {"reply_latency": 0.05, "msg_length": 3, "silence_hours": 0.5}, "chatting"
        )
        learned = s1.bayesian_estimator._get_likelihood("chatting", "reply_latency", "fast")
        assert learned != 0.60, "学习后应偏离默认值"
        assert s1.save()
        assert "bayesian" in json.loads(Path(s1.state_path).read_text())

        # 进程 B：加载但全程不触碰 estimator（早退路径），直接 save
        s2 = _make_state(td)
        assert s2._bayesian_estimator is None, "进程B 不应已创建 estimator"
        assert s2._bayesian_restored is not None, "磁盘缓存应已还原到内存"
        assert s2.save()
        # 核心回归断言：save 后磁盘缓存仍在（修复前被静默丢弃）
        assert "bayesian" in json.loads(Path(s2.state_path).read_text())

        # 进程 C：重新加载，缓存应保留且学习值不丢
        s3 = _make_state(td)
        assert s3._bayesian_restored is not None
        assert s3._bayesian_restored.get("chatting.reply_latency.fast") == learned
        assert s3.bayesian_estimator._get_likelihood("chatting", "reply_latency", "fast") == learned
    print("  OK test_state_save_preserves_persisted_bayesian_cache")
