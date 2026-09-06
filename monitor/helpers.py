#!/usr/bin/env python3
"""monitor.helpers — 视图间共享的模块级小件（#378 纯搬运）。

_num：数值守卫（alerts B2/B3 用）；mem0 import 守卫（health 用）。
base 不需要 import 本模块（ChiguoMonitor 方法体内不直接用 _num/_HAS_MEM0）。
"""

import os

# mem0 遥测必须在 import mem0 前关闭（mem0/memory/telemetry.py 在其 import 时
# 读 MEM0_TELEMETRY；晚设静默无效）。setdefault 尊重运维显式 opt-in。
os.environ.setdefault("MEM0_TELEMETRY", "false")

try:
    import mem0  # noqa: F401
    _HAS_MEM0 = True
except ImportError:
    _HAS_MEM0 = False


def _num(v):
    """数值守卫：非 int/float（损坏行 dict/list/str）→ 0，防比较 TypeError 崩溃。"""
    return v if isinstance(v, (int, float)) else 0
