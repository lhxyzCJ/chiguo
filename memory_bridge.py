# ============================================================
# memory_bridge.py — 读取 OpenClaw LanceDB 记忆系统
# 只读。不写入（写入由 OpenClaw memory_store 完成）。
# 0 token 决策，0 额外依赖（lancedb + pandas 已装）。
# v4: 新增 Ebbinghaus 遗忘曲线加权（参考 MATE 架构）
# ============================================================

import json
import math
import os
import random
import time as _time_module
from datetime import datetime, timezone, timedelta

# 注意：lancedb 在 _ensure_table() 内惰性导入，不在此处顶层导入。
# lancedb 未安装时 daemon 仍可启动（available=False 优雅降级）。

CST = timezone(timedelta(hours=8))

# ── 搜索关键词（定位与迟菓/主人相关的记忆）─────────────────
USER_KEYWORDS = ["迟菓", "菓菓", "主人", "chiguo", "微信", "互动", "早安", "晚安"]

# ── Ebbinghaus 遗忘参数 ──
DEFAULT_EBBINGHAUS_STRENGTH = 168.0   # 记忆强度 S（小时），越大遗忘越慢
DEFAULT_EBBINGHAUS_MIN_WEIGHT = 0.1   # 最低权重（不会彻底遗忘）

# ── LanceDB 降级重试 ──
_RETRY_SECONDS = 60.0  # available=False 后至少间隔这么久才重新探测（--loop 长驻下故障恢复可自愈）


class MemoryBridge:
    """只读桥接：Python → OpenClaw LanceDB 记忆库。

    解耦特性：
    - 路径由配置文件注入，不硬编码
    - LanceDB 不可用时优雅降级（返回空结果，不抛异常）
    - 只读操作，不与 OpenClaw 文件锁冲突
    - 列名使用 .get() 访问，兼容 dreaming 插件的简化 schema
    """

    def __init__(self, db_path: str = None, table_name: str = "memories",
                 strength: float = None, min_weight: float = None):
        self.db_path = os.path.expanduser(db_path or "~/.openclaw/memory/lancedb-pro")
        self.table_name = table_name
        self._strength = strength or DEFAULT_EBBINGHAUS_STRENGTH
        self._min_weight = min_weight or DEFAULT_EBBINGHAUS_MIN_WEIGHT
        self._table = None
        self._lancedb = None  # 惰性导入缓存（模块级不再 import lancedb）
        self._available: bool | None = None  # None=未检测, True=可用, False=不可用
        self._last_probe: float = 0.0  # 最近一次探测时间戳（m12: 失败后按节流重试）

    @property
    def available(self) -> bool:
        """LanceDB 是否可用。不可用时所有查询返回空列表。

        m12: 探测失败后不永久缓存 False——每次探测间隔 >= _RETRY_SECONDS
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

    @staticmethod
    def _clean_importance(row) -> float:
        """importance 清洗（m5）：None/NaN/非数值 → 0.0。

        防止：None < min_importance 抛 TypeError；NaN 穿透过滤后
        进入 random.choices 权重导致加权随机行为异常。
        """
        v = row.get("importance")
        if v is None:
            return 0.0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if math.isnan(f) else f

    def _normalize_ts(self, ts: float) -> datetime:
        """timestamp may be epoch ms or epoch s. Normalize to datetime."""
        if ts > 1e12:
            return datetime.fromtimestamp(ts / 1000, tz=CST)
        elif ts > 0:
            return datetime.fromtimestamp(ts, tz=CST)
        return datetime(1970, 1, 1, tzinfo=CST)

    # ── 查询 API ──────────────────────────────────────────

    def search(self, query: str, limit: int = 10,
               category: str = None,
               min_importance: float = 0.3) -> list[dict]:
        """
        FTS 全文搜索。
        零 token，纯 BM25 关键词匹配。
        LanceDB 不可用时返回空列表。
        """
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
                if self._clean_importance(row) < min_importance:
                    continue
                results.append(self._row_to_dict(row, meta))
        except Exception:
            return []  # 结果行防御：任何行级异常 → 优雅降级为空
        return results

    def recent(self, hours: float = 168, limit: int = 20,
               category: str = None,
               min_importance: float = 0.3) -> list[dict]:
        """
        获取最近 N 小时的记忆。
        用 SQL WHERE timestamp 过滤（自动检测 epoch-ms vs epoch-s）。
        LanceDB 不可用时返回空列表。
        """
        if not self.available:
            return []
        cutoff_ms = (datetime.now(CST) - timedelta(hours=hours)).timestamp() * 1000
        try:
            where = f"timestamp >= {cutoff_ms}"
            if category:
                esc = category.replace("'", "''")
                where += f" AND category = '{esc}'"
            df = (self.table.search()
                  .where(where)
                  .limit(limit)
                  .to_pandas())
        except Exception:
            # timestamp 可能是秒级，尝试另一种方式
            return self._recent_fallback(hours, limit, category, min_importance)

        # ponytail: detect ms-vs-s timestamp by checking if zero results on non-empty table.
        # If LanceDB stores epoch-s, the ms-based SQL matches nothing without error.
        if len(df) == 0:
            try:
                total = self.table.count_rows()
                if total and total > 0:
                    return self._recent_fallback(hours, limit, category, min_importance)
            except Exception:
                pass

        results = []
        try:
            for _, row in df.iterrows():
                meta = self._parse_meta(row)
                if self._clean_importance(row) < min_importance:
                    continue
                results.append(self._row_to_dict(row, meta))
        except Exception:
            return []
        return results

    def _recent_fallback(self, hours, limit, category, min_importance):
        """timestamp 可能是 epoch 秒的情况"""
        cutoff_s = _time_module.time() - hours * 3600
        try:
            where = f"timestamp >= {cutoff_s}"
            if category:
                esc = category.replace("'", "''")
                where += f" AND category = '{esc}'"
            df = (self.table.search()
                  .where(where)
                  .limit(limit * 3)
                  .to_pandas())
        except Exception:
            # 全量扫描取前 N 条
            df = self.table.search().limit(limit * 10).to_pandas()

        results = []
        try:
            for _, row in df.iterrows():
                meta = self._parse_meta(row)
                ts = row.get("timestamp") or 0
                if ts > 1e12:
                    age_h = (_time_module.time() - ts / 1000) / 3600
                else:
                    age_h = (_time_module.time() - ts) / 3600
                if age_h > hours:
                    continue
                if self._clean_importance(row) < min_importance:
                    continue
                results.append(self._row_to_dict(row, meta))
                if len(results) >= limit:
                    break
        except Exception:
            return []
        return results

    def user_relevant(self, limit: int = 20,
                      min_importance: float = 0.3,
                      prefer_categories: list[str] = None) -> list[dict]:
        """
        获取与用户/迟菓相关的记忆。
        用 FTS 对多个关键词搜索后合并去重。
        LanceDB 不可用时返回空列表。
        """
        if not self.available:
            return []
        seen = set()
        results = []
        for kw in USER_KEYWORDS:
            for mem in self.search(kw, limit=10, min_importance=min_importance):
                if mem["id"] not in seen:
                    seen.add(mem["id"])
                    results.append(mem)
            if len(results) >= limit:
                break
        # 偏好类别放在前面
        if prefer_categories:
            def _sort_key(m):
                cat = m.get("memory_category", "?")
                return 0 if cat in prefer_categories else 1
            results.sort(key=_sort_key)
        # 按时间降序排列
        results.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
        return results[:limit]

    def random_memory(self, category: str = None,
                      min_importance: float = 0.5,
                      prefer_categories: list[str] = None) -> dict | None:
        """
        随机返回一条与用户相关的记忆。
        用于「突然想起一件事」触发。
        prefer_categories: 偏向的记忆类别，如 ['preferences', 'entities', 'events']
        """
        if prefer_categories is None:
            prefer_categories = ["preferences", "entities", "events", "profile"]
        relevant = self.user_relevant(limit=50, min_importance=min_importance,
                                       prefer_categories=prefer_categories)
        if not relevant:
            return None
        # 加权随机（重要性高 → 概率高）
        weights = [m.get("importance", 0.5) ** 2 for m in relevant]
        total = sum(weights)
        if total <= 0:
            return random.choice(relevant) if relevant else None
        return random.choices(relevant, weights=weights, k=1)[0]

    # ── 统计 ──────────────────────────────────────────────

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

    # ── Ebbinghaus 遗忘曲线（v4） ──────────────────────────

    def ebbinghaus_weight(self, memory: dict, now: datetime = None,
                          strength: float = None,
                          min_weight: float = None) -> float:
        """
        Ebbinghaus 遗忘曲线权重。
        R = e^(-t / (S * importance))

        t = 记忆年龄（小时）
        S = 记忆强度参数（越大遗忘越慢，默认 168h = 7天）
        importance = 记忆重要性 0.1~1.0，越重要衰减越慢

        返回 0~1 的权重。min_weight 防止彻底遗忘（默认 0.1）。

        参考：MATE (doi:10.5281/zenodo.19227919) 的 Ebbinghaus 遗忘模型
        """
        if strength is None:
            strength = getattr(self, '_strength', DEFAULT_EBBINGHAUS_STRENGTH)
        if min_weight is None:
            min_weight = getattr(self, '_min_weight', DEFAULT_EBBINGHAUS_MIN_WEIGHT)
        if now is None:
            now = datetime.now(CST)

        ts = memory.get("timestamp", 0)
        if ts <= 0:
            return 1.0

        # 计算年龄（小时）
        if ts > 1e12:
            age_seconds = (now.timestamp() - ts / 1000)
        else:
            age_seconds = (now.timestamp() - ts)
        age_hours = max(0, age_seconds / 3600)

        importance = max(0.1, min(1.0, memory.get("importance", 0.5)))
        # 有效强度 = S * importance（重要记忆衰减慢）
        effective_strength = strength * importance

        if effective_strength <= 0:
            return min_weight

        weight = math.exp(-age_hours / effective_strength)
        return max(min_weight, min(1.0, weight))

    def search_with_forgetting(self, query: str, limit: int = 10,
                                category: str = None,
                                min_importance: float = 0.3,
                                now: datetime = None,
                                strength: float = None,
                                min_weight: float = None) -> list[dict]:
        """
        search() 的包装，结果按遗忘权重重排序。
        新/重要的记忆排在前面，旧/不重要的记忆排在后面。

        参考：MATE 的 Ebbinghaus 遗忘曲线应用于记忆检索
        """
        results = self.search(query, limit=limit * 3, category=category,
                              min_importance=min_importance)
        if not results:
            return []

        if now is None:
            now = datetime.now(CST)

        # 计算加权分数：原始相关性 × 遗忘权重
        for mem in results:
            mem["_ebbinghaus_weight"] = self.ebbinghaus_weight(
                mem, now, strength, min_weight
            )
            mem["_score"] = mem.get("importance", 0.5) * mem["_ebbinghaus_weight"]

        # 按加权分数降序
        results.sort(key=lambda m: m["_score"], reverse=True)

        # 清理内部字段
        for mem in results:
            mem.pop("_ebbinghaus_weight", None)
            mem.pop("_score", None)

        return results[:limit]

    def user_relevant_with_forgetting(self, limit: int = 20,
                                       min_importance: float = 0.3,
                                       prefer_categories: list[str] = None,
                                       now: datetime = None,
                                       strength: float = None,
                                       min_weight: float = None) -> list[dict]:
        """
        user_relevant() 的包装，结果按遗忘权重重排序。
        """
        results = self.user_relevant(limit=limit * 3, min_importance=min_importance,
                                      prefer_categories=prefer_categories)
        if not results:
            return []

        if now is None:
            now = datetime.now(CST)

        for mem in results:
            mem["_ebbinghaus_weight"] = self.ebbinghaus_weight(
                mem, now, strength, min_weight
            )
            mem["_score"] = mem.get("importance", 0.5) * mem["_ebbinghaus_weight"]

        # 偏好类别加分
        if prefer_categories:
            for mem in results:
                if mem.get("memory_category") in prefer_categories:
                    mem["_score"] *= 1.2

        results.sort(key=lambda m: m["_score"], reverse=True)

        for mem in results:
            mem.pop("_ebbinghaus_weight", None)
            mem.pop("_score", None)

        return results[:limit]

    def random_memory_with_forgetting(self, category: str = None,
                                       min_importance: float = 0.5,
                                       prefer_categories: list[str] = None,
                                       now: datetime = None,
                                       strength: float = None,
                                       min_weight: float = None) -> dict | None:
        """
        random_memory() 的包装，用遗忘权重加权随机选择。
        新记忆被选中的概率更高。
        """
        if prefer_categories is None:
            prefer_categories = ["preferences", "entities", "events", "profile"]

        relevant = self.user_relevant_with_forgetting(
            limit=50, min_importance=min_importance,
            prefer_categories=prefer_categories,
            now=now, strength=strength, min_weight=min_weight,
        )
        if not relevant:
            return None

        # 加权随机：遗忘权重高 + 重要性高 → 概率高
        if now is None:
            now = datetime.now(CST)
        weights = []
        for m in relevant:
            ebw = self.ebbinghaus_weight(m, now, strength, min_weight)
            w = m.get("importance", 0.5) ** 2 * ebw
            weights.append(w)

        total = sum(weights)
        if total <= 0:
            return random.choice(relevant)
        return random.choices(relevant, weights=weights, k=1)[0]

    # ── 内部 ──────────────────────────────────────────────

    def _row_to_dict(self, row, meta: dict) -> dict:
        ts = row.get("timestamp") or 0  # m5: None timestamp 防御
        return {
            "id": row.get("id", ""),
            "text": row.get("text", ""),
            "category": row.get("category", ""),
            "scope": row.get("scope", "global"),
            "importance": self._clean_importance(row),  # m5: 下游（Ebbinghaus/加权随机）不再见 None/NaN
            "timestamp": ts,
            "datetime": self._normalize_ts(ts).isoformat(),
            "memory_category": meta.get("memory_category", "?"),
            "l0_abstract": meta.get("l0_abstract", ""),
            "l2_content": meta.get("l2_content", ""),
            "tier": meta.get("tier", "working"),
            "source": meta.get("source", "?"),
        }


# ── CLI ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    bridge = MemoryBridge()

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
        elif cmd == "--recent":
            hours = float(sys.argv[2]) if len(sys.argv) > 2 else 168
            results = bridge.recent(hours=hours)
            for m in results:
                print(f"[{m['category']}] {m['datetime']} | {m['l0_abstract'] or m['text'][:80]}")
        elif cmd == "--stats":
            print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
        else:
            print(f"用法: {sys.argv[0]} [--search|--random|--recent|--stats]")
    else:
        print(json.dumps(bridge.stats(), indent=2, ensure_ascii=False))
