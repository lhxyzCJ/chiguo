# ============================================================
# chiguo_time.py — 时区常量共享模块
#
# Q22: 合并全仓库重复的 `CST = timezone(timedelta(hours=8))`
# 定义（16 文件 + chiguo_composer.py:578 的 lowercase cst）。
# 铁律：始终使用 CST（北京时间 UTC+8），绝不使用 naive datetime。
# ============================================================

from datetime import timedelta, timezone

CST = timezone(timedelta(hours=8))
