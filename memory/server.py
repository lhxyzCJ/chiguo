"""memory.server — 常驻只读记忆边车（stdio NDJSON，长命进程）。

动机：回复链每条消息 spawn 全新 python 进程跑 --memory-search，mem0 懒加载 +
qdrant 嵌入式开库冷启动 4~13s。边车进程常驻抱住 warm backend，同进程重复
检索 ~0.5s（Issue #450，2C）。

协议：stdin 一行一请求 {"id", "cmd", ...}；stdout 一行一响应并 flush。
  cmd=memory_search {query, limit?} → {"id","ok":true,"action":"memory_search","query","count","memories"}
  cmd=attention {}                 → {"id","ok":true,"action":"attention",...}（形状同 --attention）
  cmd=ping {}                      → {"id","ok":true,"action":"ping"}
异常永不崩进程：{"id","ok":false,"reason"}；空行忽略；stdin EOF → exit 0。
诊断进 stderr，stdout 保持纯 JSON 行（bridge 按行解析）。

只读语义：与 --attention/--memory-search 同源逻辑；search 默认 reinforce 关闭
→ note_recalled 内存短路零写（见 memory/base.py）。写路径（--user-msg/
--analysis 升级）不进边车，仍走 spawn。
用法：python memory/server.py [--config PATH]（CWD 需为项目根；bridge 经
memory-rpc.mjs 拉起，随 bridge 退出 stdin EOF 而死）。
"""
import json as _json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from chiguo_time import CST  # noqa: E402
from cli.commands import _load_light_config  # noqa: E402
from memory import create_backend  # noqa: E402


def _attention_payload(cfg: dict) -> dict:
    """与 _cmd_attention 同源组装（不 print、不 exit，供边车复用）。"""
    from schedule.sources import load_sources
    from schedule.attention import build_attention
    src = load_sources(cfg["_base_dir"], cfg)
    att = build_attention(src, datetime.now(CST).date())
    emotion = {}
    try:
        st = _json.loads((Path(cfg["_base_dir"]) / "chiguo_state.json").read_text())
        emotion = st.get("emotion", {})
    except (ValueError, TypeError, OSError):
        pass
    return {"ok": True, "action": "attention", "attention": att,
            "emotion": emotion, "week_num": att["week_num"],
            "today_exceptions": att["today_exceptions"]}


def _search_payload(backend, query: str, limit: int) -> dict:
    rows = backend.search_with_forgetting(query, limit=limit)
    return {"ok": True, "action": "memory_search", "query": query,
            "count": len(rows), "memories": rows}


def serve(config_path: str | None = None) -> int:
    cfg = _load_light_config(config_path)
    try:
        backend = create_backend(cfg.get("memory", {}), base_dir=cfg["_base_dir"])
    except Exception as e:
        print(f"[memory-server] backend 初始化失败: {e}", file=sys.stderr)
        return 1
    print("[memory-server] ready", file=sys.stderr)
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = _json.loads(line)
        except ValueError:
            continue  # 脏行忽略（行协议自愈，不破坏后续请求）
        if not isinstance(req, dict):
            continue  # 合法 JSON 但非对象（123/"x"/[]/null）→ 忽略，无 id 可回
        rid = req.get("id")
        try:
            cmd = req.get("cmd")
            if cmd == "ping":
                resp = {"ok": True, "action": "ping"}
            elif cmd == "attention":
                resp = _attention_payload(cfg)
            elif cmd == "memory_search":
                query = req.get("query")
                if not isinstance(query, str) or not query.strip():
                    resp = {"ok": False, "reason": "query 非空字符串必填"}
                else:
                    limit = req.get("limit", 5)
                    limit = limit if isinstance(limit, int) and 1 <= limit <= 20 else 5
                    resp = _search_payload(backend, query, limit)
            else:
                resp = {"ok": False, "reason": f"未知 cmd: {str(cmd)[:200]}"}
        except Exception as e:
            resp = {"ok": False, "reason": str(e)[:200]}
        resp["id"] = rid
        try:
            out.write(_json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            out.flush()
        except BrokenPipeError:
            break
    return 0


def main(argv=None) -> int:
    config_path = None
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--config" and len(args) > 1:
        config_path = args[1]
    return serve(config_path)


if __name__ == "__main__":
    sys.exit(main())
