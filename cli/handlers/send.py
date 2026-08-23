"""cli.handlers.send — --record-send / --send-result 分支。"""
import json
import sys


def handle_record_send(args) -> bool:
    if not args.record_send:
        return False
    from decision.engine import DecisionEngine
    if not args.text:
        print(json.dumps({"error": "--text required with --record-send"}, ensure_ascii=False))
        sys.exit(1)
    engine = DecisionEngine()
    engine.record_send_text(args.record_send, args.text, args.trigger, args.intensity,
                            fallback=args.fallback)
    print(json.dumps({"action": "send_text_recorded", "msg_id": args.record_send, "ok": True}, ensure_ascii=False))
    return True


def handle_send_result(args) -> bool:
    if not args.send_result:
        return False
    from decision.engine import DecisionEngine
    if not args.send_status:
        print(json.dumps({"error": "--send-status required with --send-result"}, ensure_ascii=False))
        sys.exit(1)
    engine = DecisionEngine()
    r = engine.record_send_result(args.send_result, args.send_status, args.error)
    print(json.dumps(r, ensure_ascii=False))
    return True
