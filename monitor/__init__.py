#!/usr/bin/env python3
"""monitor 包 — chiguo_monitor.py 按视图拆分（#378，纯搬运零行为变化）。

- base.py: ChiguoMonitor 主类（StatsMixin/AlertsMixin/HealthMixin 组装）
  + 全部 I/O 与解析 helper + report/conversation/export + main CLI
- stats.py: stats 视图（StatsMixin）
- alerts.py: alerts 视图 + AlertManager + collect_new_alerts_to_push（AlertsMixin）
- health.py: health 视图（HealthMixin）

对外 import 路径经 chiguo_monitor.py 兼容 shim 保持不变。
"""

from .alerts import AlertManager, collect_new_alerts_to_push
from .base import ChiguoMonitor, main

__all__ = ["ChiguoMonitor", "AlertManager", "collect_new_alerts_to_push", "main"]
