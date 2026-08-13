# ============================================================
# memory/mem0_backend.py — mem0 AI 记忆后端（读写双向）
#
# 基于 mem0ai Python 库（https://github.com/mem0ai/mem0）：
#   LLM       OpenAI 兼容端点（默认 opencode.ai/zen/go/v1 + deepseek-v4-flash，
#             key 缺省读 ~/.pi/agent/auth.json 的 opencode-go）
#   Embedder  本地 ollama（默认 qwen3-embedding:0.6b，1024 维，零成本）
#   Vector    本地 qdrant 嵌入式（on_disk 持久化，无需 docker）
#   History   SQLite（mem0 操作历史）
#
# 读侧原语（search/random_memory/stats）零 LLM 调用（纯向量检索）；
# 写侧 add_messages() 由 mem0 用 LLM 提取事实（daemon 对话后自动调用）。
# mem0 不可用（库缺失/无 key/ollama 未启动）→ available=False 优雅降级，
# 60s 节流重试自愈（与旧 LanceDbBackend 同款策略）。
# ============================================================

import json
import math
import os
import random
import sys
import threading
import time as _time_module
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

from memory.base import (
    DEFAULT_CONSOLIDATE_MAX_AGE_HOURS,
    DEFAULT_CONSOLIDATE_MIN_IMPORTANCE,
    DEFAULT_CONSOLIDATE_SIM_THRESHOLD,
    DEFAULT_REINFORCE_BONUS,
    MemoryBackend,
)

# mem0 遥测默认关闭（posthog 后台上报，对自部署无意义）
os.environ.setdefault("MEM0_TELEMETRY", "false")

_RETRY_SECONDS = 60.0  # available=False 后至少间隔这么久才重新探测

# M4: mem0 读侧单次调用超时预算(秒)。mem0 2.x 的 OllamaEmbedding 硬编码
# Client(host=...) 无超时(ollama-python timeout=None → httpx 无限阻塞),嵌入 HTTP
# 是 daemon evaluate 锁内唯一无上限的网络 IO——持锁时长无上限会让并发进程 5s
# 拿不到锁降级无锁、该轮 save 被拒写白跑。此处给 search 原语(含 available 探测)
# 加线程超时兜底,超时按失败降级(置不可用 + 60s 节流重探自愈)。
_MEM0_TIMEOUT = 10.0


def _call_with_timeout(fn, timeout: float):
    """在 daemon 线程执行 fn;超时返回 None(调用方按失败降级),异常原样重抛。

    不侵入 mem0 内部:仅把我们这一侧的 `self._m.*` 调用圈进超时预算。超时后
    放弃等待,遗留线程会在底层请求返回后自然结束(ollama 恢复即自愈);故障驱动
    自愈(_available=False + _RETRY_SECONDS 节流)保证 60s 内最多遗留一个线程。
    """
    box = {}

    def runner():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001 —— 跨线程重抛,保持调用方异常语义
            box["e"] = e

    t = threading.Thread(target=runner, daemon=True, name="mem0-timeout")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None
    if "e" in box:
        raise box["e"]
    return box["v"]

_DEFAULT_LLM_MODEL = "deepseek-v4-flash"
_DEFAULT_LLM_BASE_URL = "https://opencode.ai/zen/go/v1"
_DEFAULT_EMBEDDER_MODEL = "qwen3-embedding:0.6b"
_DEFAULT_EMBEDDER_BASE_URL = "http://localhost:11434"
_DEFAULT_EMBEDDER_DIMS = 1024
_DEFAULT_QDRANT_PATH = "data/mem0/qdrant"
_DEFAULT_HISTORY_DB = "data/mem0/history.db"


def _pi_api_key(provider: str = "opencode-go") -> str | None:
    """从 ~/.pi/agent/auth.json 读 pi provider 的 API key；失败返回 None。"""
    try:
        with open(os.path.expanduser("~/.pi/agent/auth.json"), encoding="utf-8") as f:
            return (json.load(f).get(provider) or {}).get("key") or None
    except Exception:
        return None


def _finite_float(value, default: float) -> float:
    """配置数值解析：非数值 / NaN / inf / 负数一律回退默认。

    toml 手改事故（字符串阈值、NaN）会让 consolidate_plan 内 `sim >= sim_threshold`
    直接 TypeError 或静默禁掉去重；统一在这里兜底，CLI 与 idle 双入口共享。
    """
    try:
        fv = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(fv) or fv < 0:
        return default
    return fv


class Mem0Backend(MemoryBackend):
    """mem0 AI 记忆后端：语义检索 + LLM 事实提取写入。

    行契约（search/random_memory 返回的 dict）与 memory/base.py 的约定一致：
    id/text/category/scope/importance/timestamp/datetime/
    memory_category/l0_abstract/l2_content/tier/source。
    """

    def __init__(self, user_id: str = "chiguo",
                 collection_name: str = "chiguo",
                 qdrant_path: str = None, history_db: str = None,
                 llm_model: str = None, llm_base_url: str = None,
                 llm_api_key: str = None,
                 embedder_model: str = None, embedder_base_url: str = None,
                 embedder_dims: int = None,
                 strength: float = None, min_weight: float = None,
                 max_rows: int = None,
                 consolidate_sim_threshold: float = None,
                 consolidate_min_importance: float = None,
                 consolidate_max_age_hours: float = None,
                 reinforce_enabled: bool = False,
                 reinforce_bonus: float = None,
                 **kwargs):
        self.user_id = str(user_id or "chiguo")
        self.collection_name = str(collection_name or "chiguo")
        self.qdrant_path = qdrant_path or _DEFAULT_QDRANT_PATH
        self.history_db = history_db or _DEFAULT_HISTORY_DB
        self.llm_model = llm_model or _DEFAULT_LLM_MODEL
        self.llm_base_url = llm_base_url or _DEFAULT_LLM_BASE_URL
        self.llm_api_key = llm_api_key or _pi_api_key()
        self.embedder_model = embedder_model or _DEFAULT_EMBEDDER_MODEL
        self.embedder_base_url = embedder_base_url or _DEFAULT_EMBEDDER_BASE_URL
        self.embedder_dims = int(_finite_float(embedder_dims or _DEFAULT_EMBEDDER_DIMS,
                                               _DEFAULT_EMBEDDER_DIMS))
        self._strength = strength or 168.0
        self._min_weight = min_weight or 0.1
        self.max_rows = int(_finite_float(max_rows or 1000, 1000))  # _all_rows get_all 的 top_k 上限
        # ── C1/C2: 记忆巩固 & 复习强化配置（默认关闭恒等，可灰度）──
        self._consolidate_sim_threshold = (
            consolidate_sim_threshold
            if consolidate_sim_threshold is not None
            else DEFAULT_CONSOLIDATE_SIM_THRESHOLD)
        self._consolidate_min_importance = (
            consolidate_min_importance
            if consolidate_min_importance is not None
            else DEFAULT_CONSOLIDATE_MIN_IMPORTANCE)
        self._consolidate_max_age_hours = (
            consolidate_max_age_hours
            if consolidate_max_age_hours is not None
            else DEFAULT_CONSOLIDATE_MAX_AGE_HOURS)
        self._reinforce_enabled = bool(reinforce_enabled)
        self._reinforce_bonus = (float(reinforce_bonus)
                                 if reinforce_bonus is not None
                                 else DEFAULT_REINFORCE_BONUS)
        self._recall_counts: dict[str, int] = {}
        self._m = None  # mem0 Memory 实例（惰性初始化）
        self._available: bool | None = None
        self._last_probe: float = 0.0
        self._last_error: tuple | None = None  # (ts, op, error_str)，暴露到 stats()
        self._capability_warned = False  # D2: 能力缺失告警只打一次，不重复刷屏

    # ── mem0 初始化 ───────────────────────────────────────

    def _mem0_config(self) -> dict | None:
        """mem0 MemoryConfig dict；无 API key 返回 None（不可用）。"""
        if not self.llm_api_key:
            return None
        return {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self.llm_model,
                    "api_key": self.llm_api_key,
                    "openai_base_url": self.llm_base_url,
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": self.embedder_model,
                    "embedding_dims": self.embedder_dims,
                    "ollama_base_url": self.embedder_base_url,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": self.collection_name,
                    "path": self.qdrant_path,
                    "embedding_model_dims": self.embedder_dims,
                    "on_disk": True,
                },
            },
            "history_db_path": self.history_db,
        }

    def _ensure_mem0(self):
        """惰性构造 mem0 Memory；失败抛异常（由 available 捕获）。"""
        if self._m is None:
            from mem0 import Memory  # 惰性导入：mem0ai 缺失时在此抛 ImportError
            self._m = Memory.from_config(self._mem0_config())

    @property
    def available(self) -> bool:
        """mem0 是否可用。不可用时所有查询返回空列表。

        探测结果（无论 True/False）只缓存 _RETRY_SECONDS 秒，超时即重探——
        长驻/定时场景下 mem0 运行期故障（qdrant 满、ollama 挂、key 失效）恢复后可自愈。
        测试隔离：CHIGUO_MEM0_DISABLED=1 时恒不可用（确定性，不碰真实库）。
        """
        if os.environ.get("CHIGUO_MEM0_DISABLED") == "1":
            return False
        now = _time_module.time()
        if self._available is not None and now - self._last_probe < _RETRY_SECONDS:
            return self._available
        try:
            if not self._mem0_config():
                raise RuntimeError("mem0: 无 LLM API key（~/.pi/agent/auth.json opencode-go 或配置 mem0_llm_api_key）")
            self._ensure_mem0()
            # 连通性探测：search 一次（走 embedder，覆盖 ollama 依赖；
            # get_all 只走 qdrant scroll 不触 embedder，ollama 故障探测不到）
            if _call_with_timeout(
                lambda: self._m.search("probe", filters={"user_id": self.user_id}, top_k=1),
                _MEM0_TIMEOUT) is None:
                raise TimeoutError("mem0 探测超时")
            self._warn_missing_capabilities()  # D2: 探测成功时检查一次能力缺失，显式告警
            self._available = True
        except Exception as e:
            import logging
            logging.warning("mem0 %s failed: %r", "available", e)
            self._last_error = (_time_module.time(), "available", str(e))
            self._available = False
        self._last_probe = _time_module.time()
        return self._available

    # ── mem0 result → chiguo 行契约 ───────────────────────

    def _row(self, r: dict) -> dict:
        meta = r.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        text = str(r.get("memory") or r.get("text") or "")
        ts = 0.0
        created = r.get("created_at") or ""
        if not isinstance(created, str):
            created = str(created)  # 非字符串(数字等)→ 转串走解析,非法落 0.0,防 .replace AttributeError(R14)
        try:
            ts = _parse_iso_ts(created)
        except (TypeError, ValueError, AttributeError):
            ts = 0.0
        importance = self.clean_importance(meta) or 0.5
        if importance <= 0:
            importance = 0.5  # mem0 无 importance 概念；缺省中位权重，避免全零
        rc = meta.get("recall_count")
        try:
            recall_count = int(rc)
        except (TypeError, ValueError):
            recall_count = 0
        return {
            "id": str(r.get("id") or ""),
            "text": text,
            "category": str(meta.get("category") or ""),
            "scope": str(meta.get("scope") or "global"),
            "importance": importance,
            "timestamp": ts,
            "datetime": created,
            "memory_category": str(meta.get("memory_category") or "?"),
            "l0_abstract": str(meta.get("l0_abstract") or ""),
            "l2_content": str(meta.get("l2_content") or ""),
            "tier": str(meta.get("tier") or "working"),
            "source": str(meta.get("source") or "mem0"),
            # B2: 情绪标签（写侧 emotion_tagging 打标；读侧 _apply_forgetting 按相近加权）
            "emotion_tag": meta.get("emotion_tag") if isinstance(meta.get("emotion_tag"), dict) else None,
            # C2: 召回次数回读（_persist_recall 写进 metadata；跨进程 cron 部署下
            # _recall_counts 每次从空开始，读行内持久化值强化才不失效——审查 #2）
            "recall_count": recall_count,
            # C1: 巩固去重标记回读（consolidate 写回 metadata.consolidated_with；
            # consolidate_plan 据此跳过二次降权——审查 #159）
            "consolidated_with": str(meta.get("consolidated_with") or ""),
        }

    # ── 原语 ──────────────────────────────────────────────

    def search(self, query: str, limit: int = 10,
               category: str = None,
               min_importance: float = 0.3) -> list[dict]:
        """mem0 语义检索（向量 + BM25 融合，零 LLM）。不可用返回空列表。"""
        if not self.available:
            return []
        limit = int(_finite_float(limit, 10))
        min_importance = _finite_float(min_importance, 0.3)
        try:
            filters = {"user_id": self.user_id}
            if category:
                filters["category"] = category  # mem0 原生支持 metadata 键过滤
            r = _call_with_timeout(
                lambda: self._m.search(query, filters=filters, top_k=max(limit, 20)),
                _MEM0_TIMEOUT)
            if r is None:
                raise TimeoutError("mem0 search 超时")
            results = r.get("results", [])
        except Exception as e:
            import logging
            logging.warning("mem0 %s failed: %r", "search", e)
            self._last_error = (_time_module.time(), "search", str(e))
            # 故障驱动自愈：置不可用并刷新探测节流，60s 后重探
            self._available = False
            self._last_probe = _time_module.time()
            return []
        out = []
        for r in results:
            try:
                row = self._row(r)
            except (TypeError, ValueError, AttributeError):
                continue  # 单条脏形状不拖垮整次检索(R14 形状防御)
            if row["importance"] < min_importance:
                continue
            out.append(row)
        return out[:limit]

    def random_memory(self, category: str = None,
                      min_importance: float = 0.5,
                      prefer_categories: list[str] = None) -> dict | None:
        """从全量记忆里按重要性² 加权随机返回一条。"""
        rows = self._all_rows(min_importance=min_importance)
        if not rows:
            return None
        if category:
            rows = [m for m in rows if m["category"] == category]
            if not rows:
                return None
        if prefer_categories:
            def _sort_key(m):
                return 0 if m.get("memory_category") in prefer_categories else 1
            rows.sort(key=_sort_key)
        weights = [m.get("importance", 0.5) ** 2 for m in rows]
        total = sum(weights)
        if total <= 0:
            return random.choice(rows)
        return random.choices(rows, weights=weights, k=1)[0]

    def stats(self) -> dict:
        if not self.available:
            return {
                "available": False,
                "total_memories": 0,
                "user_relevant_count": 0,
                "db_path": self.qdrant_path,
                "backend": "mem0",
                "last_error": self._last_error,
            }
        total = self._count_rows()
        return {
            "available": True,
            "total_memories": total,
            "user_relevant_count": len(self.user_relevant(limit=100)),
            "db_path": self.qdrant_path,
            "backend": "mem0",
            "last_error": self._last_error,
        }

    # ── 写入 ──────────────────────────────────────────────

    def add_messages(self, messages: list[dict] | str,
                     metadata: dict = None) -> bool:
        """对话后写入 mem0（infer=True，LLM 提取事实）。

        messages: [{"role": "user"/"assistant", "content": str}, ...] 或原始文本。
        失败静默返回 False（不影响主流程）。
        """
        if not self.available:
            return False
        try:
            self._m.add(messages, user_id=self.user_id, metadata=metadata)
            return True
        except Exception as e:
            import logging
            logging.warning("mem0 %s failed: %r", "add", e)
            self._last_error = (_time_module.time(), "add", str(e))
            # 故障驱动自愈：置不可用并刷新探测节流，60s 后重探
            self._available = False
            self._last_probe = _time_module.time()
            return False

    # ── C1: 确定性记忆巩固（无 LLM 的 Letta dreaming 版；config 门控）──

    def consolidate(self, now: datetime = None,
                    sim_threshold: float = None,
                    min_importance: float = None,
                    max_age_hours: float = None,
                    dry_run: bool = False,
                    limit: int = None) -> dict:
        """扫描全量记忆做确定性合并/降权/过期（零 LLM 调用，不换库）。

        - 去重：text jaccard_3gram ≥ sim_threshold 的近似重复对 → 保留一条、
          另一条 importance 减半 + `_consolidated` 标记。
        - 过期：importance < min_importance 且年龄 > max_age_hours → 删除。
        写回：
        - 降权经 mem0 `update_memory` 写 metadata.importance（有该能力时；
          mem0 无该 API → 静默跳过，仅报告）。
        - 过期经 mem0 `delete` 删除（有该能力时；否则仅报告）。
        dry_run=True → 只出计划不写库（配置检查/灰度用）。
        不可用 → 空报告（不抛）。返回报告 dict。
        """
        if not self.available:
            return {"available": False, "ok": False, "error": "backend unavailable",
                    "demoted": [], "expired": [], "kept": [],
                    "demoted_ids": [], "expired_ids": []}
        if now is None:
            now = datetime.now(CST)
        rows = self._all_rows(min_importance=0.0, top_k=limit)
        # 阈值有限性强制：配置可能是字符串/NaN/inf（toml 手改事故），统一兜底防
        # consolidate_plan 内 sim >= sim_threshold 直接 TypeError（cli/_maybe_consolidate 双入口都走这）。
        sim_threshold = _finite_float(
            sim_threshold if sim_threshold is not None else self._consolidate_sim_threshold,
            DEFAULT_CONSOLIDATE_SIM_THRESHOLD)
        min_importance = _finite_float(
            min_importance if min_importance is not None else self._consolidate_min_importance,
            DEFAULT_CONSOLIDATE_MIN_IMPORTANCE)
        max_age_hours = _finite_float(
            max_age_hours if max_age_hours is not None else self._consolidate_max_age_hours,
            DEFAULT_CONSOLIDATE_MAX_AGE_HOURS)
        plan = self.consolidate_plan(
            rows, now, sim_threshold=sim_threshold,
            min_importance=min_importance, max_age_hours=max_age_hours,
        )
        if not dry_run:
            for r in plan["demoted"]:
                try:
                    upd = getattr(self._m, "update", None)
                    if upd is None:
                        continue  # mem0 无 update API → 仅报告降权计划
                    upd(r["id"], metadata={"importance": r["importance"],
                                           "consolidated_with": r.get("consolidated_with")})
                except Exception as e:
                    import logging
                    logging.warning("mem0 consolidate demote failed: %r", e)
            for r in plan["expired"]:
                try:
                    dele = getattr(self._m, "delete", None)
                    if dele is None:
                        continue  # mem0 无 delete API → 仅报告过期计划
                    dele(r["id"])
                except Exception as e:
                    import logging
                    logging.warning("mem0 consolidate expire failed: %r", e)
        return {
            "available": True,
            "ok": True,
            "dry_run": bool(dry_run),
            "scanned": len(rows),
            "demoted": plan["demoted"],
            "expired": plan["expired"],
            "kept": plan["kept"],
            "demoted_ids": [r.get("id") for r in plan["demoted"]],
            "expired_ids": [r.get("id") for r in plan["expired"]],
        }

    # ── C2: 复习强化写回（_persist_recall 钩子覆写）──

    def _persist_recall(self, memory_id: str, count: int):
        """召回次数持久化：mem0 有 update 时写 metadata.recall_count；
        无该 API → no-op（仅内存侧 recall_count）。update 的 metadata 为 merge 语义，
        不会覆盖既有 category/scope/emotion_tag。"""
        upd = getattr(self._m, "update", None)
        if upd is None:
            return
        upd(memory_id, metadata={"recall_count": count})

    def _load_recall_count(self, memory_id: str) -> int:
        """读回 mem0 已持久化的 recall_count（A2 跨进程累积数据源）。

        cron 每 15 分钟新进程，base.note_recalled 计数基数必须来自持久化旧值而非
        进程内空 dict。mem0 有 get(memory_id)（返回含 metadata 的记录）时读 metadata
        .recall_count；无该 API / 读取失败 / 非法值 → 0（退化为进程内计数，
        写侧只 +1 不覆盖旧值，跨进程累积不失效）。
        """
        getter = getattr(self._m, "get", None)
        if getter is None:
            return 0
        try:
            rec = getter(memory_id)
        except Exception as e:
            import logging
            logging.warning("mem0 %s failed: %r", "get_recall_count", e)
            return 0
        meta = (rec or {}).get("metadata") or {}
        if not isinstance(meta, dict):
            return 0
        try:
            return int(meta.get("recall_count") or 0)
        except (TypeError, ValueError):
            return 0

    # ── 内部 ──────────────────────────────────────────────

    def _all_rows(self, min_importance: float = 0.0,
                  top_k: int = None) -> list[dict]:
        """全量记忆（get_all）→ 行 dict 列表（importance 过滤）。"""
        if not self.available:
            return []
        try:
            results = self._m.get_all(
                filters={"user_id": self.user_id},
                top_k=self.max_rows if top_k is None else top_k,
            ).get("results", [])
        except Exception as e:
            import logging
            logging.warning("mem0 %s failed: %r", "get_all", e)
            self._last_error = (_time_module.time(), "get_all", str(e))
            # 故障驱动自愈：置不可用并刷新探测节流，60s 后重探
            self._available = False
            self._last_probe = _time_module.time()
            return []
        out = []
        for r in results:
            try:
                row = self._row(r)
            except (TypeError, ValueError, AttributeError):
                continue  # 单条脏形状不拖垮整次遍历(R14 形状防御)
            if row["importance"] < min_importance:
                continue
            out.append(row)
        return out

    def _count_rows(self) -> int:
        """记忆总数：优先 qdrant get_collection 精确计数（零向量拉取，O(1)），
        mem0/qdrant 版本差异或假后端无 vector_store.col_info → 回退全量 scroll 计数。"""
        try:
            store = getattr(self._m, "vector_store", None)
            if store is None:
                raise AttributeError("no vector_store")
            info = store.col_info()  # qdrant get_collection → CollectionInfo(points_count)
            n = int(getattr(info, "points_count", -1))
            if n >= 0:
                return n
        except Exception:
            pass
        return len(self._all_rows(min_importance=0.0))

    def _warn_missing_capabilities(self):
        """mem0 升级后 update/delete/get API 可能改名/删除 → getattr 返回 None → 巩固降权/
        召回计数静默跳过。探测时检查一次，缺失即显式 stderr 告警（不重复刷屏）。"""
        if self._capability_warned:
            return
        missing = [name for name in ("update", "delete", "get")
                   if getattr(self._m, name, None) is None]
        if missing:
            self._capability_warned = True
            print(f"[warn] mem0 缺 {', '.join(missing)} API → 巩固降权/召回计数将静默跳过；"
                  f"升级 mem0ai 后需回归（memory/mem0_backend.py）", file=sys.stderr)


def _parse_iso_ts(s: str) -> float:
    """ISO 时间戳 → epoch 秒。"""
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
