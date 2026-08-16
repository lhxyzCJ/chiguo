"""cli.parser — 迟菓 daemon CLI 参数定义（36 个参数，拆自 chiguo_daemon.py main()）。

对外 CLI 契约（参数集合/默认值/帮助文本/exit code 语义）必须与拆分前逐字一致。
参数快照测试据此断言 argparse 前后一致。
"""
import argparse

from chiguo_version import VERSION


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（36 个参数）。"""
    parser = argparse.ArgumentParser(description="迟菓主动消息 决策引擎")
    # L2 (#234): --version 帮助不写死具体版本链，只写规则（防过期）；实际次版本见 chiguo_version.py
    parser.add_argument("--version", action="version", version=f"chiguo v{VERSION} (规则: 每次迭代次版本 MINOR+1，见 chiguo_version.py)")
    parser.add_argument("--loop", type=int, nargs="?", const=300, metavar="SECONDS",
                        help="循环评估间隔秒数（最小60）")
    parser.add_argument("--user-msg", type=str, default=None,
                        help="记录哥哥消息")
    parser.add_argument("--analysis", type=str, default=None,
                        help="LLM情感分析JSON（配合 --user-msg 使用）")
    parser.add_argument("--recv-id", type=str, default=None,
                        help="bridge 每条主人消息本地生成的 uuid，用于 recv_dedup 精确去重（同 id 补报升级，不进 agent prompt；无则回退 text_sha+窗口逻辑）")
    # ── v6: 文件传参（避免 shell 转义问题，SKILL.md 已采用此路径）──
    parser.add_argument("--user-msg-file", type=str, default=None,
                        help="消息文本文件（配合 --analysis-file 使用）")
    parser.add_argument("--analysis-file", type=str, default=None,
                        help="LLM分析JSON文件（配合 --user-msg-file 使用）")
    parser.add_argument("--status", action="store_true",
                        help="显示状态")
    parser.add_argument("--compact", action="store_true",
                        help="紧凑输出（cron用，idle时不输出）")
    parser.add_argument("--anniversary", type=str, default=None,
                        help="纪念日管理: add anniversary <DATE> <NAME> / remove <ID> / list / update <ID> key=val...")
    parser.add_argument("--break", type=str, default=None, dest="break_cmd",
                        metavar="CMD",
                        help="寒暑假: on|off|status|add <起> <止> <备注>|remove <序号>|list|clear")
    parser.add_argument("--health", action="store_true",
                        help="健康检查：检测 daemon 最近是否正常运行")
    parser.add_argument("--attention", action="store_true",
                        help="注意力快照（T1/T2/T3 + 情感快照，轻量读，零写）")
    parser.add_argument("--schedule-recall", type=str, default=None,
                        metavar="QUERY",
                        help="安排回忆检索（日期或关键词）")
    parser.add_argument("--schedule-change", type=str, default=None,
                        metavar="JSON",
                        help="写安排（JSON: reminder/add/cancel/move/exam_week/remove）")
    parser.add_argument("--memory-search", type=str, default=None,
                        metavar="QUERY",
                        help="记忆检索（mem0 语义检索，回复侧记忆注入用；mem0 不可用软降级返回空）")
    parser.add_argument("--tune", action="store_true",
                        help="参数校准：基于回复延迟推荐 base_lambda 调整")
    parser.add_argument("--stats", type=int, nargs="?", const=7, metavar="DAYS",
                        help="统计摘要（默认7天，0=全部历史）")
    parser.add_argument("--alerts", action="store_true",
                        help="异常检测告警")
    parser.add_argument("--monitor", action="store_true",
                        help="完整监控报告（stats + alerts + health）")
    # ── C1: 确定性记忆巩固 ──
    parser.add_argument("--consolidate", action="store_true",
                        help="确定性记忆巩固（去重降权+过期；零 LLM；停机维护专用）")
    # ── v5: 对话日志 & 归档 ──
    parser.add_argument("--conversation", type=str, default=None,
                        metavar="DATE",
                        help="显示某天对话记录 (YYYY-MM-DD)")
    parser.add_argument("--conversation-days", type=int, default=None,
                        metavar="N",
                        help="显示最近N天对话记录")
    parser.add_argument("--export", type=str, nargs="?", const="json",
                        metavar="FORMAT",
                        help="导出对话历史 (默认json)")
    parser.add_argument("--record-send", type=str, default=None,
                        metavar="MSG_ID",
                        help="记录已发送消息文本 (配合 --text)")
    parser.add_argument("--fallback", action="store_true",
                        help="A8: 标记该消息为 composer 确定性兜底生成 (配合 --record-send)")
    parser.add_argument("--text", type=str, default=None,
                        help="消息文本 (配合 --record-send)")
    parser.add_argument("--trigger", type=str, default=None,
                        help="触发类型 (配合 --record-send)")
    parser.add_argument("--intensity", type=str, default=None,
                        help="消息强度 (配合 --record-send)")
    # ── v6: 反馈闭环 ──
    parser.add_argument("--send-result", type=str, default=None, metavar="MSG_ID",
                        help="回传发送结果 (配合 --send-status success|failed|uncertain, 可选 --error)")
    parser.add_argument("--send-status", type=str, default=None,
                        choices=["success", "failed", "uncertain"],
                        help="发送状态 (配合 --send-result)")
    parser.add_argument("--error", type=str, default=None, help="失败原因 (配合 --send-result)")
    # ── v5: 告警持久化 ──
    parser.add_argument("--alerts-all", action="store_true",
                        help="显示所有告警（含已解决）")
    parser.add_argument("--ack", type=str, default=None,
                        metavar="ALERT_ID",
                        help="确认告警 (配合 --alerts)")
    parser.add_argument("--alerts-push", action="store_true",
                        help="Q24: 把新告警经微信 bridge /send 推送（scripts/alert-cron.sh 入口）")
    # ── v5: 日志轮转 ──
    parser.add_argument("--rotate", action="store_true",
                        help="强制日志轮转")
    return parser
