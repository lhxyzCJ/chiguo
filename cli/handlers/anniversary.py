"""cli.handlers.anniversary — --anniversary 分支（搬自 dispatch.run）。"""
import json
import sys


def handle_anniversary(args) -> bool:
    if not args.anniversary:
        return False
    from cli.commands import _load_light_config
    from schedule.api import ScheduleApi
    cfg = _load_light_config()
    api = ScheduleApi(cfg["_base_dir"], cfg)
    parts = args.anniversary.split()
    cmd = parts[0] if parts else ""
    try:
        if cmd == "add" and len(parts) >= 4:
            result = api.add_anniversary(parts[1], " ".join(parts[3:]), parts[2])
        elif cmd == "remove" and len(parts) >= 2:
            result = api.remove_anniversary(parts[1])
        elif cmd == "list":
            result = api.list_anniversaries()
        elif cmd == "update" and len(parts) >= 3:
            kwargs = {}
            for kv in parts[2:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    kwargs[k] = v
            result = api.update_anniversary(parts[1], **kwargs)
        else:
            result = {"error": f"未知子命令: {args.anniversary}",
                      "usage": "add anniversary <DATE> <NAME> / remove <ID> / list / update <ID> key=val"}
    except ValueError as e:
        result = {"action": "anniversary_added", "ok": False, "error": str(e)} \
            if cmd == "add" else {"action": "anniversary_updated", "ok": False, "error": str(e)}
    print(json.dumps(result, ensure_ascii=False))
    if result.get("error") or result.get("ok") is False:
        sys.exit(1)
    return True
