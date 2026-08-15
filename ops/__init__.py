"""ops — 记账/审计运维子逻辑（拆自 chiguo_daemon.py 的 ops 职责）。

本包承载 daemon 侧的「记账/审计」类独立函数；引擎级记账方法（record_*）归属
decision.ops 的 AccountingMixin。CLI 级运维子命令（--consolidate 等）见 cli.dispatch。
"""
