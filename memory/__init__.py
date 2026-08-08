# ============================================================
# memory/ — 记忆后端抽象包（v1.8 解耦；v1.9 默认后端 = mem0）
#
# 任意替换记忆模块：实现 memory/base.py 的 MemoryBackend 四原语
# （available/search/random_memory/stats），toml [memory].backend
# 指向 "module.path.ClassName" 即接入；内置 "mem0"。
# 详情见 doc/SYSTEM.md「记忆后端抽象」。
# ============================================================

from memory.base import MemoryBackend
from memory.factory import create_backend
from memory.mem0_backend import Mem0Backend

__all__ = ["MemoryBackend", "Mem0Backend", "create_backend"]
