"""cli.handlers.light — 轻量子命令分支（--attention / --schedule-recall / ...）。"""
from cli.commands import _cmd_attention, _cmd_schedule_recall, _cmd_schedule_change, _cmd_memory_search


def handle_light(args) -> bool:
    if args.attention:
        _cmd_attention()
        return True
    if args.schedule_recall:
        _cmd_schedule_recall(args.schedule_recall)
        return True
    if args.schedule_change:
        _cmd_schedule_change(args.schedule_change)
        return True
    if args.memory_search:
        _cmd_memory_search(args.memory_search)
        return True
    return False
