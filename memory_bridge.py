# ============================================================
# memory_bridge.py — 记忆桥接兼容门面（v1.8 已解耦到 memory/ 包）
#
# 原实现迁移至 memory/ 包：LanceDbBackend（memory/lancedb.py）、
# JsonMemoryBackend（memory/json.py）、工厂 create_backend（memory/factory.py）。
# 本文件保留 MemoryBridge 名称与 CLI 供向后兼容；
# 新代码请直接使用 memory.create_backend()（见 chiguo_state.py）。
# ============================================================

import json
import sys

from memory import LanceDbBackend, create_backend
from memory.base import (
    DEFAULT_EBBINGHAUS_MIN_WEIGHT,
    DEFAULT_EBBINGHAUS_STRENGTH,
    USER_KEYWORDS,
)

# 兼容别名：MemoryBridge 即 LanceDB 后端（历史行为：LanceDB 只读记忆库）
MemoryBridge = LanceDbBackend

__all__ = ["MemoryBridge", "LanceDbBackend", "create_backend",
           "DEFAULT_EBBINGHAUS_STRENGTH", "DEFAULT_EBBINGHAUS_MIN_WEIGHT", "USER_KEYWORDS"]


# ── CLI（沿用历史命令；经工厂创建，尊重 toml [memory].backend）──

def _default_bridge():
    """按仓库 toml [memory] 段创建后端（--loop/手工检查用）。"""
    import tomllib
    from pathlib import Path

    repo = Path(__file__).resolve().parent
    try:
        with open(repo / "chiguo_proactive.toml", "rb") as f:
            cfg = tomllib.load(f)
    except Exception:
        cfg = {}
    return create_backend(cfg.get("memory", {}), base_dir=repo)


if __name__ == "__main__":
    bridge = _default_bridge()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--search":
            query = sys.argv[2] if len(sys.argv) > 2 else "迟菓"
            results = bridge.search(query)
            for m in results:
                print(f"[{m['category']}] {m['l0_abstract'] or m['text'][:80]}")
        elif cmd == "--random":
            m = bridge.random_memory()
            if m:
                print(json.dumps(m, indent=2, ensure_ascii=False, default=str))
            else:
                print("无相关记忆")
        elif cmd == "--stats":
            print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
        else:
            print(f"用法: {sys.argv[0]} [--search|--random|--stats]")
    else:
        print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
