"""cli.handlers.conversation — --conversation / --conversation-days / --export 分支。"""
import json


def handle_conversation(args) -> bool:
    if not (args.conversation or args.conversation_days or args.export):
        return False
    from decision.engine import DecisionEngine
    from chiguo_monitor import ChiguoMonitor
    engine = DecisionEngine()
    mon = ChiguoMonitor(
        log_path=str(engine._base_dir / "chiguo_decisions.jsonl"),
        state_path=str(engine._base_dir / "chiguo_state.json"),
        break_state_path=str(engine._base_dir / "break_state.json"),
        config_path=str(engine._base_dir / "chiguo_proactive.toml"),
        messages_log_path=str(engine._base_dir / "chiguo_messages.jsonl"),
    )
    if args.export:
        result = mon.export(format=args.export)
        print(result)
    elif args.conversation_days:
        msgs = mon.conversation(days=args.conversation_days)
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
    elif args.conversation:
        msgs = mon.conversation(date_str=args.conversation)
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
    return True
