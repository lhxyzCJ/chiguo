# ============================================================
# memory/ — 记忆后端抽象包（v1.8 解耦）
#
# 任意替换记忆模块：实现 memory/base.py 的 MemoryBackend 四原语
# （available/search/random_memory/stats），toml [memory].backend
# 指向 "module.path.ClassName" 即接入；内置 "auto"/"lancedb"/"json"。
# 详情见 doc/SYSTEM.md「记忆后端抽象」。
# ============================================================

from memory.base import MemoryBackend
from memory.factory import create_backend
from memory.json import JsonMemoryBackend
from memory.lancedb import LanceDbBackend

__all__ = ["MemoryBackend", "LanceDbBackend", "JsonMemoryBackend", "create_backend"]
