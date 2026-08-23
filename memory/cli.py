# ============================================================
# memory/cli.py — 记忆后端 CLI（一等公民；原根目录 memory_bridge.py 门面已删）
#
# 经 memory.create_backend 工厂创建后端，尊重 toml [memory].backend，
# 提供 --search/--random/--stats/--add 四个展示/维护子命令。
# 使用：python -m memory <--search|--random|--stats|--add <文本>>
# 或 python -m memory.cli <...>（同入口）。
# ============================================================

import json
import sys

from memory import create_backend


def _default_bridge():
    """按仓库 toml [memory] 段创建后端（CLI/手工检查用）。"""
    import tomllib
    from pathlib import Path

    # 仓库根：memory/cli.py → memory/ → 根目录
    repo = Path(__file__).resolve().parent.parent
    try:
        with open(repo / "chiguo_proactive.toml", "rb") as f:
            cfg = tomllib.load(f)
    except (ValueError, TypeError, OSError):
        cfg = {}
    return create_backend(cfg.get("memory", {}), base_dir=repo)


# ── C3: 展示用读——text 优先（l0_abstract 已废弃，仅作空值兜底）。──

def fmt_search_row(m: dict) -> str:
    """search 结果单行展示：text 优先，l0_abstract 兜底（不依赖死字段）。"""
    return f"[{m.get('category', '')}] {(m.get('text') or m.get('l0_abstract') or '')[:80]}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bridge = _default_bridge()

    if argv:
        cmd = argv[0]
        if cmd == "--search":
            query = argv[1] if len(argv) > 1 else "迟菓"
            results = bridge.search(query)
            for m in results:
                print(fmt_search_row(m))
            return 0
        if cmd == "--random":
            m = bridge.random_memory()
            if m:
                print(json.dumps(m, indent=2, ensure_ascii=False, default=str))
            else:
                print("无相关记忆")
            return 0
        if cmd == "--stats":
            print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
            return 0
        if cmd == "--add":
            text = argv[1] if len(argv) > 1 else ""
            if not text:
                print("用法: python -m memory --add <文本>")
                return 1
            ok = bridge.add_messages(text, metadata={"category": "manual"})
            print(json.dumps({"ok": ok}, ensure_ascii=False))
            return 0 if ok else 1
        print(f"用法: {argv[0]} [--search|--random|--stats|--add <文本>]")
        return 2
    print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
