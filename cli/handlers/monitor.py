"""cli.handlers.monitor — --stats / --alerts / --monitor / --alerts-push 分支。"""
import json


def handle_monitor(args) -> bool:
    if not (args.stats is not None or args.alerts or args.monitor or args.alerts_push):
        return False
    from decision.engine import DecisionEngine
    from chiguo_monitor import ChiguoMonitor, AlertManager
    engine = DecisionEngine()
    mon = ChiguoMonitor(
        log_path=str(engine._base_dir / "chiguo_decisions.jsonl"),
        state_path=str(engine._base_dir / "chiguo_state.json"),
        break_state_path=str(engine._base_dir / "break_state.json"),
        config_path=str(engine._base_dir / "chiguo_proactive.toml"),
        messages_log_path=str(engine._base_dir / "chiguo_messages.jsonl"),
        alerts_path=str(engine._base_dir / "chiguo_alerts.json"),
        events_path=str(engine._base_dir / "chiguo_events.jsonl"),
    )
    if args.alerts_push:
        from chiguo_monitor import collect_new_alerts_to_push
        from ops.bridge_ops import push_alerts_via_wechat
        am = AlertManager(state_path=str(engine._base_dir / "chiguo_alerts.json"))
        new_alerts = collect_new_alerts_to_push(mon, am)
        pushed = push_alerts_via_wechat(engine, new_alerts)
        print(json.dumps({"action": "alerts_push", "pushed": len(pushed),
                          "alerts": pushed}, ensure_ascii=False, indent=2))
        return True
    if args.alerts:
        am = AlertManager(state_path=str(engine._base_dir / "chiguo_alerts.json"))
        if args.ack:
            ok = am.acknowledge(args.ack)
            print(json.dumps({"action": "ack", "alert_id": args.ack, "ok": ok}, ensure_ascii=False))
            return True
        fresh = mon.alerts()
        am.ingest(fresh)
        if args.alerts_all:
            result = am.list_all()
        else:
            result = am.list_active()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.monitor:
        report = mon.report(days=args.stats if args.stats is not None else 7)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        stats = mon.stats(days=args.stats)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return True
