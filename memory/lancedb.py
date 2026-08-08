# ============================================================
# memory/lancedb.py — LanceDB 记忆后端（只读）
# 读取 pi 记忆扩展（memory-lancedb-pro）写入的记忆库，不写入。
# 0 token 决策；lancedb 惰性导入：未安装/不可用 → available=False
# 优雅降级（查询返回空，不抛异常），60s 节流重试自愈。
# ============================================================

import json
import os
import random
import time as _time_module

from memory.base import MemoryBackend

# ── LanceDB 降级重试 ──
_RETRY_SECONDS = 60.0  # available=False 后至少间隔这么久才重新探测（--loop 长驻下故障恢复可自愈）


class LanceDbBackend(MemoryBackend):
    """LanceDB FTS 只读后端。

    行结构依赖（.get() 防御式访问）：id/text/category/scope/importance/timestamp
    + metadata JSON 里的 memory_category/l0_abstract/l2_content/tier/source
    （由 pi 记忆扩展写入；兼容 dreaming 插件的简化 schema）。
    """

    def __init__(self, db_path: str = None, table_name: str = "memories",
                 strength: float = None, min_weight: float = None):
        self.db_path = os.path.expanduser(db_path or "~/.pi-agent/memory/lancedb-pro")
        self.table_name = table_name
        self._strength = strength or 168.0
        self._min_weight = min_weight or 0.1
        self._table = None
        self._lancedb = None  # 惰性导入缓存（模块级不再 import lancedb）
        self._available: bool | None = None  # None=未检测, True=可用, False=不可用
        self._last_probe: float = 0.0  # 最近一次探测时间戳（失败后按节流重试）

    @property
    def available(self) -> bool:
        """LanceDB 是否可用。不可用时所有查询返回空列表。

        探测失败后不永久缓存 False——每次探测间隔 >= _RETRY_SECONDS
        就重新尝试，--loop 长驻时 LanceDB 故障恢复可自愈。
        """
        if self._available is True:
            return True
        if self._available is False and (
            _time_module.time() - self._last_probe < _RETRY_SECONDS
        ):
            return False
        try:
            self._ensure_table()
            self._available = True
        except Exception:
            self._available = False
        self._last_probe = _time_module.time()
        return self._available

    def _ensure_table(self):
        """延迟连接，失败抛异常（由 available 捕获）"""
        if self._table is None:
            if self._lancedb is None:
                import lancedb  # 惰性导入：lancedb 缺失时在此抛 ImportError → available=False
                self._lancedb = lancedb
            db = self._lancedb.connect(self.db_path)
            self._table = db.open_table(self.table_name)

    @property
    def table(self):
        if not self.available:
            return None
        self._ensure_table()
        return self._table

    def _parse_meta(self, row) -> dict:
        try:
            return json.loads(row.get("metadata", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    def search(self, query: str, limit: int = 10,
               category: str = None,
               min_importance: float = 0.3) -> list[dict]:
        """FTS 全文搜索（零 token，纯 BM25 关键词匹配）。不可用返回空列表。"""
        if not self.available:
            return []
        try:
            s = self.table.search(query, query_type="fts").limit(limit)
            if category:
                esc = category.replace("'", "''")
                s = s.where(f"category = '{esc}'")
            df = s.to_pandas()
        except Exception:
            return []

        results = []
        try:
            for _, row in df.iterrows():
                meta = self._parse_meta(row)
                if self.clean_importance(row) < min_importance:
                    continue
                results.append(self._row_to_dict(row, meta))
        except Exception:
            return []  # 结果行防御：任何行级异常 → 优雅降级为空
        return results

    def random_memory(self, category: str = None,
                      min_importance: float = 0.5,
                      prefer_categories: list[str] = None) -> dict | None:
        """随机返回一条与用户相关的记忆（重要性² 加权随机）。"""
        if prefer_categories is None:
            prefer_categories = ["preferences", "entities", "events", "profile"]
        relevant = self.user_relevant(limit=50, min_importance=min_importance,
                                       prefer_categories=prefer_categories)
        if not relevant:
            return None
        weights = [m.get("importance", 0.5) ** 2 for m in relevant]
        total = sum(weights)
        if total <= 0:
            return random.choice(relevant) if relevant else None
        return random.choices(relevant, weights=weights, k=1)[0]

    def stats(self) -> dict:
        if not self.available:
            return {
                "total_memories": 0,
                "user_relevant_count": 0,
                "db_path": self.db_path,
                "available": False,
            }
        try:
            total = self.table.count_rows()
        except Exception:
            total = "?"
        return {
            "total_memories": total,
            "user_relevant_count": len(self.user_relevant(limit=100)),
            "db_path": self.db_path,
            "available": True,
        }

    def _row_to_dict(self, row, meta: dict) -> dict:
        ts = row.get("timestamp") or 0  # None timestamp 防御
        return {
            "id": row.get("id", ""),
            "text": row.get("text", ""),
            "category": row.get("category", ""),
            "scope": row.get("scope", "global"),
            "importance": self.clean_importance(row),  # 下游（Ebbinghaus/加权随机）不再见 None/NaN
            "timestamp": ts,
            "datetime": self.normalize_ts(ts).isoformat(),
            "memory_category": meta.get("memory_category", "?"),
            "l0_abstract": meta.get("l0_abstract", ""),
            "l2_content": meta.get("l2_content", ""),
            "tier": meta.get("tier", "working"),
            "source": meta.get("source", "?"),
        }
