"""tests/test_v2_replay.py — replay：历史事件重放跑 planner（不发送、不触碰生产库）。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.replay import replay  # noqa: E402
from storage.events import EventStore  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=CST)
CONFIG = {"emotion": {}, "schedule": {"quiet_start": 0, "quiet_end": 8},
          "planning": {}}


def _counts(db) -> dict:
    conn = db.connect()
    out = {}
    for t in ("intents", "actions", "deliveries", "autonomous_turns", "opportunities"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


def test_replay_reproduces_decisions_without_side_effects(tmp_path):
    prod = Database(tmp_path / "prod.sqlite")
    migrate(prod)
    store = EventStore(prod)
    store.append("commitment.created", source="extractor", occurred_at=T0,
                 payload={"kind": "user_event", "subject": "线代考试",
                          "due_at": (T0 + timedelta(days=1)).isoformat()})
    store.append("wake", source="scheduler", occurred_at=T0 + timedelta(hours=1))
    store.append("wake", source="scheduler",
                 occurred_at=T0 + timedelta(days=1, hours=2))
    before = _counts(prod)

    results = replay(prod, CONFIG, since=T0 - timedelta(minutes=1),
                     until=T0 + timedelta(days=2))
    assert [r.outcome for r in results] == ["waited", "intent"]
    assert results[1].intent_type == "follow_up"
    assert results[1].why["opportunity_kind"] == "commitment_due"

    # 生产库零副作用
    assert _counts(prod) == before
    conn = prod.connect()
    assert conn.execute("SELECT COUNT(*) FROM runtime_checkpoints").fetchone()[0] == 0


def test_replay_window_and_limit(tmp_path):
    prod = Database(tmp_path / "prod.sqlite")
    migrate(prod)
    store = EventStore(prod)
    due = T0 + timedelta(hours=3)
    store.append("commitment.created", source="extractor", occurred_at=T0,
                 payload={"kind": "user_event", "subject": "交材料",
                          "due_at": due.isoformat()})
    for i in range(4):
        store.append("wake", source="scheduler",
                     occurred_at=T0 + timedelta(hours=i + 1))
    # 窗口只覆盖后两次 wake（都已到期）
    results = replay(prod, CONFIG, since=T0 + timedelta(hours=3),
                     until=T0 + timedelta(hours=5))
    assert len(results) == 2
    assert all(r.outcome == "intent" for r in results)
    # limit 截断
    results = replay(prod, CONFIG, since=T0, until=T0 + timedelta(hours=5), limit=1)
    assert len(results) == 1


def test_replay_ignores_preexisting_projections(tmp_path):
    """副本里已物化的派生行不得再次投影（否则机会重复/张力翻倍）。"""
    from app.runtime.reducer import Reducer
    prod = Database(tmp_path / "prod.sqlite")
    migrate(prod)
    store = EventStore(prod)
    store.append("thread.opened", source="reducer",
                 occurred_at=T0 - timedelta(days=2), payload={"subject": "考试怎么样"})
    store.append("wake", source="scheduler", occurred_at=T0)
    Reducer(prod, CONFIG).catch_up()   # 生产库先物化（thread 行已存在）
    results = replay(prod, CONFIG, since=T0 - timedelta(minutes=1),
                     until=T0 + timedelta(hours=1))
    assert len(results) == 1
    assert results[0].opportunities.count("open_thread") == 1  # 未重置会变 2
