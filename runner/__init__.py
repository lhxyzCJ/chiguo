"""runner — 发送形态包（拆自 chiguo_daemon.py）。

  - runner.loop   loop 发送侧内聚（LoopSenderMixin）+ --loop 常驻循环编排（run_loop）
cron 形态（chiguo-tick.sh 每 15 分钟单次 spawn）的决策评估由 cli/run() 默认分支完成。
"""
