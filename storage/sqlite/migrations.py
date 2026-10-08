"""storage.sqlite.migrations — 顺序迁移（schema_migrations + checksum 防漂移）。

- 迁移在显式事务内执行（DDL 原子；逐语句提交，不用 executescript 的隐式 COMMIT）；
- 已应用迁移的 SQL 被修改（checksum 漂移）→ 拒绝启动（防 schema 静默分叉）；
- 数据库版本高于代码 SCHEMA_VERSION → SchemaTooNewError（fail-fast）。
"""
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database, SchemaTooNewError, StorageError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


# ── v1：初始 schema（设计见 docs/migration-plan.md §1）──────────────

_SQL_V1 = """
-- 注：schema_migrations 由迁移 runner 负责创建（migrate() 开头 IF NOT EXISTS），
-- 不属于任何迁移版本。

-- 事件日志（append-only；因果链载体）
CREATE TABLE events (
  event_id       TEXT PRIMARY KEY,
  type           TEXT NOT NULL,
  occurred_at    TEXT NOT NULL,
  observed_at    TEXT NOT NULL,
  source         TEXT NOT NULL,
  correlation_id TEXT,
  causation_id   TEXT,
  payload        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_type_time ON events(type, occurred_at);
CREATE INDEX idx_events_correlation ON events(correlation_id);
CREATE INDEX idx_events_causation ON events(causation_id);

-- 会话 / 消息（Pi transcript 的语义镜像）
CREATE TABLE sessions (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  pi_session_id TEXT,
  started_at    TEXT NOT NULL,
  ended_at      TEXT
);
CREATE TABLE messages (
  id          TEXT PRIMARY KEY,
  session_id  TEXT REFERENCES sessions(id),
  direction   TEXT NOT NULL,
  text        TEXT NOT NULL,
  at          TEXT NOT NULL,
  event_id    TEXT REFERENCES events(event_id),
  analysis    TEXT,
  delivery_id TEXT
);
CREATE INDEX idx_messages_time ON messages(at);

-- 物化状态（每域单行 current；变更由事件驱动）
CREATE TABLE affect_state (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  valence    REAL,
  arousal    REAL,
  energy     REAL,
  tension    REAL,
  updated_at TEXT NOT NULL,
  event_id   TEXT REFERENCES events(event_id)
);
CREATE TABLE relationship_state (
  id                   INTEGER PRIMARY KEY CHECK (id = 1),
  closeness            REAL,
  trust                REAL,
  familiarity          REAL,
  recent_warmth        REAL,
  recent_tension       REAL,
  interaction_rhythm   TEXT,
  initiative_balance   REAL,
  shared_history_depth REAL,
  updated_at           TEXT NOT NULL
);
CREATE TABLE self_state (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE user_state (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 承诺 / 线索（一等公民）
CREATE TABLE commitments (
  id                 TEXT PRIMARY KEY,
  kind               TEXT NOT NULL,
  subject            TEXT NOT NULL,
  details            TEXT,
  due_at             TEXT,
  status             TEXT NOT NULL DEFAULT 'open',
  created_from_event TEXT REFERENCES events(event_id),
  created_at         TEXT NOT NULL,
  resolved_at        TEXT,
  resolution_event   TEXT REFERENCES events(event_id)
);
CREATE INDEX idx_commitments_status_due ON commitments(status, due_at);
CREATE TABLE threads (
  id                  TEXT PRIMARY KEY,
  subject             TEXT NOT NULL,
  state               TEXT NOT NULL DEFAULT 'open',
  opened_at           TEXT NOT NULL,
  last_interaction_at TEXT,
  closed_at           TEXT,
  source              TEXT,
  payload             TEXT
);
CREATE INDEX idx_threads_state ON threads(state, last_interaction_at);

-- 机会 / 驱动力 / 意图 / 行动
CREATE TABLE opportunities (
  id                   TEXT PRIMARY KEY,
  kind                 TEXT NOT NULL,
  novelty              REAL,
  relevance            REAL,
  urgency              REAL,
  emotional_affordance REAL,
  expires_at           TEXT,
  observation_event_id TEXT REFERENCES events(event_id),
  status               TEXT NOT NULL DEFAULT 'open',
  created_at           TEXT NOT NULL,
  payload              TEXT
);
CREATE INDEX idx_opportunities_status ON opportunities(status, expires_at);
CREATE TABLE drives (
  id                TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,
  intensity         REAL NOT NULL,
  evaluated_at      TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'active',
  inputs            TEXT,
  autonomous_turn_id TEXT
);
CREATE TABLE intents (
  id                 TEXT PRIMARY KEY,
  type               TEXT NOT NULL,
  why                TEXT NOT NULL,
  plan               TEXT,
  created_at         TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'open',
  autonomous_turn_id TEXT
);
CREATE TABLE actions (
  id             TEXT PRIMARY KEY,
  type           TEXT NOT NULL,
  status         TEXT NOT NULL,
  intent_id      TEXT REFERENCES intents(id),
  cause_event_id TEXT REFERENCES events(event_id),
  correlation_id TEXT,
  created_at     TEXT NOT NULL,
  started_at     TEXT,
  completed_at   TEXT,
  input          TEXT,
  output         TEXT,
  error          TEXT
);
CREATE INDEX idx_actions_status ON actions(status, created_at);
CREATE TABLE deliveries (
  id                  TEXT PRIMARY KEY,
  action_id           TEXT REFERENCES actions(id),
  message_id          TEXT REFERENCES messages(id),
  channel             TEXT NOT NULL,
  status              TEXT NOT NULL,
  attempts            INTEGER NOT NULL DEFAULT 1,
  sent_at             TEXT,
  error               TEXT,
  provider_message_id TEXT
);

-- 观测（source 输出；与 *.observed 事件对应）
CREATE TABLE world_observations (
  id          TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  type        TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  expires_at  TEXT,
  event_id    TEXT REFERENCES events(event_id),
  payload     TEXT NOT NULL
);
CREATE INDEX idx_world_obs_source ON world_observations(source, observed_at);

-- 记忆（canonical；mem0 仅作语义索引，embedding 永不作为事实来源）
CREATE TABLE memories (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  text         TEXT NOT NULL,
  source       TEXT,
  origin       TEXT,
  confidence   REAL,
  observed_at  TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  valid_from   TEXT,
  valid_to     TEXT,
  status       TEXT NOT NULL DEFAULT 'active',
  importance   REAL,
  emotion_tag  TEXT,
  mem0_id      TEXT,
  meta         TEXT
);
CREATE INDEX idx_memories_kind_status ON memories(kind, status);
CREATE TABLE memory_links (
  from_memory_id TEXT NOT NULL REFERENCES memories(id),
  to_memory_id   TEXT NOT NULL REFERENCES memories(id),
  relation       TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (from_memory_id, to_memory_id, relation)
);

-- 调度（wake / deferred work；不含「要不要发」的决策）
CREATE TABLE schedules (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  due_at        TEXT,
  recurrence    TEXT,
  payload       TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  last_fired_at TEXT,
  next_fire_at  TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_schedules_next ON schedules(status, next_fire_at);

-- 自主回合审计（replay / shadow / 「为什么」）
CREATE TABLE autonomous_turns (
  id               TEXT PRIMARY KEY,
  reason           TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  event_window_from TEXT,
  event_window_to   TEXT,
  state_snapshot   TEXT,
  opportunity_ids  TEXT,
  drive_ids        TEXT,
  intent_id        TEXT,
  action_id        TEXT,
  outcome          TEXT
);
"""


# ── v2：运行时消费游标（reducer / extractor / scheduler 增量消费 events）──

_SQL_V2 = """
CREATE TABLE runtime_checkpoints (
  stream           TEXT PRIMARY KEY,
  last_event_id    TEXT,
  last_occurred_at TEXT,
  state            TEXT,
  updated_at       TEXT NOT NULL
);
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "init", _SQL_V1),
    Migration(2, "runtime_checkpoints", _SQL_V2),
)

SCHEMA_VERSION = MIGRATIONS[-1].version


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _statements(script: str):
    """按 sqlite3.complete_statement 切分 SQL 语句（正确处理字符串内分号）。"""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                yield stmt
            buf = ""
    tail = buf.strip()
    if tail:
        yield tail


def migrate(db: Database) -> list[int]:
    """应用未执行的迁移；返回本次应用的版本号列表（已最新 → 空列表）。"""
    conn = db.connect()
    with db.transaction() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r["version"]: r for r in
               conn.execute("SELECT version, name, checksum FROM schema_migrations")}
    max_applied = max(applied, default=0)
    if max_applied > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"数据库 schema 版本 {max_applied} 高于当前代码 {SCHEMA_VERSION}"
            f"（{db.path}）——请升级代码或恢复备份，拒绝继续写入")

    newly: list[int] = []
    for m in MIGRATIONS:
        row = applied.get(m.version)
        if row is not None:
            if row["checksum"] != _checksum(m.sql):
                raise StorageError(
                    f"迁移 {m.version}({m.name}) 校验和漂移：已应用版本与代码不一致"
                    f"（{db.path}）——迁移一经应用不得修改，请新增迁移版本")
            continue
        try:
            with db.transaction() as c:
                for stmt in _statements(m.sql):
                    c.execute(stmt)
                c.execute(
                    "INSERT INTO schema_migrations(version, name, checksum, applied_at)"
                    " VALUES (?, ?, ?, ?)",
                    (m.version, m.name, _checksum(m.sql),
                     datetime.now(CST).isoformat()))
        except sqlite3.DatabaseError as e:
            raise StorageError(f"迁移 {m.version}({m.name}) 执行失败: {e}") from e
        newly.append(m.version)
    return newly
