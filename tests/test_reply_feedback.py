#!/usr/bin/env python3
"""test_reply_feedback.py — A2 分类型回复率反馈闭环单元测试

覆盖: reply_stats 统计记账（sent/replied）/ 状态落盘往返 / 默认关闭恒等 /
低回复率阻尼（权重下降）/ 高回复率微加成 / min_samples 样本下限保护 /
经 daemon record_send_text 与 record_user_message 的端到端归因。
"""

import os
import random
import re
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState
from chiguo_trigger import evaluate_triggers


def _make_state(tmp: str, now: datetime, **overrides) -> ChiguoState:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    s = ChiguoState(cfg)
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = now.strftime("%Y-%m-%d")
    for k, v in overrides.items():
        if hasattr(s.emotion, k):
            setattr(s.emotion, k, v)
        elif hasattr(s.cooldown, k):
            setattr(s.cooldown, k, v)
    return s


def _run_seeds(s: ChiguoState, now: datetime, n: int = 300, seed0: int = 1000) -> dict:
    counts: dict[str, int] = {}
    for i in range(n):
        random.seed(seed0 + i)
        t = evaluate_triggers(s, now)
        key = t.type if t else "None"
        counts[key] = counts.get(key, 0) + 1
    return counts


def test_reply_stats_accounting():
    """sent/replied 记账正确；无触发历史时 replied 零效果。"""
    s = _make_state(tempfile.mkdtemp(), datetime(2026, 6, 15, 14, 0, tzinfo=CST))
    s.record_trigger_sent("lonely_low")
    s.record_trigger_sent("lonely_low")
    s.record_trigger_sent("anxiety")
    s.cooldown.trigger_history.append("lonely_low")
    s.record_trigger_replied()
    assert s.cooldown.reply_stats["lonely_low"] == {"sent": 2, "replied": 1}
    assert s.cooldown.reply_stats["anxiety"] == {"sent": 1, "replied": 0}
    # 无历史 → replied 不崩
    s2 = _make_state(tempfile.mkdtemp(), datetime(2026, 6, 15, 14, 0, tzinfo=CST))
    s2.record_trigger_replied()
    assert s2.cooldown.reply_stats == {}
    print("  OK test_reply_stats_accounting")


def test_reply_stats_persist_roundtrip():
    """reply_stats 随状态落盘并还原（dataclass 字段自动持久化）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.record_trigger_sent("lonely_mid")
        s.record_trigger_sent("lonely_mid")
        s.cooldown.trigger_history.append("lonely_mid")
        s.record_trigger_replied()
        assert s.save()
        s2 = _make_state(td, now)
        assert s2.cooldown.reply_stats.get("lonely_mid") == {"sent": 2, "replied": 1}, \
            s2.cooldown.reply_stats
    print("  OK test_reply_stats_persist_roundtrip")


def test_default_off_identity():
    """reply_feedback_enabled=0（默认）→ 触发分布与未配置时一致（恒等）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s_base = _make_state(td, now, loneliness=90)
        s_base.cooldown.reply_stats = {"lonely_low": {"sent": 5, "replied": 0}}
        c_base = _run_seeds(s_base, now)
        s_off = _make_state(td, now, loneliness=90)
        s_off.config["trigger"]["reply_feedback_enabled"] = 0
        s_off.config["trigger"]["reply_feedback_damp"] = 1.0
        s_off.cooldown.reply_stats = {"lonely_low": {"sent": 5, "replied": 0}}
        c_off = _run_seeds(s_off, now)
        assert c_base == c_off, f"关闭应恒等: {c_base} vs {c_off}"
    print("  OK test_default_off_identity")


def test_low_reply_rate_damp():
    """低回复率（replied=0）→ 该类型权重 ×(1-damp)，damp=1 → 归零不被选中。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, loneliness=90)
        s.config["trigger"]["reply_feedback_enabled"] = 1
        s.config["trigger"]["reply_feedback_damp"] = 1.0
        s.config["trigger"]["reply_feedback_min_samples"] = 3
        s.cooldown.reply_stats = {"lonely_low": {"sent": 5, "replied": 0}}
        c = _run_seeds(s, now)
        assert c.get("lonely_low", 0) == 0, f"damp=1 应归零 lonely_low: {c}"
        # 其余孤独类型仍可触发（只阻尼指定类型）
        assert sum(v for k, v in c.items() if k.startswith("lonely_")) > 0
    print("  OK test_low_reply_rate_damp")


def test_high_reply_rate_boost():
    """高回复率（replied=sent）→ 该类型权重 ×(1+boost)，选中占比显著上升。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        # 基线（无反馈）：lonely_low 占比
        s_base = _make_state(td, now, loneliness=90)
        c_base = _run_seeds(s_base, now)
        base_low = c_base.get("lonely_low", 0)
        # boost
        s_boost = _make_state(td, now, loneliness=90)
        s_boost.config["trigger"]["reply_feedback_enabled"] = 1
        s_boost.config["trigger"]["reply_feedback_boost"] = 5.0
        s_boost.config["trigger"]["reply_feedback_high_rate"] = 0.7
        s_boost.config["trigger"]["reply_feedback_min_samples"] = 3
        s_boost.cooldown.reply_stats = {"lonely_low": {"sent": 5, "replied": 5}}
        c_boost = _run_seeds(s_boost, now)
        boost_low = c_boost.get("lonely_low", 0)
        assert boost_low > base_low, f"boost 应抬升 lonely_low: {base_low} → {boost_low}"
        print(f"  OK test_high_reply_rate_boost: lonely_low {base_low} → {boost_low}")


def test_min_samples_guard():
    """sent < min_samples → 不调整（冷启动保护，权重保持默认）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, loneliness=90)
        s.config["trigger"]["reply_feedback_enabled"] = 1
        s.config["trigger"]["reply_feedback_damp"] = 1.0
        s.config["trigger"]["reply_feedback_min_samples"] = 10
        s.cooldown.reply_stats = {"lonely_low": {"sent": 2, "replied": 0}}  # 样本不足
        c = _run_seeds(s, now)
        assert c.get("lonely_low", 0) > 0, f"样本不足不应阻尼: {c}"
    print("  OK test_min_samples_guard")


def test_mid_rate_no_adjust():
    """回复率在 [low_rate, high_rate) 中段 → 不调整。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s_base = _make_state(td, now, loneliness=90)
        c_base = _run_seeds(s_base, now)
        s = _make_state(td, now, loneliness=90)
        s.config["trigger"]["reply_feedback_enabled"] = 1
        s.config["trigger"]["reply_feedback_damp"] = 1.0
        s.config["trigger"]["reply_feedback_boost"] = 5.0
        s.config["trigger"]["reply_feedback_min_samples"] = 3
        s.cooldown.reply_stats = {"lonely_low": {"sent": 10, "replied": 5}}  # 0.5 中段
        c = _run_seeds(s, now)
        assert c.get("lonely_low", 0) == c_base.get("lonely_low", 0), \
            f"中段不调整: {c_base.get('lonely_low')} vs {c.get('lonely_low')}"
    print("  OK test_mid_rate_no_adjust")


def test_daemon_record_send_persists_sent():
    """A2 修复：daemon record_send_text 立即落盘——cron --record-send 为一次性进程，
    sent+1 若不 save 会随进程退出丢失（replied 侧在 --user-msg 有 save，sent 侧必须同步）。"""
    from chiguo_daemon import DecisionEngine
    with tempfile.TemporaryDirectory() as td:
        src = Path("chiguo_proactive.toml").read_text()
        src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                     f'mem0_qdrant_path = "{Path(td) / "no_qdrant"}"', src)
        src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                     f'mem0_history_db = "{Path(td) / "no_history.db"}"', src)
        cfg_path = Path(td) / "chiguo_proactive.toml"
        cfg_path.write_text(src)
        dec_path = Path(td) / "decisions.jsonl"
        # 引擎 A：发送一条带 trigger 的消息（模拟 cron --record-send 进程）
        eng = DecisionEngine(str(cfg_path), str(dec_path))
        eng.state.cooldown.reply_stats = {}
        eng.record_send_text("msg-1", "哥哥今天好吗", trigger="lonely_low")
        assert eng.state.cooldown.reply_stats["lonely_low"]["sent"] == 1
        # 引擎 B：全新加载（等价下次进程）→ sent 计数应从磁盘还原
        eng2 = DecisionEngine(str(cfg_path), str(dec_path))
        st = eng2.state.cooldown.reply_stats.get("lonely_low")
        assert st is not None and st["sent"] == 1, f"record_send_text 后应持久化 sent: {st}"
    print("  OK test_daemon_record_send_persists_sent")


if __name__ == "__main__":
    print("test_reply_feedback.py\n")
    tests = [
        test_reply_stats_accounting,
        test_reply_stats_persist_roundtrip,
        test_default_off_identity,
        test_low_reply_rate_damp,
        test_high_reply_rate_boost,
        test_min_samples_guard,
        test_mid_rate_no_adjust,
        test_daemon_record_send_persists_sent,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} reply-feedback tests passed.")
