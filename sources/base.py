# ============================================================
# sources/base.py — Source 插件接口（v2 §3.10）
# Observation = 一条世界观测事实（对应 §3.7 的 *.observed 事件）；
# Source 只 observe()，不允许决定 send=true / 触发类型。
# ============================================================

import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Observation:
    """一条世界观测。payload 为 JSON-safe 自由形状，按 type 约定字段。"""

    type: str                     # 点分类型：schedule.state / holiday.upcoming / ...
    source: str                   # 产生者：各 Source.name（如 weather）
    observed_at: datetime         # 观察时刻（CST aware）
    expires_at: datetime | None   # 过期即作废；None = 不过期
    payload: dict


class Source(Protocol):
    """观测源协议：name 标识 + observe(now) 产出观测列表。"""

    name: str

    def observe(self, now: datetime) -> list[Observation]: ...


def observe_all(sources: Iterable[Source], now: datetime) -> list[Observation]:
    """逐源 observe；单源异常 → 该源空结果 + stderr 告警，绝不抛出。"""
    observations: list[Observation] = []
    for src in sources:
        name = getattr(src, "name", type(src).__name__)
        try:
            observations.extend(src.observe(now) or [])
        except Exception as exc:  # noqa: BLE001 — 源隔离：任何单源异常都不拖垮整轮观测
            print(f"[sources] {name} observe 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    return observations
