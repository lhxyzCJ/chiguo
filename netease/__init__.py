# ============================================================
# netease/__init__.py — 网易云集成包
# 数据面 NeteaseBridge(HTTP 桥接,实例化) + 策略层 NeteaseService(DI)。
# 运行时文件统一锚定 <base_dir>/netease/。零新依赖。
# ============================================================

from netease.bridge import NeteaseBridge
from netease.service import NeteaseService

__all__ = ["NeteaseBridge", "NeteaseService"]
