"""storage.dualwrite — 旧链路事件双写（Phase 3, #477）。

旧链路（daemon / bridge / 命令）照常运行；本模块把关键事实旁路写入 v2
SQLite events（message.received / message.sent / message.delivery_failed /
message.uncertain / wake / schedule.created）。

硬约束：**任何失败静默跳过，绝不阻断旧链**（进程内一次性 stderr 告警）。
激活条件（全部满足才写）：
- env `CHIGUO_EVENT_DUALWRITE`：仅当值为 `"1"`（缺省即 `"1"`）才启用；其他任何值（含 `"0"`）关闭双写；
- DB 路径可解析且文件存在、可打开、schema_version >= 1（未初始化 → 跳过）。

路径解析：env `CHIGUO_DB_PATH` > config `[storage].db_path`
（相对路径锚定 config `_base_dir`）> 默认 `~/.chiguo/chiguo.sqlite`。
"""
import os
import sys
from pathlib import Path

from storage.events import EventStore
from storage.sqlite.db import Database, StorageError

DEFAULT_DB_PATH = "~/.chiguo/chiguo.sqlite"

# 进程内惰性连接缓存：None=未探测；False=已判定不可用（跳过后续直到 reset）
_sink: "EventStore | bool | None" = None
_warned = False


def reset_cache():
    """清空惰性缓存（测试隔离 / 运维重载用）。"""
    global _sink, _warned
    _sink = None
    _warned = False


def _warn_once(msg: str):
    global _warned
    if not _warned:
        print(f"[dualwrite] 事件双写跳过: {msg}", file=sys.stderr)
        _warned = True


def _resolve_path(config) -> Path:
    env = os.environ.get("CHIGUO_DB_PATH")
    if env:
        return Path(env).expanduser()
    raw = DEFAULT_DB_PATH
    if isinstance(config, dict):
        v = (config.get("storage") or {}).get("db_path")
        if isinstance(v, str) and v.strip():
            raw = v.strip()
    p = Path(raw).expanduser()
    if not p.is_absolute() and isinstance(config, dict):
        base = config.get("_base_dir")
        if base:
            p = Path(base) / p
    return p


def _sink_for(config) -> EventStore | None:
    global _sink
    if os.environ.get("CHIGUO_EVENT_DUALWRITE", "1") != "1":
        return None
    if _sink is not None:
        return _sink if isinstance(_sink, EventStore) else None
    try:
        p = _resolve_path(config)
        if not p.exists():
            _warn_once(f"DB 未初始化（{p}），未写入（等 chiguo db migrate）")
            _sink = False
            return None
        db = Database(p)
        if db.schema_version() < 1:
            _warn_once(f"DB schema 未初始化（{p}）")
            _sink = False
            return None
        _sink = EventStore(db)
    except (StorageError, OSError, ValueError) as e:
        _warn_once(f"DB 不可用: {e}")
        _sink = False
    return _sink if isinstance(_sink, EventStore) else None


def _emit(type: str, *, source: str, payload: dict, config=None,
          correlation_id: str | None = None, causation_id: str | None = None):
    """旁路追加一条事件；任何异常静默（旧链优先）。"""
    try:
        sink = _sink_for(config)
        if sink is None:
            return
        sink.append(type, source=source, payload=payload,
                    correlation_id=correlation_id, causation_id=causation_id)
    except Exception as e:  # noqa: BLE001 —— 双写绝不阻断旧链
        _warn_once(f"写入失败: {e}")


# ── 事件类型封装（旧链调用点只依赖这些函数名）────────────────

def message_received(text: str, *, recv_id: str | None = None,
                     analysis: dict | None = None, config=None):
    _emit("message.received", source="wechat",
          payload={"text": text, "recv_id": recv_id, "analysis": analysis},
          config=config)


def message_sent(msg_id: str, text: str, *, trigger: str | None = None,
                 intensity: str | None = None, config=None):
    _emit("message.sent", source="wechat",
          payload={"text": text, "trigger": trigger, "intensity": intensity},
          config=config, correlation_id=msg_id)


def delivery_failed(msg_id: str, error: str = "", *, config=None):
    _emit("message.delivery_failed", source="wechat",
          payload={"error": error}, config=config, correlation_id=msg_id)


def delivery_uncertain(msg_id: str, error: str = "", *, config=None):
    _emit("message.uncertain", source="wechat",
          payload={"error": error}, config=config, correlation_id=msg_id)


def wake(*, action: str, reason: str | None = None,
         msg_id: str | None = None, config=None):
    _emit("wake", source="scheduler",
          payload={"action": action, "reason": reason, "msg_id": msg_id},
          config=config)


def schedule_changed(*, kind: str, item: dict, actor: str = "wechat_command",
                     config=None):
    _emit("schedule.created", source="schedule",
          payload={"kind": kind, "item": item, "actor": actor}, config=config)
