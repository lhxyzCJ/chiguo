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
import os
import random
import time as _time_module

from memory.base import MemoryBackend

# mem0 遥测默认关闭（posthog 后台上报，对自部署无意义）
os.environ.setdefault("MEM0_TELEMETRY", "false")

_RETRY_SECONDS = 60.0  # available=False 后至少间隔这么久才重新探测

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
                 max_rows: int = None, **kwargs):
        self.user_id = str(user_id or "chiguo")
        self.collection_name = str(collection_name or "chiguo")
        self.qdrant_path = qdrant_path or _DEFAULT_QDRANT_PATH
        self.history_db = history_db or _DEFAULT_HISTORY_DB
        self.llm_model = llm_model or _DEFAULT_LLM_MODEL
        self.llm_base_url = llm_base_url or _DEFAULT_LLM_BASE_URL
        self.llm_api_key = llm_api_key or _pi_api_key()
        self.embedder_model = embedder_model or _DEFAULT_EMBEDDER_MODEL
        self.embedder_base_url = embedder_base_url or _DEFAULT_EMBEDDER_BASE_URL
        self.embedder_dims = int(embedder_dims or _DEFAULT_EMBEDDER_DIMS)
        self._strength = strength or 168.0
        self._min_weight = min_weight or 0.1
        self.max_rows = int(max_rows or 1000)  # _all_rows get_all 的 top_k 上限
        self._m = None  # mem0 Memory 实例（惰性初始化）
        self._available: bool | None = None
        self._last_probe: float = 0.0
        self._last_error: tuple | None = None  # (ts, op, error_str)，暴露到 stats()

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
            self._m.search("probe", filters={"user_id": self.user_id}, top_k=1)
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
        }

    # ── 原语 ──────────────────────────────────────────────

    def search(self, query: str, limit: int = 10,
               category: str = None,
               min_importance: float = 0.3) -> list[dict]:
        """mem0 语义检索（向量 + BM25 融合，零 LLM）。不可用返回空列表。"""
        if not self.available:
            return []
        try:
            filters = {"user_id": self.user_id}
            if category:
                filters["category"] = category  # mem0 原生支持 metadata 键过滤
            results = self._m.search(
                query, filters=filters, top_k=max(limit, 20)
            ).get("results", [])
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
        try:
            total = len(self._all_rows(min_importance=0.0))
        except Exception:
            total = 0
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


def _parse_iso_ts(s: str) -> float:
    """ISO 时间戳 → epoch 秒（UTC→本地展示由 normalize_ts 处理）。"""
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
