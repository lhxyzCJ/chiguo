# ============================================================
# memory/json.py — JSON 文件记忆后端（零依赖兜底）
#
# 读取手动记忆文件（默认 data/chiguo_memories.json）：
#   [{"id"?, "text", "category"?, "scope"?, "importance"?,
#     "timestamp"?, "memory_category"?}, ...]
# 简单字符串匹配检索（大小写不敏感），importance 过滤 +
# 基类 Ebbinghaus 包装。文件缺失/损坏 → available=False 优雅降级。
# ============================================================

import json
import random

from memory.base import MemoryBackend


class JsonMemoryBackend(MemoryBackend):
    """手动记忆 JSON 后端：任意路径文件，零额外依赖。

    与 LanceDbBackend 输出相同的 dict 行契约，可无缝替换
    （toml [memory].backend = "json" 即切换）。
    """

    def __init__(self, path: str = None, strength: float = None,
                 min_weight: float = None):
        self.path = path or "data/chiguo_memories.json"
        self._strength = strength or 168.0
        self._min_weight = min_weight or 0.1
        self._items: list[dict] | None = None  # None=未加载/不可用

    # ── 原语 ──────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> list[dict] | None:
        """加载并规范化条目。失败返回 None（不抛）。"""
        if self._items is not None:
            return self._items
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return None
        if not isinstance(raw, list):
            self._items = []
            return self._items
        items = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            items.append(self._normalize(item, i))
        self._items = items
        return self._items

    def search(self, query: str, limit: int = 10,
               category: str = None,
               min_importance: float = 0.3) -> list[dict]:
        """大小写不敏感子串匹配（text/category）。不可用返回空列表。"""
        items = self._load()
        if items is None:
            return []
        q = query.lower().strip()
        out = []
        for row in items:
            if self.clean_importance(row) < min_importance:
                continue
            if category and row["category"] != category:
                continue
            if q and q not in row["text"].lower() and q not in row["category"].lower():
                continue
            out.append(row)
        return out[:limit]

    def random_memory(self, category: str = None,
                      min_importance: float = 0.5,
                      prefer_categories: list[str] = None) -> dict | None:
        """importance² 加权随机。无候选返回 None。"""
        if prefer_categories is None:
            prefer_categories = ["preferences", "entities", "events", "profile"]
        relevant = self.user_relevant(limit=50, min_importance=min_importance,
                                       prefer_categories=prefer_categories)
        if not relevant:
            return None
        weights = [m.get("importance", 0.5) ** 2 for m in relevant]
        total = sum(weights)
        if total <= 0:
            return random.choice(relevant)
        return random.choices(relevant, weights=weights, k=1)[0]

    def stats(self) -> dict:
        items = self._load()
        if items is None:
            return {
                "total_memories": 0,
                "user_relevant_count": 0,
                "db_path": self.path,
                "available": False,
            }
        return {
            "total_memories": len(items),
            "user_relevant_count": len(self.user_relevant(limit=100)),
            "db_path": self.path,
            "available": True,
        }

    # ── 内部 ──────────────────────────────────────────────

    def _normalize(self, item: dict, idx: int) -> dict:
        """JSON 条目 → 统一行契约 dict（字段缺失兜底默认值）。"""
        ts = item.get("timestamp") or 0
        text = str(item.get("text", "") or "")
        return {
            "id": str(item.get("id", f"json-{idx}")),
            "text": text,
            "category": str(item.get("category", "") or ""),
            "scope": str(item.get("scope", "global") or "global"),
            "importance": self.clean_importance(item),
            "timestamp": ts,
            "datetime": self.normalize_ts(ts).isoformat(),
            "memory_category": str(item.get("memory_category", "?") or "?"),
            "l0_abstract": str(item.get("l0_abstract", "") or ""),
            "l2_content": str(item.get("l2_content", "") or ""),
            "tier": str(item.get("tier", "working") or "working"),
            "source": str(item.get("source", "manual") or "manual"),
        }
