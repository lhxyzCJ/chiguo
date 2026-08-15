"""cli — 迟菓 daemon CLI 包（拆自 chiguo_daemon.py main()）。

  - cli.parser     36 参数 argparse 定义（对外 CLI 契约不变）
  - cli.commands   轻量子命令（--attention/--schedule-recall/--schedule-change/--memory-search + _load_light_config）
  - cli.dispatch   入口分发（main() / run(args)）
"""
