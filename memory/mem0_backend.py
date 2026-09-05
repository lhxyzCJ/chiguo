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
import sys
import time as _time_module
from datetime import datetime

from chiguo_concurrent import TIMEOUT, call_with_timeout
from chiguo_math import cfg_float  # #406(b)：_finite_float 收敛至 chiguo_math 单源
from chiguo_time import CST  # Q22: 共享时区常量

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

# Task 7A: add_messages 写路径超时预算(秒)。写路径运行在 state_lock 外
# (see ops/engine_ops.py _mem0_autowrite)，LLM 事实提取(推断)合法比读路径
# 慢，故预算宽于 _MEM0_TIMEOUT。单次超时仅记 fault 不翻转 _available
# （读写故障隔离）；连续超时达 _MEM0_ADD_TIMEOUT_BREAKER 次熔断翻转（#419）。
_MEM0_ADD_TIMEOUT = 30.0

# #419: add 连续超时熔断阈值（次）。LLM 端点永久不可达时每次写仍阻塞
# _MEM0_ADD_TIMEOUT 无上限；连续超时达阈值翻 _available=False，走现有 60s
# 节流自愈（联动 #402 减少遗留线程产生速率）。阈值常量起步，不加 toml 配置；
# 熔断触发后可通过 mem0_write 快照链路观察（#417）。
_MEM0_ADD_TIMEOUT_BREAKER = 3

# Task 6B: ollama 快速探测超时(秒)。构造 mem0 Memory 前预检 ollama 可达性，
# 避免昂贵的 Memory() 构造因 ollama 不可达挂起。
_OLLAMA_PROBE_TIMEOUT = 3.0

# 超时语义（#397 收敛至 chiguo_concurrent 单源）：
# TIMEOUT 专用哨兵替代 None，避免与被包装函数的合法 None 返回值碰撞
# （#414-11：_persist_recall 等 mem0 update/get 返回 None）。
# 不侵入 mem0 内部:仅把我们这一侧的 `self._m.*` 调用圈进超时预算。超时后
# 放弃等待,遗留线程会在底层请求返回后自然结束(ollama 恢复即自愈);故障驱动
# 自愈(_available=False + _RETRY_SECONDS 节流)保证 60s 内最多遗留一个线程。


def _probe_ollama_tags(base_url: str, timeout: float) -> None:
    """GET {base_url}/api/tags 探测 ollama 可达性；任何异常向上传播。

    localhost 走直连 bypass 系统代理（防 MUSIC_U 随代理外泄），逻辑收敛
    chiguo_net.build_no_proxy_opener/is_local_host。
    """
    import urllib.request
    from urllib.parse import urlparse
    url = base_url.rstrip("/") + "/api/tags"
    req = urllib.request.Request(url, headers={"User-Agent": "chiguo/1.0"})
    try:
        from chiguo_net import build_no_proxy_opener, is_local_host
        host = urlparse(url).hostname or ""
        opener = build_no_proxy_opener() if is_local_host(host) else urllib.request.build_opener()
    except ImportError:  # chiguo_net 不可用 → 回退 plain urlopen
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        resp.read(1)


_DEFAULT_LLM_MODEL = "deepseek-v4-flash"
_DEFAULT_LLM_BASE_URL = "https://opencode.ai/zen/go/v1"
_DEFAULT_EMBEDDER_MODEL = "qwen3-embedding:0.6b"
_DEFAULT_EMBEDDER_BASE_URL = "http://localhost:11434"
_DEFAULT_EMBEDDER_DIMS = 1024
_DEFAULT_QDRANT_PATH = "data/mem0/qdrant"
_DEFAULT_HISTORY_DB = "data/mem0/history.db"


def _pi_api_key(provider: str = "opencode-go") -> str | None:
    """从 ~/.pi/agent/auth.json 读 pi provider 的 API key；失败返回 None。"""
    from chiguo_auth import pi_api_key as _auth_pi_key

    return _auth_pi_key(provider)


def _finite_float(value, default: float) -> float:
    """配置数值解析：非数值 / NaN / inf / 负数一律回退默认。

    toml 手改事故（字符串阈值、NaN）会让 consolidate_plan 内 `sim >= sim_threshold`
    直接 TypeError 或静默禁掉去重；统一在这里兜底，CLI 与 idle 双入口共享。

    #406(b)：收敛至 chiguo_math.cfg_float 单源。负值先行回退默认（cfg_float
    的 clamp_min=0.0 会把负值钳为 0.0，而旧语义要求负输入 → 回退默认）；
    余下非数值 / NaN / inf → 回退默认（cfg_float 语义与旧实现一致）。
    """
    try:
        if float(value) < 0:
            return default
    except (TypeError, ValueError, OverflowError):
        return default
    return cfg_float(value, default, clamp_min=0.0)


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
        # F-RT-017 (#336): 写链故障可感知。available 探测只覆盖读链（embedder+qdrant），
        # 不触发 LLM 事实提取写链；_add_fail_count 累计 add_messages 失败次数，暴露进
        # stats() 供 monitor 感知写链故障（写失败本身已会翻转 _available + 记 _last_error）。
        self._add_fail_count = 0
        self._add_timeout_streak = 0  # #419: add 连续超时计数（成功清零，熔断达阈值翻 _available）
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
            if self._m is None:
                # Task 6B: 构造前先快检 ollama 可达性，失败直接进 except
                # （翻 _available=False + 60s 节流），跳过昂贵构造。
                try:
                    _probe_ollama_tags(self.embedder_base_url, _OLLAMA_PROBE_TIMEOUT)
                except Exception as e:
                    raise RuntimeError(
                        f"mem0: embedder 不可达（{self.embedder_base_url}）: {e}") from e
            self._ensure_mem0()
            # 连通性探测：search 一次（走 embedder，覆盖 ollama 依赖；
            # get_all 只走 qdrant scroll 不触 embedder，ollama 故障探测不到）
            if call_with_timeout(
                lambda: self._m.search("probe", filters={"user_id": self.user_id}, top_k=1),
                _MEM0_TIMEOUT, name="mem0-timeout") is TIMEOUT:
                raise TimeoutError("mem0 探测超时")
            self._warn_missing_capabilities()  # D2: 探测成功时检查一次能力缺失，显式告警
            self._available = True
        except Exception as e:  # noqa: BLE001 —— mem0 外部异常类型不稳定，降级+自愈
            import logging
            logging.warning("mem0 %s failed: %r", "available", e)
            self._last_error = (_time_module.time(), "available", str(e))
            self._available = False
        self._last_probe = _time_module.time()
        return self._available

    @property
    def add_fail_count(self) -> int:
        """F-RT-017: LLM 写链（add_messages 事实提取）累计失败次数。

        available 探测只覆盖读链（embedder+qdrant），LLM 提取端点（opencode/model）
        故障写失败不会在 available 上直接暴露；此处提供写链故障计数，供 monitor/health
        读取。写成功清零调用方自行比对（累计语义，不自动复位）。
        """
        return self._add_fail_count

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
        # F-A21-001 (#336): 区分"显式 importance"与"无 importance 信息"。
        # mem0 无 importance 概念，读路径缺省回退 0.5 保持读侧稳定（search/随机加权
        # 不受影响）；但 consolidate 过期需能识别"无 importance metadata"的行——
        # 否则 _row 固定回退 0.5 ≥ min_importance(0.3) 使超龄行永不过期，记忆库无界
        # 增长。importance_known=False 即"元数据无有效 importance"的信号，consolidate_plan
        # 据此对超龄行单独放行过期。仅显式 >0 的数值 importance 才视为"有真实 importance
        # 信息"（known=True）；缺 metadata / 显式 0 / NaN 一律 known=False —— 缺省 0.5 是
        # 读路径回退、不代表真实重要性，超龄同样应可过期（避免这类行永不过期）。
        raw_imp = self.clean_importance(meta)
        importance_known = raw_imp > 0  # 显式 >0 才视为有真实 importance 信息
        importance = raw_imp or 0.5
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
            # F-A21-001: 有无真实 importance 信息（False = metadata 无有效 importance，
            # _row 回退 0.5；供 consolidate_plan 对超龄无标记行放行过期）
            "importance_known": importance_known,
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
            r = call_with_timeout(
                lambda: self._m.search(query, filters=filters, top_k=max(limit, 20)),
                _MEM0_TIMEOUT, name="mem0-timeout")
            if r is TIMEOUT:
                raise TimeoutError("mem0 search 超时")
            results = r.get("results", [])
        except Exception as e:  # noqa: BLE001 —— mem0 外部异常类型不稳定，降级+自愈
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
        # M-5 (#229): 结构化暴露 update/delete/get 能力可用性（对齐 _warn_missing_capabilities 探测）
        def _caps():
            if getattr(self, "_m", None) is None:
                return {"update": False, "delete": False, "get": False}
            return {n: getattr(self._m, n, None) is not None
                    for n in ("update", "delete", "get")}
        if not self.available:
            return {
                "available": False,
                "total_memories": 0,
                "user_relevant_count": 0,
                "db_path": self.qdrant_path,
                "backend": "mem0",
                "capabilities": {"update": False, "delete": False, "get": False},
                "add_fail_count": self._add_fail_count,  # F-RT-017: 写链失败计数
                "last_error": self._last_error,
            }
        total = self._count_rows()
        return {
            "available": True,
            "total_memories": total,
            "user_relevant_count": len(self.user_relevant(limit=100)),
            "db_path": self.qdrant_path,
            "backend": "mem0",
            "capabilities": _caps(),
            "add_fail_count": self._add_fail_count,  # F-RT-017: 写链失败计数
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
            # Task 7A: 写路径 LLM 提取慢，用加长预算；超时仅记 fault，
            # 不翻 _available（读写故障隔离：读可用不被写拖垮）。
            r = call_with_timeout(
                lambda: self._m.add(messages, user_id=self.user_id, metadata=metadata),
                _MEM0_ADD_TIMEOUT, name="mem0-timeout")
            if r is TIMEOUT:
                raise TimeoutError("mem0 add 超时")
            self._add_timeout_streak = 0  # #419: 成功清零
            return True
        except TimeoutError as e:  # TimeoutError 先于 Exception（其子类）
            import logging
            logging.warning("mem0 %s failed: %r", "add_timeout", e)
            self._last_error = (_time_module.time(), "add_timeout", str(e))
            self._add_fail_count += 1
            # #419: 连续超时熔断——达阈值翻 _available=False，走现有 60s 节流自愈
            # （60s 窗口内不重试，联动 #402 减少遗留线程产生速率）
            self._add_timeout_streak += 1
            if self._add_timeout_streak >= _MEM0_ADD_TIMEOUT_BREAKER:
                self._available = False
                self._last_probe = _time_module.time()
            return False
        except Exception as e:  # noqa: BLE001 —— mem0 外部异常类型不稳定，降级+自愈
            import logging
            logging.warning("mem0 %s failed: %r", "add", e)
            self._last_error = (_time_module.time(), "add", str(e))
            self._add_fail_count += 1  # F-RT-017: 写链失败计数（stats() 暴露供 monitor）
            self._add_timeout_streak = 0  # #419: 写异常已有翻转语义，不并入连续超时计数
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
                    # Task 8: 逐条超时预算；超时仅告警跳过（report-only，
                    # 绝不让单条挂起拖垮整个 consolidate 计划）。
                    rr = call_with_timeout(
                        lambda r=r, upd=upd: upd(
                            r["id"], metadata={"importance": r["importance"],
                                               "consolidated_with": r.get("consolidated_with")}),
                        _MEM0_TIMEOUT, name="mem0-timeout")
                    if rr is TIMEOUT:
                        raise TimeoutError("mem0 consolidate demote 超时")
                except Exception as e:
                    import logging
                    logging.warning("mem0 consolidate demote failed: %r", e)
            for r in plan["expired"]:
                try:
                    dele = getattr(self._m, "delete", None)
                    if dele is None:
                        continue  # mem0 无 delete API → 仅报告过期计划
                    rr = call_with_timeout(
                        lambda r=r, dele=dele: dele(r["id"]),
                        _MEM0_TIMEOUT, name="mem0-timeout")
                    if rr is TIMEOUT:
                        raise TimeoutError("mem0 consolidate expire 超时")
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
        # Task 8: 超时预算；超时 raise TimeoutError——调用方 base.note_recalled
        # 已按条目 except Exception 捕获并记日志，此处超时即按失败降级同语义。
        rr = call_with_timeout(
            lambda: upd(memory_id, metadata={"recall_count": count}),
            _MEM0_TIMEOUT, name="mem0-timeout")
        if rr is TIMEOUT:
            raise TimeoutError("mem0 _persist_recall 超时")

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
            # Task 8: 超时预算；超时返回 0 退化为进程内计数（与当前失败路径同语义）。
            rec = call_with_timeout(lambda: getter(memory_id), _MEM0_TIMEOUT, name="mem0-timeout")
            if rec is TIMEOUT:
                raise TimeoutError("mem0 get_recall_count 超时")
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
            # F-A5-07 (#309): get_all 用 call_with_timeout 包裹（对齐 search 的
            # _MEM0_TIMEOUT=10.0）。否则 qdrant 挂起会在 daemon evaluate 锁内
            # 无上限阻塞 → 并发进程 5s 拿不到锁降级无锁 → lost update 前置成立。
            # 超时按失败降级（置不可用 + 60s 节流重探自愈），与 search 语义一致。
            r = call_with_timeout(
                lambda: self._m.get_all(
                    filters={"user_id": self.user_id},
                    top_k=self.max_rows if top_k is None else top_k,
                ),
                _MEM0_TIMEOUT,
                name="mem0-timeout",
            )
            if r is TIMEOUT:
                raise TimeoutError("mem0 get_all 超时")
            results = r.get("results", [])
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
            # Task 8: 超时预算；超时 fall through 至 _all_rows fallback。
            info = call_with_timeout(lambda: store.col_info(), _MEM0_TIMEOUT, name="mem0-timeout")
            if info is TIMEOUT:
                raise TimeoutError("_count_rows col_info 超时")
            n = int(getattr(info, "points_count", -1))
            if n >= 0:
                return n
        except (ValueError, TypeError, OSError, AttributeError, TimeoutError):
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
