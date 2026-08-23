"""cli.handlers — dispatch 子命令处理器包（#376 表驱动拆分）。

每个 handler 暴露 handle_*(args) -> bool：命中返回 True（已处理，需早返回），
未命中返回 False。全部零行为变更，仅搬运原 cli/dispatch.py run() 分支。
"""
