"""app.cli — Chiguo v2 CLI（debug/runtime interface 基座）。

Phase 2 提供 `chiguo db status|migrate|integrity|backup`。
约定：JSON → stdout，诊断 → stderr；成功 0 / 运行失败 1 / 用法错误 2。

DB 路径解析优先级：`--db` 参数 > toml `[storage].db_path`
（相对路径锚定 config 所在目录）> 默认 `~/.chiguo/chiguo.sqlite`。
"""
import argparse
import json
import os
import sqlite3
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
                 "schedules", "world_observations", "autonomous_turns")


def load_config(config_path: str | None = None) -> tuple[dict, Path]:
    """读取 toml 配置（缺省项目根 chiguo_proactive.toml）；失败 → 空配置。"""
    cfg_path = Path(config_path) if config_path else PROJECT_ROOT / "chiguo_proactive.toml"
    try:
        with open(cfg_path, "rb") as f:
            return tomllib.load(f), cfg_path
    except (OSError, tomllib.TOMLDecodeError):
        return {}, cfg_path


def resolve_db_path(db_arg: str | None, config_path: str | None = None) -> Path:
    """解析数据库路径（--db > env CHIGUO_DB_PATH > toml [storage].db_path > 默认）。"""
    if db_arg:
        return Path(db_arg).expanduser()
    env = os.environ.get("CHIGUO_DB_PATH")
    if env:
        return Path(env).expanduser()
    cfg, cfg_path = load_config(config_path)
    db_path = DEFAULT_DB_PATH
    configured = (cfg.get("storage", {}) or {}).get("db_path")
    if isinstance(configured, str) and configured.strip():
        db_path = configured.strip()
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


# ── commitments / threads 读取命令 ─────────────────────────────

def _jsonable(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in row.items()}


def cmd_commitments(args) -> int:
    from storage.repositories.commitments import CommitmentRepo
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "commitments", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    items = CommitmentRepo(db).list_open()
    _print({"action": "commitments", "ok": True, "count": len(items),
            "commitments": [_jsonable(vars(c)) for c in items]})
    return 0


def cmd_threads(args) -> int:
    from storage.repositories.threads import ThreadRepo
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "threads", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    items = ThreadRepo(db).list_open()
    _print({"action": "threads", "ok": True, "count": len(items),
            "threads": [_jsonable(vars(t)) for t in items]})
    return 0


# ── chiguo autonomous-turn（shadow 自主回合）────────────────────

def cmd_autonomous_turn(args) -> int:
    from app.autonomous.turn import autonomous_turn
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "autonomous_turn", "ok": False,
                "error": f"数据库不存在: {db.path}（先 chiguo db migrate）"})
        return 1
    cfg, cfg_path = load_config(args.config)
    cfg = dict(cfg)
    cfg.setdefault("_base_dir", str(cfg_path.resolve().parent))
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
    res = autonomous_turn(db=db, config=cfg, reason=args.reason or "manual",
                          now=now, execute=bool(args.execute))
    _print({"action": "autonomous_turn", "ok": True, "turn_id": res.turn_id,
            "outcome": res.outcome, "intent_id": res.intent_id,
            "action_id": res.action_id, "why": res.why})
    return 0


# ── chiguo execute（执行 pending action）───────────────────────

def cmd_execute(args) -> int:
    from app.actions.executor import build_generation_payload, execute_action
    from storage.repositories.actions import ActionRepo
    from storage.repositories.drives import IntentRepo
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "execute", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    cfg, cfg_path = load_config(args.config)
    cfg = dict(cfg)
    cfg.setdefault("_base_dir", str(cfg_path.resolve().parent))
    if args.dry_run:
        action = ActionRepo(db).get(args.action_id)
        if action is None:
            _print({"action": "execute", "ok": False,
                    "error": f"action 不存在: {args.action_id}"})
            return 1
        intent = IntentRepo(db).get(action.intent_id) if action.intent_id else None
        _print({"action": "execute", "ok": True, "dry_run": True,
                "action_id": action.id, "status": action.status,
                "prompt": build_generation_payload(intent, config=cfg)})
        return 0
    out = execute_action(db, args.action_id, config=cfg)
    _print({"action": "execute", "ok": out.ok, "status": out.status,
            "error": out.error, "text": out.text})
    return 0 if out.ok else 1


# ── chiguo tick（唤醒入口：wake → autonomous turn）───────────────

def cmd_tick(args) -> int:
    from app.actions.executor import execute_action
    from app.autonomous.turn import autonomous_turn
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "tick", "ok": False,
                "error": f"数据库不存在: {db.path}（先 chiguo db migrate）"})
        return 1
    cfg, cfg_path = load_config(args.config)
    cfg = dict(cfg)
    cfg.setdefault("_base_dir", str(cfg_path.resolve().parent))
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
    res = autonomous_turn(db=db, config=cfg, reason=args.reason or "cron",
                          now=now, execute=bool(args.execute))
    sent = None
    if args.execute and res.action_id:
        out = execute_action(db, res.action_id, config=cfg)
        sent = {"ok": out.ok, "status": out.status, "error": out.error}
    _print({"action": "tick", "ok": sent is None or sent["ok"],
            "turn_id": res.turn_id,
            "outcome": res.outcome, "intent_id": res.intent_id,
            "action_id": res.action_id, "sent": sent})
    # M8：--execute 下发送失败 → exit 1（cron/运维能看到失败）；shadow 恒 0
    if sent is not None and not sent["ok"]:
        return 1
    return 0


# ── chiguo serve（运行时回环 HTTP；Pi extension 服务端）─────────

def cmd_serve(args) -> int:
    from app.runtime.serve import DEFAULT_PORT, RuntimeServer
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "serve", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    cfg, cfg_path = load_config(args.config)
    cfg = dict(cfg)
    cfg.setdefault("_base_dir", str(cfg_path.resolve().parent))
    port = args.port if args.port is not None else DEFAULT_PORT  # --port 0 = 系统分配
    srv = RuntimeServer(db, cfg, port=port)
    srv.start()
    print(json.dumps({"action": "serve", "ok": True, "url":
                      f"http://127.0.0.1:{srv.port}"}, ensure_ascii=False),
          flush=True)
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
    return 0


# ── chiguo replay（历史重放，不发送不触碰生产库）────────────────

def cmd_replay(args) -> int:
    from app.runtime.replay import replay
    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "replay", "ok": False, "error": f"数据库不存在: {db.path}"})
        return 1
    cfg, cfg_path = load_config(args.config)
    cfg = dict(cfg)
    cfg.setdefault("_base_dir", str(cfg_path.resolve().parent))
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=CST)
    until = None
    if args.until:
        until = datetime.fromisoformat(args.until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=CST)
    results = replay(db, cfg, since=since, until=until, limit=args.limit)
    _print({"action": "replay", "ok": True, "count": len(results),
            "decisions": [{"event_id": r.event_id, "at": r.at.isoformat(),
                           "outcome": r.outcome, "intent_type": r.intent_type,
                           "why": r.why, "opportunities": list(r.opportunities)}
                          for r in results]})
    return 0


# ── chiguo status（聚合视图；只读，不消费事件）─────────────────

def cmd_status(args) -> int:
    from app.runtime.reducer import Reducer
    from storage.events import EventStore
    from storage.repositories.commitments import CommitmentRepo
    from storage.repositories.opportunities import OpportunityRepo
    from storage.repositories.threads import ThreadRepo
    from storage.repositories.turns import TurnRepo

    db = Database(resolve_db_path(args.db, args.config))
    if not db.path.exists():
        _print({"action": "status", "ok": True, "initialized": False,
                "path": str(db.path)})
        return 0
    cfg, _ = load_config(args.config)
    cfg = dict(cfg)
    cfg.setdefault("_base_dir", str(Path(args.config).resolve().parent if args.config
                                     else PROJECT_ROOT))

    state = Reducer(db, cfg).current()  # 只读检查点，不 catch_up
    commitments = CommitmentRepo(db).list_open()
    threads = ThreadRepo(db).list_open()
    opps = OpportunityRepo(db).list_open(now=datetime.now(CST))
    turns = TurnRepo(db).recent(limit=1)
    last_sent = EventStore(db).recent(limit=1, type="message.sent")
    _print({
        "action": "status", "ok": True, "initialized": True,
        "db": {"path": str(db.path), "schema_version": db.schema_version(),
               "size_bytes": db.path.stat().st_size},
        "affect": {
            "loneliness": round(state.affect.loneliness, 1),
            "affection": round(state.affect.affection, 1),
            "anxiety": round(state.affect.anxiety, 1),
            "energy": round(state.affect.energy, 1),
            "tsundere_index": round(state.affect.tsundere_index, 1),
            "dominant_layer": state.affect.dominant_layer,
        },
        "relationship": _jsonable(vars(state.relationship)),
        "commitments": {"count": len(commitments),
                        "items": [_jsonable(vars(c)) for c in commitments]},
        "threads": {"count": len(threads),
                    "threads": [_jsonable(vars(t)) for t in threads]},
        "opportunities_open": [
            {"kind": o.kind, "payload": o.payload, "expires_at": o.expires_at}
            for o in opps],
        "last_turn": (_jsonable(vars(turns[0])) if turns else None),
        "last_message_sent": (_event_json(last_sent[0]) if last_sent else None),
    })
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

    for name, help_text in (("commitments", "未完成承诺（open）"),
                            ("threads", "未结束话题（open）"),
                            ("status", "聚合状态（affect/关系/承诺/线程/最近回合）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--db", default=None)
        p.add_argument("--config", default=None)
    p = sub.add_parser("autonomous-turn",
                       help="自主回合（默认 shadow：只记录不发送）")
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--execute", action="store_true",
                   help="有意图时创建 pending action 行（仍不发送）")
    p.add_argument("--now", default=None, help="覆盖当前时间（ISO8601；调试/replay）")
    p.add_argument("--reason", default="manual")
    p = sub.add_parser("execute", help="执行 pending send_message action（生成+发送）")
    p.add_argument("action_id")
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true", help="只打印生成 prompt，不生成不发送")
    p = sub.add_parser("replay", help="历史事件重放（不发送、不触碰生产库）")
    p.add_argument("--since", required=True, help="起始时间（ISO8601）")
    p.add_argument("--until", default=None, help="截止时间（ISO8601，不含）")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None)
    p = sub.add_parser("tick", help="唤醒入口（默认 shadow；--execute 生成并发送）")
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--execute", action="store_true", help="执行 send（生成+bridge 发送）")
    p.add_argument("--now", default=None, help="覆盖当前时间（ISO8601）")
    p.add_argument("--reason", default="cron")
    p = sub.add_parser("serve", help="运行时回环 HTTP（Pi extension 服务端）")
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--port", type=int, default=None)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "events":
        handlers = {"recent": cmd_events_recent, "show": cmd_events_show}
        handler = handlers[args.events_command]
        action = f"events_{args.events_command}"
    elif args.command in ("commitments", "threads", "status", "autonomous-turn",
                          "execute", "replay", "tick", "serve"):
        handlers = {"commitments": cmd_commitments, "threads": cmd_threads,
                    "status": cmd_status, "autonomous-turn": cmd_autonomous_turn,
                    "execute": cmd_execute, "replay": cmd_replay,
                    "tick": cmd_tick, "serve": cmd_serve}
        handler = handlers[args.command]
        action = args.command.replace("-", "_")
    else:
        handlers = {"status": cmd_db_status, "migrate": cmd_db_migrate,
                    "integrity": cmd_db_integrity, "backup": cmd_db_backup}
        handler = handlers[args.db_command]
        action = f"db_{args.db_command}"
    try:
        return handler(args)
    except (StorageError, sqlite3.Error) as e:
        # StorageError：损坏/迁移问题；sqlite3.Error：空文件/未迁移库的表缺失等
        # —— 一律 JSON→stdout + exit 1（不裸 traceback）
        _print({"action": action, "ok": False, "error": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
