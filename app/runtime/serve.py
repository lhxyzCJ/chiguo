"""app.runtime.serve — 运行时回环 HTTP（Pi extension 服务端，Phase 7, #477）。

仅监听 127.0.0.1（单用户本地）：
- GET  /context?session=<id> → 运行时上下文块（personality/relationship/
  agenda/memories/intent/world），供 Pi extension 在 before_agent_start 注入；
- POST /turn {session, role, text, at} → 记录对话事实（user → message.received；
  assistant → conversation.replied，不扣能量——回复不是主动 send），随即归约；
- GET  /health → 存活探针。

安全性：回环绑定；body ≤64KB；JSON 解析失败/缺字段 → 400；未知路径 404。
"""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from chiguo_time import CST
from storage.events import EventStore
from storage.repositories.commitments import CommitmentRepo
from storage.repositories.memories import MemoryRepo
from storage.repositories.observations import ObservationRepo
from storage.repositories.threads import ThreadRepo
from storage.repositories.turns import TurnRepo
from storage.sqlite.db import Database

MAX_BODY = 64 * 1024
DEFAULT_PORT = 8790


def _observation_line(o) -> str:
    """观测行 → 一行世界摘要（按 payload 常见字段；否则退化为类型名）。"""
    p = o.payload or {}
    if o.type == "weather.changed":
        return f"天气：{p.get('condition', '?')}，{p.get('temperature', '?')}°C"
    if o.type == "schedule.state":
        if p.get("on_break"):
            return "课表：假期中"
        if p.get("in_class"):
            return f"课表：正在上{p.get('current_course') or '课'}"
        return f"课表：{p.get('class_load', '?')}（今天）"
    if o.type == "music.observed":
        plays = p.get("plays") or []
        if plays:
            return f"音乐：最近在听 {plays[0].get('name', '?')}"
    if o.type in ("holiday.upcoming", "anniversary.upcoming"):
        return f"{'节假日' if o.type.startswith('holiday') else '纪念日'}：{p.get('name', '?')}（{p.get('days_until', '?')} 天后）"
    return o.type


def build_context(db: Database, config: dict, now: datetime | None = None) -> dict:
    """组装 /context 返回（字段名与 Pi extension CONTEXT_SECTIONS 对齐）。"""
    from app.runtime.reducer import Reducer
    now = now or datetime.now(CST)
    state = Reducer(db, config).current()   # 只读检查点
    affect, rel = state.affect, state.relationship

    personality = (f"当前人格层：{affect.dominant_layer}；"
                   f"孤独{round(affect.loneliness)}/不安{round(affect.anxiety)}/"
                   f"好感{round(affect.affection)}/元气{round(affect.energy)}")
    balance = rel.initiative_balance
    who_initiates = ("多由哥哥先开口" if balance < -0.2 else
                     "多由迟菓先开口" if balance > 0.2 else "双方大致均衡")
    relationship = [f"亲近度 {round(rel.closeness, 2)}；{who_initiates}",
                    f"近期温暖 {round(rel.recent_warmth, 2)}／紧张 {round(rel.recent_tension, 2)}"]

    agenda = []
    for c in CommitmentRepo(db).list_open()[:8]:
        due = f"（截止 {c.due_at.strftime('%m-%d %H:%M')}）" if c.due_at else ""
        agenda.append(f"承诺：{c.subject}{due}")
    for t in ThreadRepo(db).list_open()[:8]:
        agenda.append(f"话题：{t.subject}")

    memories = [m.text[:60] for m in MemoryRepo(db).list_active()[:5]]

    intent_line = None
    turns = TurnRepo(db).recent(limit=1)
    if turns and turns[0].outcome in ("intent", "action_pending") and turns[0].intent_id:
        from storage.repositories.drives import IntentRepo
        intent = IntentRepo(db).get(turns[0].intent_id)
        if intent is not None:
            intent_line = f"当前意图：{intent.type}（{json.dumps(intent.why, ensure_ascii=False)[:120]}）"

    world = [_observation_line(o) for o in ObservationRepo(db).active(now)[:6]]
    return {"personality": personality, "relationship": relationship,
            "agenda": agenda, "memories": memories, "intent": intent_line,
            "world": world}


def record_turn(db: Database, config: dict, payload: dict) -> EventStore | None:
    """记录一跳对话（user → message.received；assistant → conversation.replied）。"""
    role = payload.get("role")
    text = str(payload.get("text") or "").strip()
    if role not in ("user", "assistant") or not text:
        return None
    at = payload.get("at")
    if isinstance(at, (int, float)):
        occurred = datetime.fromtimestamp(at / 1000.0, tz=CST)
    else:
        occurred = datetime.now(CST)
    store = EventStore(db)
    ev_type = "message.received" if role == "user" else "conversation.replied"
    source = "wechat" if role == "user" else "pi"
    store.append(ev_type, source=source, occurred_at=occurred,
                 payload={"text": text, "session": payload.get("session")})
    # 立即归约（/context 立刻可见；assistant 回复类型不进 affect）
    from app.runtime.reducer import Reducer
    Reducer(db, config).catch_up(occurred)
    return store


class RuntimeServer:
    """回环 HTTP 服务器（start/stop；port=0 时由系统分配）。"""

    def __init__(self, db: Database, config: dict, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT):
        self.db = db
        self.config = config or {}
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静默 access log（stderr 由调用方掌控）
                pass

            def _json(self, status: int, obj: dict):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _request_db(self) -> Database:
                """每请求独立连接（sqlite3 连接不可跨线程；WAL 支持多连接并存）。"""
                return Database(outer.db.path)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    return self._json(200, {"ok": True})
                if parsed.path == "/context":
                    db = self._request_db()
                    try:
                        ctx = build_context(db, outer.config)
                    except Exception as e:  # noqa: BLE001
                        return self._json(500, {"ok": False, "error": str(e)[:200]})
                    finally:
                        db.close()
                    return self._json(200, ctx)
                return self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if urlparse(self.path).path != "/turn":
                    return self._json(404, {"ok": False, "error": "not found"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_BODY:
                    return self._json(400, {"ok": False, "error": "invalid body size"})
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("not an object")
                except (ValueError, UnicodeDecodeError):
                    return self._json(400, {"ok": False, "error": "bad json"})
                db = self._request_db()
                try:
                    ok = record_turn(db, outer.config, payload) is not None
                except Exception as e:  # noqa: BLE001
                    return self._json(500, {"ok": False, "error": str(e)[:200]})
                finally:
                    db.close()
                if not ok:
                    return self._json(400, {"ok": False,
                                            "error": "role/text required"})
                return self._json(200, {"ok": True})

        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="chiguo-runtime-http")
        self._thread.start()

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
