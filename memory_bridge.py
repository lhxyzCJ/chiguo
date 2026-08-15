# ============================================================
# memory_bridge.py — 记忆桥接门面（已解耦到 memory/ 包；mem0 为唯一后端）
#
# 实现迁移至 memory/ 包：Mem0Backend（memory/mem0_backend.py）、
# 工厂 create_backend（memory/factory.py）。
# 本文件保留 MemoryBridge 名称与 CLI（经工厂创建，尊重 toml [memory].backend）。
# 新代码请直接使用 memory.create_backend()（见 chiguo_state.py）。
# ============================================================

import json
import sys

from memory import Mem0Backend, create_backend
from memory.base import (
    DEFAULT_EBBINGHAUS_MIN_WEIGHT,
    DEFAULT_EBBINGHAUS_STRENGTH,
    USER_KEYWORDS,
)

# 兼容别名：MemoryBridge 即 mem0 后端
MemoryBridge = Mem0Backend

__all__ = ["MemoryBridge", "Mem0Backend", "create_backend",
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


# ── 展示用读——text 优先（l0_abstract 已废弃，仅作空值兜底）。──

def fmt_search_row(m: dict) -> str:
    """search 结果单行展示：text 优先，l0_abstract 兜底（不依赖死字段）。"""
    return f"[{m.get('category', '')}] {(m.get('text') or m.get('l0_abstract') or '')[:80]}"


if __name__ == "__main__":
    bridge = _default_bridge()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--search":
            query = sys.argv[2] if len(sys.argv) > 2 else "迟菓"
            results = bridge.search(query)
            for m in results:
                print(fmt_search_row(m))
        elif cmd == "--random":
            m = bridge.random_memory()
            if m:
                print(json.dumps(m, indent=2, ensure_ascii=False, default=str))
            else:
                print("无相关记忆")
        elif cmd == "--stats":
            print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
        elif cmd == "--add":
            text = sys.argv[2] if len(sys.argv) > 2 else ""
            if not text:
                print("用法: memory_bridge.py --add <文本>")
                sys.exit(1)
            ok = bridge.add_messages(text, metadata={"category": "manual"})
            print(json.dumps({"ok": ok}, ensure_ascii=False))
            sys.exit(0 if ok else 1)
        else:
            print(f"用法: {sys.argv[0]} [--search|--random|--stats|--add <文本>]")
    else:
        print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
