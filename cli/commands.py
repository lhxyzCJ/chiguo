"""cli.commands — daemon 轻量子命令（拆自 chiguo_daemon.py）。

含 _load_light_config 与四个轻量读/写子命令：
  --attention / --schedule-recall / --schedule-change / --memory-search
这些分支不构造 DecisionEngine/ChiguoState，毫秒级执行；JSON→stdout、诊断→stderr，
失败 exit 1（bridge 据此降级普通回复）。schedule/memory 依赖均函数体内惰性 import，
不污染 cli 包顶层导入路径。
"""
import json as _json
import sys
import tomllib
from datetime import datetime
from chiguo_time import CST
from pathlib import Path

from chiguo_paths import PROJECT_ROOT



def _load_light_config(config_path: str | None = None) -> dict:
    """轻量分支共用:读 toml + 注入 _base_dir(config 所在目录)。不构造任何引擎对象。"""
    if config_path is None:
        config_path = str(PROJECT_ROOT / "chiguo_proactive.toml")
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(Path(config_path).resolve().parent)
    return cfg


def _cmd_attention(config_path: str | None = None):
    """--attention 轻量读(§5.4):T1/T2/T3 组装 + 情感快照。零写副作用,毫秒级。"""
    from schedule.sources import load_sources
    from schedule.attention import build_attention
    cfg = _load_light_config(config_path)
    try:
        src = load_sources(cfg["_base_dir"], cfg)
        att = build_attention(src, datetime.now(CST).date())
        emotion = {}
        try:
            st = _json.loads((Path(cfg["_base_dir"]) / "chiguo_state.json").read_text())
            emotion = st.get("emotion", {})
        except (ValueError, TypeError, OSError):
            pass
        print(_json.dumps({"action": "attention", "ok": True, "attention": att,
                           "emotion": emotion, "week_num": att["week_num"],
                           "today_exceptions": att["today_exceptions"]}, ensure_ascii=False))
    except Exception as e:
        print(_json.dumps({"action": "attention", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --attention 失败: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_schedule_recall(query: str, config_path: str | None = None):
    """--schedule-recall <query>:recall 检索(A4 形状;失败 ok:false + exit 1,bridge 降级普通回复)。"""
    from schedule.sources import load_sources
    from schedule.recall import recall
    cfg = _load_light_config(config_path)
    try:
        r = recall(query, load_sources(cfg["_base_dir"], cfg), datetime.now(CST).date())
    except Exception as e:
        print(_json.dumps({"action": "schedule_recall", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-recall 失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(_json.dumps({"action": "schedule_recall", "ok": True, "query": r["query"],
                       "matches": r["matches"]}, ensure_ascii=False))


def _cmd_schedule_change(json_arg: str, config_path: str | None = None):
    """--schedule-change <json>:写安排(二十轮 A4 形状;畸形 JSON → bad_json 不写入;ApiRejection → H5 文案)。"""
    from schedule.api import ScheduleApi, ApiRejection
    from schedule.confirm import build_question
    cfg = _load_light_config(config_path)
    try:
        item = _json.loads(json_arg)
    except (_json.JSONDecodeError, TypeError):
        print(_json.dumps({"action": "schedule_change", "ok": False,
                           "reason": "bad_json", "question": "处理失败,再试一次?"}, ensure_ascii=False))
        print("[chiguo_daemon] --schedule-change 畸形 JSON,未写入", file=sys.stderr)
        sys.exit(1)
    try:
        api = ScheduleApi(cfg["_base_dir"], cfg)
        if isinstance(item, dict) and item.get("kind") == "remove":
            result = api.remove_override(item.get("match", {}))
        else:
            result = api.apply_override(item)
    except ApiRejection as e:
        question, missing = build_question(e.category)
        out = {"action": "schedule_change", "ok": False, "reason": e.category, "question": question}
        if missing:
            out["missing"] = missing
        print(_json.dumps(out, ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-change 拒绝({e.category}): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(_json.dumps({"action": "schedule_change", "ok": False,
                           "reason": "internal_error", "question": "处理失败,再试一次?"},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-change 异常: {e}", file=sys.stderr)
        sys.exit(1)
    print(_json.dumps({"action": "schedule_change", "ok": True, "text": result["text"]},
                      ensure_ascii=False))


def _cmd_memory_search(query: str, config_path: str | None = None):
    """--memory-search <query>: 回复侧记忆检索(mem0,软降级)。JSON→stdout,诊断→stderr,失败 exit 1。"""
    from memory import create_backend
    cfg = _load_light_config(config_path)
    try:
        bridge = create_backend(cfg.get("memory", {}), base_dir=cfg["_base_dir"])
        rows = bridge.search_with_forgetting(query, limit=5)
    except Exception as e:
        print(_json.dumps({"action": "memory_search", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --memory-search 失败: {e}", file=sys.stderr)
        sys.exit(1)
    # 行契约(id/text/category/scope/importance/timestamp/datetime…)已为 JSON 可序列化；
    # default=str 兜底 datetime 等非标准类型，防单条脏形状拖垮整个检索输出
    print(_json.dumps({"action": "memory_search", "ok": True, "query": query,
                       "count": len(rows), "memories": rows},
                      ensure_ascii=False, default=str))
