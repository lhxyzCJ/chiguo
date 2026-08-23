"""cli.handlers.session — --status / --user-msg / --loop / 默认评估。"""
import json
import sys
from datetime import datetime
from chiguo_time import CST
from pathlib import Path



def handle_status(args, engine) -> bool:
    if not args.status:
        return False
    snap = engine.snapshot()
    print(json.dumps({
        "character": "迟菓",
        "time": snap["time"],
        "dominant_layer": snap["dominant_layer"],
        "emotion": snap["emotion"],
        "cooldown": snap["cooldown"],
    }, ensure_ascii=False, indent=2))
    return True


def handle_user_msg(args, engine) -> bool:
    if args.user_msg_file:
        try:
            args.user_msg = Path(args.user_msg_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as e:
            print(json.dumps({"error": f"读取消息文件失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
    if args.analysis_file:
        try:
            args.analysis = Path(args.analysis_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as e:
            print(json.dumps({"error": f"读取分析文件失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
    if not args.user_msg:
        return False
    engine.record_user_message(args.user_msg, args.analysis, getattr(args, "recv_id", None))
    decision = engine.evaluate()
    if decision["action"] == "send":
        engine.record_send_result(decision.get("msg_id", ""), "failed",
                                  error="phantom_send_reply_path")
    if args.compact and decision["action"] == "idle":
        compact = {"action": "idle", "time": datetime.now(CST).isoformat()}
        if "next_evaluation_at" in decision:
            compact["next_evaluation_at"] = decision["next_evaluation_at"]
        print(json.dumps(compact, ensure_ascii=False))
    else:
        print(json.dumps(decision, ensure_ascii=False, indent=2))
    return True


def handle_loop(args, engine) -> bool:
    if not args.loop:
        return False
    from cli.dispatch import startup_conflict
    from runner.loop import run_loop
    rc = startup_conflict(str(engine._base_dir), "loop")
    if rc == 1:
        sys.exit(rc)
    run_loop(engine, args.loop, args.compact)
    return True
