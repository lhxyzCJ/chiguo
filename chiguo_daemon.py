#!/usr/bin/env python3
# ============================================================
# chiguo_daemon.py — 迟菓主动消息 决策引擎（拆包后薄入口 facade）
#
# 本文件不再承载决策/记账/loop 逻辑，仅作为对外 CLI 入口与兼容 facade：
#   - main(): 参数解析 + 子命令分发（实现在 cli/）
#   - DecisionEngine: 决策引擎（实现在 decision/engine.py，由 base/core/context/
#     ops.engine_ops/runner.loop 五个 mixin 组合）
#   - 对外 CLI 行为（35 参数/子命令/JSON 输出/exit code）与拆分前逐字一致。
#
# 职责拆包（T10·Q2 daemon 上帝入口拆分，#268）：
#   cli/      参数解析 + 子命令分发（parser.py / commands.py / dispatch.py）
#   runner/   loop/cron 形态（loop.py：LoopSenderMixin + run_loop）
#   decision/ 决策逻辑（base.py 基础设施 / core.py 核心决策 / context.py 上下文构建）
#   ops/      记账/审计（engine_ops.py AccountingMixin）
#
# 用法：
#   python3 chiguo_daemon.py              # 检查并输出决策 JSON
#   python3 chiguo_daemon.py --status     # 查看状态
#   python3 chiguo_daemon.py --user-msg "…"  # 记录哥哥消息
#   python3 chiguo_daemon.py --loop 120   # 持续运行（send 分支内聚发送侧）
# ============================================================

from cli.dispatch import main, run, parse_args
from decision.engine import DecisionEngine
from cli.commands import _cmd_memory_search  # 仅测试经 chiguo_daemon 入口消费，故保留
from chiguo_version import VERSION

__all__ = ["DecisionEngine", "main", "run", "parse_args", "_cmd_memory_search", "VERSION"]


if __name__ == "__main__":
    main()
