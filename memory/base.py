# ============================================================
# memory/base.py — 记忆后端抽象基类（v1.8 解耦）
#
# 任意记忆模块替换点：实现 search/random_memory/stats/available
# 四个原语（子类），Ebbinghaus 遗忘曲线包装、用户相关召回等
# 通用逻辑全部在基类完成，后端只负责"存什么、怎么搜"。
#
# 现成实现：
#   memory/mem0_backend.py  Mem0Backend — mem0 AI 记忆层（读写双向，
#     LLM 事实提取写入 + 向量语义检索；见 doc/SYSTEM.md「记忆后端抽象」）
# 自定义后端：实现本基类四个原语，toml [memory].backend 填
#   "module.path.ClassName" 即可热接入（见 memory/factory.py）。
# ============================================================

import math
import random
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# ── Ebbinghaus 遗忘参数（v4）──
DEFAULT_EBBINGHAUS_STRENGTH = 168.0   # 记忆强度 S（小时），越大遗忘越慢
DEFAULT_EBBINGHAUS_MIN_WEIGHT = 0.1   # 最低权重（不会彻底遗忘）

# ── 搜索关键词（定位与迟菓/主人相关的记忆）─────────────────
USER_KEYWORDS = ["迟菓", "菓菓", "主人", "chiguo", "微信", "互动", "早安", "晚安"]


class MemoryBackend:
    """记忆后端抽象基类：统一 dict 行契约 + Ebbinghaus 包装。

    行契约（子类 search/random_memory 返回的 dict 字段，消费方依赖）：
      id/text/category/scope/importance/timestamp/datetime/
      memory_category/l0_abstract/l2_content/tier/source
    importance 必须清洗为非 NaN 数值（_clean_importance 兜底）。

    子类需实现：
      available (property) — 后端是否可用（不可用 → 查询返回空，不抛）
      search(query, limit, category, min_importance) -> list[dict]
      random_memory(category, min_importance, prefer_categories) -> dict | None
      stats() -> dict
    基类已实现：user_relevant（多关键词合并召回）、ebbinghaus_weight、
    search/user_relevant/random_memory 的 forgetting 包装。
    """

    # ── 子类原语 ─────────────────────────────────────────

    @property
    def available(self) -> bool:
        raise NotImplementedError

    def search(self, query: str, limit: int = 10,
               category: str = None,
               min_importance: float = 0.3) -> list[dict]:
        raise NotImplementedError

    def random_memory(self, category: str = None,
                      min_importance: float = 0.5,
                      prefer_categories: list[str] = None) -> dict | None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    # ── 共享工具 ──────────────────────────────────────────

    @staticmethod
    def clean_importance(row) -> float:
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

    @staticmethod
    def normalize_ts(ts: float) -> datetime:
        """timestamp may be epoch ms or epoch s. Normalize to datetime."""
        if ts > 1e12:
            return datetime.fromtimestamp(ts / 1000, tz=CST)
        elif ts > 0:
            return datetime.fromtimestamp(ts, tz=CST)
        return datetime(1970, 1, 1, tzinfo=CST)

    # ── 基类实现：用户相关召回（基于 search 原语）──────────

    def user_relevant(self, limit: int = 20,
                      min_importance: float = 0.3,
                      prefer_categories: list[str] = None) -> list[dict]:
        """获取与用户/迟菓相关的记忆（多关键词 FTS/匹配合并去重）。

        偏好类别放在前面；按时间降序排列。后端不可用返回空列表。
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
        if prefer_categories:
            def _sort_key(m):
                cat = m.get("memory_category", "?")
                return 0 if cat in prefer_categories else 1
            results.sort(key=_sort_key)
        results.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
        return results[:limit]

    # ── Ebbinghaus 遗忘曲线（v4；纯逻辑，后端无关）─────────

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

        if ts > 1e12:
            age_seconds = (now.timestamp() - ts / 1000)
        else:
            age_seconds = (now.timestamp() - ts)
        age_hours = max(0, age_seconds / 3600)

        importance = max(0.1, min(1.0, memory.get("importance", 0.5)))
        effective_strength = strength * importance

        if effective_strength <= 0:
            return min_weight

        weight = math.exp(-age_hours / effective_strength)
        return max(min_weight, min(1.0, weight))

    def _apply_forgetting(self, results: list[dict], now: datetime,
                          strength: float = None, min_weight: float = None,
                          prefer_categories: list[str] = None,
                          limit: int = None) -> list[dict]:
        """Ebbinghaus 权重重排核心（search_with_forgetting 等共用）。"""
        for mem in results:
            mem["_ebbinghaus_weight"] = self.ebbinghaus_weight(
                mem, now, strength, min_weight
            )
            mem["_score"] = mem.get("importance", 0.5) * mem["_ebbinghaus_weight"]
        if prefer_categories:
            for mem in results:
                if mem.get("memory_category") in prefer_categories:
                    mem["_score"] *= 1.2
        results.sort(key=lambda m: m["_score"], reverse=True)
        for mem in results:
            mem.pop("_ebbinghaus_weight", None)
            mem.pop("_score", None)
        return results[:limit] if limit is not None else results

    def search_with_forgetting(self, query: str, limit: int = 10,
                                category: str = None,
                                min_importance: float = 0.3,
                                now: datetime = None,
                                strength: float = None,
                                min_weight: float = None) -> list[dict]:
        """search() 的包装，结果按遗忘权重重排序（新/重要记忆在前）。"""
        results = self.search(query, limit=limit * 3, category=category,
                              min_importance=min_importance)
        if not results:
            return []
        if now is None:
            now = datetime.now(CST)
        return self._apply_forgetting(results, now, strength, min_weight, limit=limit)

    def user_relevant_with_forgetting(self, limit: int = 20,
                                       min_importance: float = 0.3,
                                       prefer_categories: list[str] = None,
                                       now: datetime = None,
                                       strength: float = None,
                                       min_weight: float = None) -> list[dict]:
        """user_relevant() 的包装，结果按遗忘权重重排序（偏好类别 ×1.2）。"""
        results = self.user_relevant(limit=limit * 3, min_importance=min_importance,
                                      prefer_categories=prefer_categories)
        if not results:
            return []
        if now is None:
            now = datetime.now(CST)
        return self._apply_forgetting(results, now, strength, min_weight,
                                      prefer_categories=prefer_categories, limit=limit)

    def random_memory_with_forgetting(self, category: str = None,
                                       min_importance: float = 0.5,
                                       prefer_categories: list[str] = None,
                                       now: datetime = None,
                                       strength: float = None,
                                       min_weight: float = None) -> dict | None:
        """random_memory() 的包装，用遗忘权重加权随机（新记忆更可能被选中）。"""
        if prefer_categories is None:
            prefer_categories = ["preferences", "entities", "events", "profile"]

        relevant = self.user_relevant_with_forgetting(
            limit=50, min_importance=min_importance,
            prefer_categories=prefer_categories,
            now=now, strength=strength, min_weight=min_weight,
        )
        if not relevant:
            return None

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
