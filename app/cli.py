"""app.cli — Chiguo v2 CLI（debug/runtime interface 基座）。

Phase 2 提供 `chiguo db status|migrate|integrity|backup`。
约定：JSON → stdout，诊断 → stderr；成功 0 / 运行失败 1 / 用法错误 2。

DB 路径解析优先级：`--db` 参数 > toml `[storage].db_path`
（相对路径锚定 config 所在目录）> 默认 `~/.chiguo/chiguo.sqlite`。
"""
import argparse
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from chiguo_paths import PROJECT_ROOT
from chiguo_time import CST
from storage.events import EventStore
from storage.sqlite.db import Database, StorageError
from storage.sqlite.migrations import SCHEMA_VERSION, migrate

DEFAULT_DB_PATH = "~/.chiguo/chiguo.sqlite"
_COUNT_TABLES = ("events", "messages", "sessions", "commitments", "threads",
                 "opportunities", "drives", "intents", "actions", "deliveries",
                 "memories", "schedules", "world_observations", "autonomous_turns")


def resolve_db_path(db_arg: str | None, config_path: str | None = None) -> Path:
    """解析数据库路径（--db 优先；否则 toml [storage].db_path；否则默认）。"""
    if db_arg:
        return Path(db_arg).expanduser()
    cfg_path = Path(config_path) if config_path else PROJECT_ROOT / "chiguo_proactive.toml"
    db_path = DEFAULT_DB_PATH
    try:
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        configured = (cfg.get("storage", {}) or {}).get("db_path")
        if isinstance(configured, str) and configured.strip():
            db_path = configured.strip()
    except (OSError, tomllib.TOMLDecodeError):
        pass
    p = Path(db_path).expanduser()
    if not p.is_absolute():
        p = cfg_path.resolve().parent / p
    return p


def _print(obj: dict):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ── db 子命令 ──────────────────────────────────────────────

def cmd_db_status(args) -> int:
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "db_status", "ok": True, "initialized": False,
                "path": str(db.path), "schema_version": 0,
                "code_schema_version": SCHEMA_VERSION})
        return 0
    conn = db.connect()
    version = db.schema_version()
    counts = {}
    tables = set(db.table_names())
    for t in _COUNT_TABLES:
        if t in tables:
            counts[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    _print({
        "action": "db_status", "ok": True, "initialized": version > 0,
        "path": str(db.path),
        "schema_version": version,
        "code_schema_version": SCHEMA_VERSION,
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "size_bytes": db.path.stat().st_size,
        "counts": counts,
    })
    return 0


def cmd_db_migrate(args) -> int:
    db = Database(resolve_db_path(args.db, args.config))
    applied = migrate(db)
    _print({"action": "db_migrate", "ok": True, "path": str(db.path),
            "applied": applied, "schema_version": db.schema_version()})
    return 0


def cmd_db_integrity(args) -> int:
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "db_integrity", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    r = db.integrity()
    _print({"action": "db_integrity", "path": str(db.path), **r})
    return 0 if r["ok"] else 1


def cmd_db_backup(args) -> int:
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "db_backup", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    dest = (Path(args.dest).expanduser() if args.dest else
            db.path.parent / "backups" /
            f"chiguo-{datetime.now(CST).strftime('%Y%m%d-%H%M%S')}.sqlite")
    out = db.backup(dest)
    _print({"action": "db_backup", "ok": True, "path": str(db.path),
            "backup_path": str(out)})
    return 0


# ── events 子命令（因果链查看：回答「为什么」）──────────────

def _event_json(ev) -> dict:
    return {
        "event_id": ev.event_id,
        "type": ev.type,
        "occurred_at": ev.occurred_at.isoformat(),
        "observed_at": ev.observed_at.isoformat(),
        "source": ev.source,
        "correlation_id": ev.correlation_id,
        "causation_id": ev.causation_id,
        "payload": ev.payload,
    }


def cmd_events_recent(args) -> int:
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "events_recent", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    events = EventStore(db).recent(limit=args.limit, type=args.type, source=args.source)
    _print({"action": "events_recent", "ok": True, "count": len(events),
            "events": [_event_json(e) for e in events]})
    return 0


def cmd_events_show(args) -> int:
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "events_show", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    store = EventStore(db)
    ev = store.get(args.event_id)
    if ev is None:
        _print({"action": "events_show", "ok": False,
                "error": f"事件不存在: {args.event_id}"})
        return 1
    chain = store.chain(ev.event_id)
    caused = store.caused_by(ev.event_id)
    _print({"action": "events_show", "ok": True, "event": _event_json(ev),
            "chain": [_event_json(e) for e in chain],
            "caused_by": [_event_json(e) for e in caused]})
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chiguo", description="Chiguo v2 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db", help="数据库运维")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    for name, fn, help_text in (
            ("status", cmd_db_status, "数据库状态（路径/schema 版本/表计数）"),
            ("migrate", cmd_db_migrate, "应用待执行迁移（幂等）"),
            ("integrity", cmd_db_integrity, "integrity_check + foreign_key_check"),
            ("backup", cmd_db_backup, "在线备份（默认到 <db_dir>/backups/）")):
        p = db_sub.add_parser(name, help=help_text)
        p.add_argument("--db", default=None, help="数据库路径（默认读 toml [storage].db_path）")
        p.add_argument("--config", default=None, help="toml 配置路径")
        if name == "backup":
            p.add_argument("--dest", default=None, help="备份目标路径")

    events = sub.add_parser("events", help="事件日志查看（因果链）")
    ev_sub = events.add_subparsers(dest="events_command", required=True)
    p = ev_sub.add_parser("recent", help="最近事件（新→旧）")
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--type", default=None)
    p.add_argument("--source", default=None)
    p = ev_sub.add_parser("show", help="单事件 + 因果链（为什么）")
    p.add_argument("event_id")
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "events":
        handlers = {"recent": cmd_events_recent, "show": cmd_events_show}
        handler = handlers[args.events_command]
        action = f"events_{args.events_command}"
    else:
        handlers = {"status": cmd_db_status, "migrate": cmd_db_migrate,
                    "integrity": cmd_db_integrity, "backup": cmd_db_backup}
        handler = handlers[args.db_command]
        action = f"db_{args.db_command}"
    try:
        return handler(args)
    except StorageError as e:
        _print({"action": action, "ok": False, "error": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
