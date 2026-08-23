"""cli.handlers.rotation — --rotate 分支。"""
import json


def handle_rotation(args) -> bool:
    if not args.rotate:
        return False
    from decision.engine import DecisionEngine
    from chiguo_rotation import force_rotate
    engine = DecisionEngine()
    force_rotate(
        [str(engine._base_dir / "chiguo_decisions.jsonl"),
         str(engine._base_dir / "chiguo_messages.jsonl")],
        archive_dir=str(engine._base_dir / "archive"),
    )
    print(json.dumps({"action": "rotate", "ok": True}, ensure_ascii=False))
    return True
