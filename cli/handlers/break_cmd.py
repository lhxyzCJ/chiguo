"""cli.handlers.break_cmd — --break 分支。"""
import json
import sys
from cli.commands import _load_light_config


def handle_break(args) -> bool:
    if not args.break_cmd:
        return False
    from schedule.api import ScheduleApi
    cfg = _load_light_config()
    api = ScheduleApi(cfg["_base_dir"], cfg)
    result = api.set_break(args.break_cmd)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("error") or result.get("ok") is False:
        sys.exit(1)
    return True
