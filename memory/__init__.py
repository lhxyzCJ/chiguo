# ============================================================
# memory/ — 记忆后端抽象包（已解耦；mem0 唯一后端）
#
# mem0 为唯一记忆后端（[memory].backend 仅接受 "mem0"/"auto" 遗留值）；
# MemoryBackend 抽象保留作内部测试桩/复用层。
# 详情见 doc/SYSTEM.md「记忆后端抽象」。
# ============================================================

from memory.base import MemoryBackend
from memory.factory import create_backend
from memory.mem0_backend import Mem0Backend

__all__ = ["MemoryBackend", "Mem0Backend", "create_backend"]
