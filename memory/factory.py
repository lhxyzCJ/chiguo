# ============================================================
# memory/factory.py — 记忆后端工厂（v1.8 解耦；v1.9 默认 mem0）
#
# mem0 为唯一记忆后端（[memory].backend 仅接受 "mem0"/"auto" 遗留值）：
#   "mem0"   （默认）mem0 AI 记忆后端（mem0ai 库；库缺失/无 key/
#             ollama 未启动 → available=False 优雅降级，60s 节流重试）
# 自定义类路径（module.path.ClassName）已移除，非 "mem0"/"auto" 直接抛错。
# ============================================================

import os
from pathlib import Path

from memory.base import MemoryBackend
from memory.mem0_backend import Mem0Backend


def create_backend(config: dict | None = None, base_dir: str | Path | None = None) -> MemoryBackend:
    """按 [memory] 配置创建记忆后端（mem0 唯一）。

    config: toml [memory] 段 dict（backend + mem0_* 键 +
            ebbinghaus_strength/ebbinghaus_min_weight）。
    base_dir: 相对路径锚定目录（daemon 的 _base_dir；缺省 cwd）。
    """
    cfg = config or {}
    backend = cfg.get("backend", "mem0")
    if backend not in ("mem0", "auto"):
        raise ValueError(f"[memory].backend={backend} 不是受支持的后端;mem0 是唯一记忆后端")
    base = Path(base_dir) if base_dir else None
    strength = cfg.get("ebbinghaus_strength")
    min_weight = cfg.get("ebbinghaus_min_weight")

    # mem0 后端（唯一）；相对路径（qdrant_path/history_db）锚定 base_dir
    return Mem0Backend(
        user_id=cfg.get("mem0_user_id", "chiguo"),
        collection_name=cfg.get("mem0_collection", "chiguo"),
        qdrant_path=_resolve_path(cfg.get("mem0_qdrant_path", "data/mem0/qdrant"), base),
        history_db=_resolve_path(cfg.get("mem0_history_db", "data/mem0/history.db"), base),
        llm_model=cfg.get("mem0_llm_model"),
        llm_base_url=cfg.get("mem0_llm_base_url"),
        llm_api_key=cfg.get("mem0_llm_api_key"),
        embedder_model=cfg.get("mem0_embedder_model"),
        embedder_base_url=cfg.get("mem0_embedder_base_url"),
        embedder_dims=cfg.get("mem0_embedder_dims"),
        max_rows=cfg.get("mem0_max_rows"),
        strength=strength, min_weight=min_weight,
        # ── C1/C2: 记忆巩固 & 复习强化（默认关闭恒等）──
        consolidate_enabled=cfg.get("consolidate_enabled", False),
        consolidate_sim_threshold=cfg.get("consolidate_sim_threshold"),
        consolidate_min_importance=cfg.get("consolidate_min_importance"),
        consolidate_max_age_hours=cfg.get("consolidate_max_age_hours"),
        reinforce_enabled=cfg.get("reinforce_enabled", False),
        reinforce_bonus=cfg.get("reinforce_bonus"),
    )


def _resolve_path(raw: str, base_dir: Path | None) -> str:
    """相对路径锚定 base_dir（缺省 cwd）；绝对路径/~ 原样保留。"""
    p = Path(os.path.expanduser(raw))
    if p.is_absolute():
        return str(p)
    return str((base_dir or Path.cwd()) / p)
