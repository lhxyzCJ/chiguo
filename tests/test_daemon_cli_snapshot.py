#!/usr/bin/env python3
"""test_daemon_cli_snapshot.py — daemon CLI 35 参数快照（验收①）。

T10·Q2 daemon 上帝入口拆分：拆包后对外 CLI 行为必须完全不变。
本 runner 对 cli.parser 的 argparse 做参数集合/类型/默认值/nargs/const/choices/
metavar/action/help 全量快照断言，锁定 35 个用户参数契约（help 为 argparse 自动
项，不计入 35）。任一参数增删/改默认/改文案都会在此 fail。

快照值即拆分后（与拆分前逐字一致）的 argparse 实际产物。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.parser import build_parser


# 35 用户参数快照。字段: type名 | default | nargs | const | choices | metavar
EXPECTED = {
    "version":         dict(type=None, default="==SUPPRESS==", nargs=0, const=None, choices=None, metavar=None),
    "loop":            dict(type="int", default=None, nargs="?", const=300, choices=None, metavar="SECONDS"),
    "user_msg":        dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "analysis":        dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "recv_id":         dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "user_msg_file":   dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "analysis_file":   dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "status":          dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "compact":         dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "anniversary":     dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "break_cmd":       dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="CMD"),
    "health":          dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "attention":       dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "schedule_recall": dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="QUERY"),
    "schedule_change": dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="JSON"),
    "memory_search":   dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="QUERY"),
    "tune":            dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "stats":           dict(type="int", default=None, nargs="?", const=7, choices=None, metavar="DAYS"),
    "alerts":          dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "monitor":         dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "consolidate":     dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "conversation":    dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="DATE"),
    "conversation_days": dict(type="int", default=None, nargs=None, const=None, choices=None, metavar="N"),
    "export":          dict(type="str", default=None, nargs="?", const="json", choices=None, metavar="FORMAT"),
    "record_send":     dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="MSG_ID"),
    "fallback":        dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "text":            dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "trigger":         dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "intensity":       dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "send_result":     dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="MSG_ID"),
    "send_status":     dict(type="str", default=None, nargs=None, const=None, choices=["success", "failed"], metavar=None),
    "error":           dict(type="str", default=None, nargs=None, const=None, choices=None, metavar=None),
    "alerts_all":      dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
    "ack":             dict(type="str", default=None, nargs=None, const=None, choices=None, metavar="ALERT_ID"),
    "rotate":          dict(type=None, default=False, nargs=0, const=True, choices=None, metavar=None),
}

# help 文本逐字快照（防拆分改文案）。全量覆盖 35 个参数。
HELP_SNAPSHOT = {
    "version": "show program's version number and exit",
    "loop": "循环评估间隔秒数（最小60）",
    "user_msg": "记录哥哥消息",
    "analysis": "LLM情感分析JSON（配合 --user-msg 使用）",
    "recv_id": "bridge 每条主人消息本地生成的 uuid，用于 recv_dedup 精确去重（同 id 补报升级，不进 agent prompt；无则回退 text_sha+窗口逻辑）",
    "user_msg_file": "消息文本文件（配合 --analysis-file 使用）",
    "analysis_file": "LLM分析JSON文件（配合 --user-msg-file 使用）",
    "status": "显示状态",
    "compact": "紧凑输出（cron用，idle时不输出）",
    "anniversary": "纪念日管理: add anniversary <DATE> <NAME> / remove <ID> / list / update <ID> key=val...",
    "break_cmd": "寒暑假: on|off|status|add <起> <止> <备注>|remove <序号>|list|clear",
    "health": "健康检查：检测 daemon 最近是否正常运行",
    "attention": "注意力快照（T1/T2/T3 + 情感快照，轻量读，零写）",
    "schedule_recall": "安排回忆检索（日期或关键词）",
    "schedule_change": "写安排（JSON: reminder/add/cancel/move/exam_week/remove）",
    "memory_search": "记忆检索（mem0 语义检索，回复侧记忆注入用；mem0 不可用软降级返回空）",
    "tune": "参数校准：基于回复延迟推荐 base_lambda 调整",
    "stats": "统计摘要（默认7天，0=全部历史）",
    "alerts": "异常检测告警",
    "monitor": "完整监控报告（stats + alerts + health）",
    "consolidate": "确定性记忆巩固（去重降权+过期；零 LLM；停机维护专用）",
    "conversation": "显示某天对话记录 (YYYY-MM-DD)",
    "conversation_days": "显示最近N天对话记录",
    "export": "导出对话历史 (默认json)",
    "record_send": "记录已发送消息文本 (配合 --text)",
    "fallback": "A8: 标记该消息为 composer 确定性兜底生成 (配合 --record-send)",
    "text": "消息文本 (配合 --record-send)",
    "trigger": "触发类型 (配合 --record-send)",
    "intensity": "消息强度 (配合 --record-send)",
    "send_result": "回传发送结果 (配合 --send-status success|failed, 可选 --error)",
    "send_status": "发送状态 (配合 --send-result)",
    "error": "失败原因 (配合 --send-result)",
    "alerts_all": "显示所有告警（含已解决）",
    "ack": "确认告警 (配合 --alerts)",
    "rotate": "强制日志轮转",
}


def _actual(action):
    t = getattr(action, "type", None)
    tname = t.__name__ if t else None
    act = getattr(action, "action", None)
    return dict(
        type=tname,
        default=getattr(action, "default", None),
        nargs=getattr(action, "nargs", None),
        const=getattr(action, "const", None),
        choices=list(action.choices) if getattr(action, "choices", None) else None,
        metavar=getattr(action, "metavar", None),
        action=act,
    )


def main():
    failures = []
    parser = build_parser()
    actions = {a.dest: a for a in parser._actions}
    user_dests = [a.dest for a in parser._actions
                  if a.option_strings and a.dest != "help"]  # help=-h 为 argparse 自动项，不计 35

    if len(user_dests) != 35:
        failures.append(f"用户参数应为 35，实际 {len(user_dests)}: {sorted(user_dests)}")
    if set(user_dests) != set(EXPECTED):
        failures.append(f"参数集合不一致: 缺={set(EXPECTED) - set(user_dests)} 增={set(user_dests) - set(EXPECTED)}")

    for dest, spec in EXPECTED.items():
        a = actions.get(dest)
        if a is None:
            failures.append(f"{dest}: 参数缺失")
            continue
        rec = _actual(a)
        for key, exp in spec.items():
            if rec[key] != exp:
                failures.append(f"{dest}.{key}: 应={exp!r}, 实际={rec[key]!r}")
        ah = getattr(a, "help", None)
        if ah != HELP_SNAPSHOT.get(dest):
            failures.append(f"{dest}.help: 应={HELP_SNAPSHOT.get(dest)!r}, 实际={ah!r}")

    # 确保 HELP_SNAPSHOT 恰好覆盖 35 参数（无孤儿文案）
    if set(HELP_SNAPSHOT) != set(EXPECTED):
        failures.append(f"HELP_SNAPSHOT 键与 EXPECTED 不一致: 缺={set(EXPECTED)-set(HELP_SNAPSHOT)} 增={set(HELP_SNAPSHOT)-set(EXPECTED)}")

    # --version 的 action 必须是 'version'（其余参数以 type/const/nargs 区分行为）
    from argparse import _VersionAction
    if not isinstance(actions.get("version"), _VersionAction):
        failures.append("version 参数应为 argparse._VersionAction")

    if failures:
        print(f"FAIL {len(failures)}:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("  OK 35 参数集合/类型/默认/nargs/const/choices/metavar/action/help 快照一致")
    print(f"\n{'='*40}\nALL snapshot tests passed.")


if __name__ == "__main__":
    main()
