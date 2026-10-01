"""tests/test_v2_autonomous.py — 自主回合（autonomous_turn）与 scenario 测试。

覆盖用户验收场景 1-4：
1. 「明天考试」→ 承诺 → 到期 → follow-up 意图（完整因果链）；
2. 长期无世界事件 → 孤独不单独无限主动（无机会 → waited）；
3. 天气 + open thread → weather 作 secondary cue 而非 primary；
4. 静默窗口 → deferred（携带候选意图）。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.autonomous.turn import autonomous_turn  # noqa: E402
from storage.events import EventStore  # noqa: E402
from storage.repositories.commitments import CommitmentRepo  # noqa: E402
from storage.repositories.drives import IntentRepo  # noqa: E402
from storage.repositories.turns import TurnRepo  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 20, 0, tzinfo=CST)
CONFIG = {
    "emotion": {},
    "schedule": {"quiet_start": 0, "quiet_end": 8},
    "planning": {},
    "netease": {"enabled": False},   # 测试不触发网络
    "weather": {"enabled": False},
}


def _config(tmp_path) -> dict:
    return {**CONFIG, "_base_dir": str(tmp_path)}


def _setup(tmp_path, config=None):
    db = Database(tmp_path / "c.sqlite")
    migrate(db)
    return db, EventStore(db)


def test_scenario_1_exam_commitment_to_follow_up(tmp_path):
    """用户说「明天考线代」→ 提取承诺 → 次日到期 → follow-up 意图。"""
    db, store = _setup(tmp_path)

    # Day 0 晚：用户消息（含日期词 + 事件词）
    store.append("message.received", source="wechat", occurred_at=T0,
                 payload={"text": "明天考线代", "analysis": {"warmth": 0.3}})
    r1 = autonomous_turn(db=db, config=_config(tmp_path), reason="cron", now=T0 + timedelta(minutes=5))
    assert r1.outcome in ("waited", "deferred"), "考试前无到期承诺 → 不该产意图"
    open_c = CommitmentRepo(db).list_open()
    assert len(open_c) == 1, "提取器应把「明天考线代」记为 open commitment"
    assert open_c[0].subject

    # Day 1 晚：考试结束（承诺到期过后）
    later = T0 + timedelta(days=1, hours=2)
    r2 = autonomous_turn(db=db, config=_config(tmp_path), reason="cron", now=later)
    assert r2.outcome == "intent"
    assert r2.intent_id is not None
    intent = IntentRepo(db).get(r2.intent_id)
    assert intent.type == "follow_up"
    assert "commitment_due" in str(intent.why)

    # 因果链可回溯：message → commitment → （turn 记录）
    turns = TurnRepo(db).recent(limit=5)
    assert turns and turns[0].id == r2.turn_id


def test_scenario_2_loneliness_alone_waits(tmp_path):
    """无世界事件、长期孤独 → waited（不因孤独单独发送）。"""
    db, store = _setup(tmp_path)
    store.append("message.received", source="wechat",
                 occurred_at=T0 - timedelta(days=3), payload={"text": "嗯"})
    r = autonomous_turn(db=db, config=_config(tmp_path), reason="heartbeat",
                        now=T0 + timedelta(days=3))
    assert r.outcome == "waited"
    assert r.intent_id is None


def test_scenario_3_weather_is_secondary_not_primary(tmp_path):
    """天气 + open thread → primary 是 open_thread，天气进 secondary。"""
    db, store = _setup(tmp_path)
    store.append("thread.opened", source="reducer",
                 occurred_at=T0 - timedelta(days=2),
                 payload={"subject": "考试怎么样"})
    store.append("weather.changed", source="weather", occurred_at=T0,
                 payload={"observed_at": T0.isoformat(),
                          "expires_at": (T0 + timedelta(minutes=30)).isoformat(),
                          "data": {"condition": "雨", "temperature": 16}})
    r = autonomous_turn(db=db, config=_config(tmp_path), reason="cron", now=T0)
    assert r.outcome == "intent"
    intent = IntentRepo(db).get(r.intent_id)
    assert intent.type == "follow_up"          # open_thread → follow_up
    assert "open_thread" in str(intent.why)
    assert "weather" in str(intent.plan)        # secondary cue 保留


def test_scenario_4_quiet_hours_defers(tmp_path):
    """静默窗口内机会（承诺到期）→ deferred，候选意图保留。"""
    db, store = _setup(tmp_path)
    store.append("commitment.created", source="extractor",
                 occurred_at=T0 - timedelta(days=1),
                 payload={"kind": "user_event", "subject": "交材料",
                          "due_at": (T0 - timedelta(hours=2)).isoformat()})
    night = (T0 + timedelta(days=1)).replace(hour=3)  # 次日 03:00 ∈ [0,8) 静默（承诺已到期）
    r = autonomous_turn(db=db, config=_config(tmp_path), reason="cron", now=night)
    assert r.outcome == "deferred"
    turn = TurnRepo(db).get(r.turn_id)
    assert "quiet" in str(turn.state_snapshot) or "quiet" in str(turn.outcome)


def test_scenario_5_mem0_unavailable_core_still_works(tmp_path, monkeypatch):
    """Mem0 不可用：structured state 与核心运行不受影响（v2 核心不依赖 mem0）。"""
    monkeypatch.setenv("CHIGUO_MEM0_DISABLED", "1")
    db, store = _setup(tmp_path)
    cfg = {**_config(tmp_path),
           "memory": {"backend": "mem0",
                      "mem0_qdrant_path": str(tmp_path / "nonexistent-qdrant"),
                      "mem0_history_db": str(tmp_path / "nonexistent.db")}}
    store.append("commitment.created", source="extractor",
                 occurred_at=T0 - timedelta(hours=3),
                 payload={"kind": "user_event", "subject": "面试",
                          "due_at": (T0 - timedelta(hours=2)).isoformat()})
    r = autonomous_turn(db=db, config=cfg, reason="cron", now=T0)
    assert r.outcome == "intent"          # 核心链路照常产出意图
    assert r.intent_id is not None


def test_scenario_6_corrupt_db_fails_fast(tmp_path):
    """SQLite 损坏 → fail-fast（StorageError），不静默重建。"""
    from storage.sqlite.db import StorageError
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"garbage" * 128)
    db = Database(bad)
    try:
        autonomous_turn(db=db, config=_config(tmp_path), reason="cron", now=T0)
        raised = False
    except StorageError:
        raised = True
    assert raised, "损坏数据库必须 fail-fast（StorageError），交由备份恢复流程处理"
