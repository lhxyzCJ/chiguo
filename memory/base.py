# ============================================================
# memory/base.py — 记忆后端抽象基类（v1.8 解耦）
#
# mem0 为唯一后端；MemoryBackend 抽象保留作内部测试桩/复用层
# （Ebbinghaus 遗忘曲线包装、用户相关召回等通用逻辑全部在基类完成，
# 后端只负责"存什么、怎么搜"；子类实现四个原语）。
#
# 现成实现：
#   memory/mem0_backend.py  Mem0Backend — mem0 AI 记忆层（读写双向，
#     LLM 事实提取写入 + 向量语义检索；见 doc/SYSTEM.md「记忆后端抽象」）
# ============================================================

import math
import random
from datetime import datetime, timezone, timedelta

from chiguo_math import jaccard_3gram  # C1: 文本相似度（零新依赖，3-gram Jaccard）

CST = timezone(timedelta(hours=8))

# ── C1: 确定性记忆巩固默认参数（config [memory].consolidate_* 可覆盖）──
DEFAULT_CONSOLIDATE_SIM_THRESHOLD = 0.85   # jaccard_3gram 相似度阈值，≥ 视为近似重复
DEFAULT_CONSOLIDATE_MIN_IMPORTANCE = 0.3   # 低于此重要度且超龄 → 过期候选
DEFAULT_CONSOLIDATE_MAX_AGE_HOURS = 720.0  # 低重要度记忆超龄阈值（小时，720=30天）
DEFAULT_CONSOLIDATE_PAIR_SCAN = 10         # 去重时每个候选最多向后比对行数（O(n·k) 有界）

# ── C2: Ebbinghaus 复习强化默认参数（config [memory].reinforce_* 可覆盖）──
DEFAULT_REINFORCE_BONUS = 0.0              # 每次召回 importance ×(1+bonus×count)；0=关闭恒等

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

    # ── C1: 确定性记忆巩固（无 LLM 的 Letta dreaming / Deep Dream 版）──

    @staticmethod
    def consolidate_plan(rows: list[dict], now: datetime,
                         sim_threshold: float = DEFAULT_CONSOLIDATE_SIM_THRESHOLD,
                         min_importance: float = DEFAULT_CONSOLIDATE_MIN_IMPORTANCE,
                         max_age_hours: float = DEFAULT_CONSOLIDATE_MAX_AGE_HOURS,
                         pair_scan: int = DEFAULT_CONSOLIDATE_PAIR_SCAN) -> dict:
        """生成确定性巩固计划（纯函数，零 LLM，不写库）。

        对标 Letta dreaming / CowAgent Deep Dream，但吸收思想不换库：
          - 去重：按 text 的 jaccard_3gram 相似度 ≥ sim_threshold 找近似重复对，
            保留 importance 高 / 时间新的一条（排序靠前者），另一条降权
            （importance 减半 + `_consolidated` 标记 + consolidated_with）。
          - 过期：importance < min_importance 且年龄 > max_age_hours → 标记
            `_expired`（候选删除）。timestamp 缺失/非法（≤0）→ 年龄未知，不过期
            （避免误删脏数据）。

        rows 会被就地补标记/改 importance（消费方按需读改写）。
        返回报告：{"demoted": [...], "expired": [...], "kept": [...]}，每条为行 dict。
        """
        if now is None:
            now = datetime.now(CST)

        def _age_hours(r: dict) -> float | None:
            ts = r.get("timestamp") or 0
            if not isinstance(ts, (int, float)) or ts <= 0:
                return None
            ts = ts / 1000.0 if ts > 1e12 else ts  # epoch ms → s
            return max(0.0, (now.timestamp() - ts) / 3600.0)

        # 排序：importance 高 / 时间新在前（前者为"保留方"）
        # C3 兼容：timestamp 可能为 ISO 字符串/None/非数值（原始行直入纯函数时），
        # 排序键归一化为数值，避免 float < str 抛 TypeError（年龄未知不过期）。
        def _norm_ts(r: dict) -> float:
            ts = r.get("timestamp")
            return float(ts) if isinstance(ts, (int, float)) else 0.0
        ordered = sorted(rows, key=lambda r: (MemoryBackend.clean_importance(r),
                                              _norm_ts(r)),
                         reverse=True)
        demoted_ids: set[str] = set()
        demoted: list[dict] = []
        for i, keeper in enumerate(ordered):
            if keeper.get("id") in demoted_ids:
                continue  # 已被降权，不再作为保留方
            for other in ordered[i + 1:i + 1 + max(pair_scan, 1)]:
                if other.get("id") in demoted_ids:
                    continue
                sim = jaccard_3gram(keeper.get("text", ""), other.get("text", ""))
                if sim >= sim_threshold:
                    other["_consolidated"] = True
                    other["consolidated_with"] = keeper.get("id")
                    other["importance"] = MemoryBackend.clean_importance(other) / 2.0
                    demoted.append(other)
                    demoted_ids.add(other.get("id"))
        # 过期：未参与去重的行，低重要度且超龄
        expired: list[dict] = []
        for r in ordered:
            if r.get("id") in demoted_ids:
                continue
            age = _age_hours(r)
            if age is None:
                continue  # 年龄未知不过期
            if MemoryBackend.clean_importance(r) < min_importance and age > max_age_hours:
                r["_expired"] = True
                expired.append(r)
        kept = [r for r in ordered if r.get("id") not in demoted_ids
                and not r.get("_expired")]
        return {"demoted": demoted, "expired": expired, "kept": kept}

    # ── C2: Ebbinghaus 复习强化（对标 FSRS：成功召回 → 强度 S 增大）──

    def _recall_counts_dict(self) -> dict:
        """惰性初始化 recall_count 表（内存侧；写回由 _persist_recall 钩子负责）。"""
        if not hasattr(self, "_recall_counts"):
            self._recall_counts = {}
        return self._recall_counts

    def _reinforce_attrs(self) -> tuple[bool, float]:
        """读取 reinforce 配置（constructor kwargs 或子类属性；缺省关闭恒等）。"""
        enabled = bool(getattr(self, "_reinforce_enabled", False))
        try:
            bonus = float(getattr(self, "_reinforce_bonus", 0.0) or 0.0)
        except (TypeError, ValueError):
            bonus = 0.0
        return enabled, bonus

    def note_recalled(self, memory_ids: list[str] | None) -> int:
        """C2: 记录记忆被召回（复习强化；[memory].reinforce_enabled 默认 False 恒等）。

        被召回记忆 recall_count+1；importance 有效值 = importance × (1 + bonus×count)
        （见 _effective_importance，读侧加权用）。写回由 _persist_recall 钩子完成：
        基类 no-op（内存侧），Mem0Backend 覆写为 mem0 update_memory（有该能力时）。
        返回本次记录条数。

        A2 跨进程累积（Issue #133）：cron 每 15 分钟起新进程，_recall_counts dict 从空
        开始——只从 dict 取数会把持久化 recall_count 覆盖成 1。计数基数改为先经
        _load_recall_count 读回后端持久化旧值，再 +1；进程内 dict 仅作会话内缓存
        （同一进程内多次召回不再重复读库）。reinforce 关闭 → 仍不引入任何副作用。
        """
        enabled, bonus = self._reinforce_attrs()
        if not enabled or bonus <= 0:
            return 0  # 默认关闭：不引入任何副作用，read 侧纯函数语义保持
        counts = self._recall_counts_dict()
        n = 0
        for mid in memory_ids or []:
            if not mid:
                continue
            if mid not in counts:
                try:
                    persisted = int(self._load_recall_count(mid) or 0)
                except (TypeError, ValueError):
                    persisted = 0  # 后端读回异常/非法值 → 兜底 0，不阻断召回记录
                counts[mid] = persisted
            cnt = counts[mid] + 1
            counts[mid] = cnt
            try:
                self._persist_recall(mid, cnt)
            except Exception:
                pass  # 写回失败不阻断召回记录
            n += 1
        return n

    def _persist_recall(self, memory_id: str, count: int):
        """C2: 召回次数持久化钩子。基类 no-op；子类（Mem0Backend）覆写写回。"""
        return None

    def _load_recall_count(self, memory_id: str) -> int:
        """C2: 读取后端已持久化的 recall_count（A2 跨进程累积数据源）。

        基类 no-op 返回 0（无持久化能力）；子类（Mem0Backend）覆写为 mem0 get 读 metadata。
        """
        return 0

    def _effective_importance(self, mem: dict) -> float:
        """C2: 记忆有效重要度 = importance × (1 + reinforce_bonus × recall_count)。

        reinforce 关闭（默认）→ 恒等返回 raw importance（read 侧行为不变）。
        """
        imp = self.clean_importance(mem)
        enabled, bonus = self._reinforce_attrs()
        if not enabled or bonus <= 0:
            return imp
        cnt = self._recall_counts_dict().get(mem.get("id"), 0)
        if cnt <= 0:
            # 跨进程回读：cron 每 15 分钟起新进程，_recall_counts 从空开始——读
            # 行 dict 里持久化的 recall_count（_row 已从 mem0 metadata 映射）。
            try:
                cnt = int(mem.get("recall_count") or 0)
            except (TypeError, ValueError):
                cnt = 0
        if cnt <= 0:
            return imp
        return min(1.0, imp * (1.0 + bonus * cnt))

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
        # 单一 sort：偏好类别在前，同类别内按时间降序
        # （原先两次 sort 相互覆盖，prefer_categories 不生效）
        def _cat_rank(m):
            if prefer_categories and m.get("memory_category") in prefer_categories:
                return 0
            return 1
        results.sort(key=lambda m: (_cat_rank(m), -(m.get("timestamp") or 0)))
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

        # importance 统一经 clean_importance 清洗（None/NaN/非数值 → 0.0），
        # 防御脏数据进入权重计算；清洗后为 0 则回退中位权重 0.5
        importance = self.clean_importance(memory)
        if importance <= 0:
            importance = 0.5
        importance = max(0.1, min(1.0, importance))
        effective_strength = strength * importance

        if effective_strength <= 0:
            return min_weight

        weight = math.exp(-age_hours / effective_strength)
        return max(min_weight, min(1.0, weight))

    @staticmethod
    def emotion_tag_similarity(mem_tag, req_tag) -> float:
        """B2: 情绪标签相似度（0~1）= 请求档位与记忆档位匹配的维度占比。

        记忆或请求任一缺 emotion_tag（None/非 dict/空 dict）→ 0（不加权）。
        user_mood 键存在时也参与比对（写侧打标含 user_mood）。
        """
        if not isinstance(mem_tag, dict) or not isinstance(req_tag, dict) or not req_tag:
            return 0.0
        matched = sum(1 for k, v in req_tag.items() if mem_tag.get(k) == v)
        return matched / len(req_tag)

    def _apply_forgetting(self, results: list[dict], now: datetime,
                          strength: float = None, min_weight: float = None,
                          prefer_categories: list[str] = None,
                          limit: int = None,
                          emotion_tag: dict = None,
                          emotion_tag_weight: float = 0.0) -> list[dict]:
        """Ebbinghaus 权重重排核心（search_with_forgetting 等共用）。

        B2: emotion_tag（请求当前情绪）+ emotion_tag_weight>0 时，对带 emotion_tag
        的记忆按相似度加权 ×(1 + weight × sim)；weight 默认 0 → 恒等关闭。
        """
        for mem in results:
            mem["_ebbinghaus_weight"] = self.ebbinghaus_weight(
                mem, now, strength, min_weight
            )
            # C2: importance 走 _effective_importance（reinforce 开启时被召回记忆更强）
            mem["_score"] = self._effective_importance(mem) * mem["_ebbinghaus_weight"]
            if emotion_tag and emotion_tag_weight > 0:
                sim = self.emotion_tag_similarity(mem.get("emotion_tag"), emotion_tag)
                if sim > 0:
                    mem["_score"] *= (1.0 + emotion_tag_weight * sim)
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
                                min_weight: float = None,
                                emotion_tag: dict = None,
                                emotion_tag_weight: float = 0.0) -> list[dict]:
        """search() 的包装，结果按遗忘权重重排序（新/重要记忆在前）。

        B2: emotion_tag + emotion_tag_weight 透传给 _apply_forgetting（情绪相近加权）。
        """
        results = self.search(query, limit=limit * 3, category=category,
                              min_importance=min_importance)
        if not results:
            return []
        if now is None:
            now = datetime.now(CST)
        out = self._apply_forgetting(results, now, strength, min_weight, limit=limit,
                                     emotion_tag=emotion_tag,
                                     emotion_tag_weight=emotion_tag_weight)
        # C2: 召回即强化（[memory].reinforce_enabled 开启时记录 recall_count）
        self.note_recalled([m.get("id") for m in out])
        return out

    def user_relevant_with_forgetting(self, limit: int = 20,
                                       min_importance: float = 0.3,
                                       prefer_categories: list[str] = None,
                                       now: datetime = None,
                                       strength: float = None,
                                       min_weight: float = None,
                                       emotion_tag: dict = None,
                                       emotion_tag_weight: float = 0.0) -> list[dict]:
        """user_relevant() 的包装，结果按遗忘权重重排序（偏好类别 ×1.2）。

        B2: emotion_tag + emotion_tag_weight 透传给 _apply_forgetting。
        """
        results = self.user_relevant(limit=limit * 3, min_importance=min_importance,
                                      prefer_categories=prefer_categories)
        if not results:
            return []
        if now is None:
            now = datetime.now(CST)
        return self._apply_forgetting(results, now, strength, min_weight,
                                      prefer_categories=prefer_categories, limit=limit,
                                      emotion_tag=emotion_tag,
                                      emotion_tag_weight=emotion_tag_weight)

    def random_memory_with_forgetting(self, category: str = None,
                                       min_importance: float = 0.5,
                                       prefer_categories: list[str] = None,
                                       now: datetime = None,
                                       strength: float = None,
                                       min_weight: float = None,
                                       emotion_tag: dict = None,
                                       emotion_tag_weight: float = 0.0) -> dict | None:
        """random_memory() 的包装，用遗忘权重加权随机（新记忆更可能被选中）。

        B2: emotion_tag + emotion_tag_weight 透传给 user_relevant_with_forgetting。
        """
        if prefer_categories is None:
            prefer_categories = ["preferences", "entities", "events", "profile"]

        relevant = self.user_relevant_with_forgetting(
            limit=50, min_importance=min_importance,
            prefer_categories=prefer_categories,
            now=now, strength=strength, min_weight=min_weight,
            emotion_tag=emotion_tag, emotion_tag_weight=emotion_tag_weight,
        )
        if not relevant:
            return None

        if now is None:
            now = datetime.now(CST)
        weights = []
        for m in relevant:
            ebw = self.ebbinghaus_weight(m, now, strength, min_weight)
            # C2: importance 走 _effective_importance（reinforce 开启时被召回记忆更强）
            w = self._effective_importance(m) ** 2 * ebw
            weights.append(w)

        total = sum(weights)
        if total <= 0:
            picked = random.choice(relevant)
        else:
            picked = random.choices(relevant, weights=weights, k=1)[0]
        # C2: 召回即强化（[memory].reinforce_enabled 开启时记录 recall_count）
        self.note_recalled([picked.get("id")])
        return picked
