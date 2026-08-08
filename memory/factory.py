# ============================================================
# memory/factory.py — 记忆后端工厂（v1.8 解耦）
#
# [memory].backend 取值：
#   "auto"    （默认）LanceDB 可用 → LanceDbBackend；否则 JSON 兜底
#   "lancedb"          显式 LanceDB（库缺失/路径不可用 → available=False 降级）
#   "json"             显式 JSON 手动记忆文件
#   "module.path.ClassName"  自定义后端类（importlib 动态加载，
#                            实例化 kwargs = [memory] 段其余键）
# 自定义后端只需实现 memory/base.py MemoryBackend 的四个原语。
# ============================================================

import importlib
import importlib.util
import inspect
import os
from pathlib import Path

from memory.base import MemoryBackend
from memory.json import JsonMemoryBackend
from memory.lancedb import LanceDbBackend

_DEFAULT_MANUAL = "data/chiguo_memories.json"
_DEFAULT_LANCEDB_PATH = "~/.pi-agent/memory/lancedb-pro"


def _resolve_path(raw: str, base_dir: Path | None) -> str:
    """相对路径锚定 base_dir（缺省 cwd）；绝对路径/~ 原样保留。"""
    p = Path(os.path.expanduser(raw))
    if p.is_absolute():
        return str(p)
    return str((base_dir or Path.cwd()) / p)


def _lancedb_importable() -> bool:
    try:
        importlib.util.find_spec("lancedb")
        return True
    except (ImportError, ValueError):
        return False


def create_backend(config: dict | None = None, base_dir: str | Path | None = None) -> MemoryBackend:
    """按 [memory] 配置创建记忆后端。

    config: toml [memory] 段 dict（backend/lancedb_path/lancedb_table/
            manual_path/ebbinghaus_strength/ebbinghaus_min_weight + 自定义键）。
    base_dir: 相对路径锚定目录（daemon 的 _base_dir；缺省 cwd）。
    """
    cfg = config or {}
    backend = cfg.get("backend", "auto")
    base = Path(base_dir) if base_dir else None
    strength = cfg.get("ebbinghaus_strength")
    min_weight = cfg.get("ebbinghaus_min_weight")

    # 自定义类路径（含 "." 且非内置名）
    if isinstance(backend, str) and "." in backend and backend not in ("auto", "lancedb", "json"):
        module_name, _, class_name = backend.rpartition(".")
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        if not issubclass(cls, MemoryBackend):
            raise TypeError(f"[memory].backend={backend} 不是 MemoryBackend 子类")
        kwargs = {k: v for k, v in cfg.items() if k != "backend"}
        # 按构造签名过滤（内置键名如 manual_path 对自定义类可能不适用；**kwargs 类全传）
        try:
            params = inspect.signature(cls.__init__).parameters
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):
            pass
        return cls(**kwargs)

    if backend == "lancedb":
        return LanceDbBackend(
            db_path=cfg.get("lancedb_path", _DEFAULT_LANCEDB_PATH),
            table_name=cfg.get("lancedb_table", "memories"),
            strength=strength, min_weight=min_weight,
        )

    if backend == "json":
        return JsonMemoryBackend(
            path=_resolve_path(cfg.get("manual_path", _DEFAULT_MANUAL), base),
            strength=strength, min_weight=min_weight,
        )

    # auto（默认）：LanceDB 可导入 → LanceDB；否则 JSON 兜底
    if _lancedb_importable():
        return LanceDbBackend(
            db_path=cfg.get("lancedb_path", _DEFAULT_LANCEDB_PATH),
            table_name=cfg.get("lancedb_table", "memories"),
            strength=strength, min_weight=min_weight,
        )
    return JsonMemoryBackend(
        path=_resolve_path(cfg.get("manual_path", _DEFAULT_MANUAL), base),
        strength=strength, min_weight=min_weight,
    )
