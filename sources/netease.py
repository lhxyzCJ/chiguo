# ============================================================
# sources/netease.py — 听歌观测源
# 只读包装既有 NeteaseService（注入实例或懒建），产出 music.observed：
# 近 2h（netease.play_proof_window_hours 可配）内确有播放时，给出播放事实。
# 不消费音乐话题配额（peek/consume 是旧链路的事），不写入任何状态。
# ============================================================

import sys
from datetime import datetime

from chiguo_time import CST
from sources.base import Observation

_DEFAULT_WINDOW_HOURS = 2.0


def _now_cst(now: datetime) -> datetime:
    return now.replace(tzinfo=CST) if now.tzinfo is None else now


class NeteaseSource:
    name = "netease"

    def __init__(self, service=None, base_dir=None, config: dict | None = None):
        self._service = service
        self.base_dir = str(base_dir) if base_dir is not None else None
        self.config = config or {}

    def _get_service(self):
        if self._service is None:
            if not self.base_dir:
                raise ValueError("NeteaseSource 需要注入 service 或提供 base_dir")
            from netease.service import NeteaseService
            self._service = NeteaseService(self.config, self.base_dir)
        return self._service

    def observe(self, now: datetime) -> list[Observation]:
        try:
            return self._observe(_now_cst(now))
        except Exception as exc:  # noqa: BLE001 — fail-safe：网络/服务异常 → 空观测
            print(f"[sources.netease] observe 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return []

    def _observe(self, now: datetime) -> list[Observation]:
        service = self._get_service()
        if not getattr(service, "enabled", False):
            return []
        plays = service.fetch_play_proof(now) or []
        if not plays:
            return []
        net_cfg = self.config.get("netease", {}) if isinstance(self.config, dict) else {}
        try:
            window_h = float(net_cfg.get("play_proof_window_hours", _DEFAULT_WINDOW_HOURS))
        except (TypeError, ValueError):
            window_h = _DEFAULT_WINDOW_HOURS
        now_ms = now.timestamp() * 1000
        recent = []
        for p in plays:
            if not isinstance(p, dict):
                continue
            pt = p.get("playTime")
            if not isinstance(pt, (int, float)) or isinstance(pt, bool):
                continue
            if 0 <= now_ms - pt <= window_h * 3600 * 1000:
                recent.append({"playTime": int(pt), "name": str(p.get("name", "")),
                               "artist": str(p.get("artist", ""))})
        if not recent:
            return []
        recent.sort(key=lambda p: p["playTime"], reverse=True)
        return [Observation(type="music.observed", source=self.name, observed_at=now,
                            expires_at=None, payload={"plays": recent})]
