"""tests/test_v2_reducer.py — app.runtime.reducer 事件 → 物化状态 TDD。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.reducer import Reducer  # noqa: E402
from storage.events import EventStore  # noqa: E402
from storage.repositories.commitments import CommitmentRepo  # noqa: E402
from storage.repositories.observations import ObservationRepo  # noqa: E402
from storage.repositories.threads import ThreadRepo  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=CST)
CONFIG = {"emotion": {}}


def _setup(tmp_path):
    db = Database(tmp_path / "c.sqlite")
    migrate(db)
    return db, EventStore(db)


def test_empty_catch_up_and_initial_state(tmp_path):
    db, store = _setup(tmp_path)
    r = Reducer(db, CONFIG)
    assert r.catch_up(limit=100) == 0
    st = r.current()
    assert st.affect.loneliness == 15.0
    assert st.affect.energy == 85.0
    assert st.relationship.closeness == 0.5
    assert st.last_event_id is None


def test_message_received_updates_affect_and_relationship(tmp_path):
    db, store = _setup(tmp_path)
    store.append("message.received", source="wechat", occurred_at=T0,
                 payload={"text": "菓菓我回来啦", "analysis": {"warmth": 0.8}})
    r = Reducer(db, CONFIG)
    assert r.catch_up() == 1
    st = r.current()
    assert st.affect.affection > 55.0          # 温暖回复 → 好感增
    assert st.affect.loneliness < 15.0         # 收到回复 → 孤独骤降
    assert st.relationship.recent_warmth > 0   # 关系域感知温暖
    assert st.last_event_id is not None


def test_time_gap_applies_tick(tmp_path):
    db, store = _setup(tmp_path)
    store.append("message.received", source="wechat", occurred_at=T0, payload={"text": "在吗"})
    store.append("wake", source="scheduler", occurred_at=T0 + timedelta(hours=40))
    r = Reducer(db, CONFIG)
    r.catch_up()
    st = r.current()
    assert st.affect.loneliness > 30.0, "40h 无互动 → 孤独按半衰期推进"


def test_message_sent_costs_energy(tmp_path):
    db, store = _setup(tmp_path)
    store.append("message.sent", source="wechat", occurred_at=T0,
                 payload={"text": "哼，才没有等你", "trigger": "lonely_mid"})
    r = Reducer(db, CONFIG)
    r.catch_up()
    assert r.current().affect.energy == 65.0   # 85 - 20
    assert r.current().relationship.initiative_balance > 0.0


def test_commitment_projection(tmp_path):
    db, store = _setup(tmp_path)
    created = store.append("commitment.created", source="extractor", occurred_at=T0,
                           payload={"kind": "user_event", "subject": "线代考试",
                                    "due_at": (T0 + timedelta(days=1)).isoformat()})
    store.append("commitment.resolved", source="planner", occurred_at=T0 + timedelta(days=1),
                 payload={"commitment_id": None, "subject": "线代考试"})
    r = Reducer(db, CONFIG)
    r.catch_up()
    repo = CommitmentRepo(db)
    assert repo.list_open() == []  # created 后即被 resolved
    # 再单独验证 created 投影
    db2, store2 = _setup(tmp_path / "b")
    store2.append("commitment.created", source="extractor", occurred_at=T0,
                  payload={"kind": "user_event", "subject": "交作业",
                           "due_at": (T0 + timedelta(days=2)).isoformat()})
    Reducer(db2, CONFIG).catch_up()
    open_items = CommitmentRepo(db2).list_open()
    assert len(open_items) == 1 and open_items[0].subject == "交作业"


def test_commitment_resolved_by_subject_fallback(tmp_path):
    db, store = _setup(tmp_path)
    store.append("commitment.created", source="extractor", occurred_at=T0,
                 payload={"kind": "user_event", "subject": "体检",
                          "due_at": (T0 + timedelta(days=1)).isoformat()})
    store.append("commitment.resolved", source="planner", occurred_at=T0 + timedelta(days=1),
                 payload={"subject": "体检"})
    r = Reducer(db, CONFIG)
    r.catch_up()
    assert CommitmentRepo(db).list_open() == []


def test_thread_projection(tmp_path):
    db, store = _setup(tmp_path)
    store.append("thread.opened", source="reducer", occurred_at=T0,
                 payload={"subject": "线代考试"})
    r = Reducer(db, CONFIG)
    r.catch_up()
    repo = ThreadRepo(db)
    threads = repo.list_open()
    assert len(threads) == 1 and threads[0].subject == "线代考试"
    store.append("thread.closed", source="reducer", occurred_at=T0 + timedelta(days=1),
                 payload={"thread_id": threads[0].id})
    Reducer(db, CONFIG).catch_up()
    assert ThreadRepo(db).list_open() == []


def test_observation_projection(tmp_path):
    db, store = _setup(tmp_path)
    store.append("weather.changed", source="weather", occurred_at=T0,
                 payload={"observed_at": T0.isoformat(),
                          "expires_at": (T0 + timedelta(minutes=30)).isoformat(),
                          "data": {"condition": "雨", "temperature": 18}})
    r = Reducer(db, CONFIG)
    r.catch_up()
    rows = ObservationRepo(db).recent(limit=5)
    assert len(rows) == 1
    assert rows[0].type == "weather.changed"
    assert rows[0].payload["condition"] == "雨"


def test_legacy_schedule_change_reminder_projects_commitment(tmp_path):
    db, store = _setup(tmp_path)
    store.append("schedule.created", source="schedule", occurred_at=T0,
                 payload={"kind": "reminder",
                          "item": {"kind": "reminder", "when": {"date": "2026-10-08"},
                                   "label": "交材料"},
                          "actor": "wechat_command"})
    r = Reducer(db, CONFIG)
    r.catch_up()
    items = CommitmentRepo(db).list_open()
    assert len(items) == 1 and items[0].subject == "交材料"
    assert items[0].due_at.date().isoformat() == "2026-10-08"


def test_catch_up_idempotent_and_checkpoint_persists(tmp_path):
    db, store = _setup(tmp_path)
    store.append("message.received", source="wechat", occurred_at=T0,
                 payload={"text": "早", "analysis": {"warmth": 0.5}})
    r1 = Reducer(db, CONFIG)
    assert r1.catch_up() == 1
    snap1 = r1.current()
    # 同一实例重跑 → 零消费、状态不变
    assert r1.catch_up() == 0
    assert r1.current().affect == snap1.affect
    # 新实例（模拟新进程）从检查点恢复
    r2 = Reducer(db, CONFIG)
    assert r2.catch_up() == 0
    st2 = r2.current()
    assert st2.affect.loneliness == snap1.affect.loneliness
    assert st2.affect.affection == snap1.affect.affection
    assert st2.last_event_id == snap1.last_event_id


def test_quiet_window_constraint_helper(tmp_path):
    """in_quiet_hours：跨午夜窗口语义与旧 cooldown 一致（qe 不含）。"""
    from app.runtime.reducer import in_quiet_hours
    assert in_quiet_hours(datetime(2026, 10, 1, 3, 0, tzinfo=CST), 0, 8) is True
    assert in_quiet_hours(datetime(2026, 10, 1, 9, 0, tzinfo=CST), 0, 8) is False
    assert in_quiet_hours(datetime(2026, 10, 1, 23, 0, tzinfo=CST), 22, 7) is True
    assert in_quiet_hours(datetime(2026, 10, 1, 12, 0, tzinfo=CST), 22, 7) is False
