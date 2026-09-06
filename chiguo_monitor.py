#!/usr/bin/env python3
# ============================================================
# chiguo_monitor.py — 迟菓主动消息 结构化监控（兼容 shim，#378）
#
# 实现已按视图拆入 monitor/ 包（纯搬运，零行为变化）：
#   monitor/base.py    ChiguoMonitor 壳（Stats/Alerts/Health Mixin 组装）
#                      + I/O 与解析 helper + report/conversation/export + main
#   monitor/stats.py   stats 视图
#   monitor/alerts.py  alerts 视图 + AlertManager + collect_new_alerts_to_push
#   monitor/health.py  health 视图
#   monitor/helpers.py 视图间共享小件（_num/mem0 守卫）
#
# 本文件仅重导出对外符号 + 保留 __main__ 入口，对外 import 路径零变化。
# 独立可用：python3 chiguo_monitor.py [--days 7] [--alerts]
# daemon 集成：python3 chiguo_daemon.py --stats|--alerts|--monitor
# ============================================================

import shutil  # noqa: F401 — 保留 cm.shutil 补丁面（test_health_disk_check_anchored_to_project）

from chiguo_version import VERSION  # noqa: F401 — 版本单源重导出；report() 以 {"app_version": VERSION} 上报（实现见 monitor/base.py）
from monitor.alerts import AlertManager, collect_new_alerts_to_push
from monitor.base import ChiguoMonitor, main

__all__ = ["ChiguoMonitor", "AlertManager", "collect_new_alerts_to_push", "main"]


if __name__ == "__main__":
    import sys

    sys.exit(main())
