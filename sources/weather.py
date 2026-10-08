# ============================================================
# sources/weather.py — 可选天气源（默认关）
# 配置 [weather] enabled=true + base_url（open-meteo 风格端点）+ latitude/longitude
# 后启用；纯 stdlib urllib，无网络/失败 → 空列表，不崩，不引入第三方依赖。
# 接口留待 Phase 6 配置接线；本文件保持最小。
# ============================================================

import json
import sys
from datetime import datetime, timedelta
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from chiguo_time import CST
from sources.base import Observation

_TTL = timedelta(minutes=30)

# WMO weather code → 中文描述（open-meteo 当前天气字段）
_WMO_CONDITIONS = {
    0: "晴", 1: "少云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}


def _now_cst(now: datetime) -> datetime:
    return now.replace(tzinfo=CST) if now.tzinfo is None else now


class WeatherSource:
    name = "weather"

    def __init__(self, config: dict | None = None):
        weather = (config or {}).get("weather", {}) or {}
        self.enabled = bool(weather.get("enabled", False))
        self.base_url = str(weather.get("base_url") or "").strip()
        self.latitude = weather.get("latitude")
        self.longitude = weather.get("longitude")
        try:
            self.timeout = float(weather.get("timeout_seconds", 10))
        except (TypeError, ValueError):
            self.timeout = 10.0

    def observe(self, now: datetime) -> list[Observation]:
        if not (self.enabled and self.base_url):
            return []   # 未配置 = 源关闭
        try:
            return self._observe(_now_cst(now))
        except Exception as exc:  # noqa: BLE001 — fail-safe：网络/解析异常 → 空观测
            print(f"[sources.weather] observe 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return []

    def _observe(self, now: datetime) -> list[Observation]:
        params = {"current": "temperature_2m,weather_code"}
        if self.latitude is not None:
            params["latitude"] = self.latitude
        if self.longitude is not None:
            params["longitude"] = self.longitude
        sep = "&" if "?" in self.base_url else "?"
        url = f"{self.base_url}{sep}{urllib_parse.urlencode(params)}"
        with urllib_request.urlopen(url, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        if not isinstance(data, dict):
            return []
        current = data.get("current")
        if isinstance(current, dict):
            code = current.get("weather_code")
            payload = {
                "condition": _WMO_CONDITIONS.get(code, f"weather_code={code}" if code is not None else None),
                "temperature": current.get("temperature_2m"),
                "weather_code": code,
                "time": current.get("time"),
            }
        else:
            # 通用端点：响应直接给 condition/temperature
            payload = {"condition": data.get("condition"), "temperature": data.get("temperature")}
        if payload.get("condition") is None and payload.get("temperature") is None:
            return []   # 响应无可辨识字段 → 不臆造观测
        return [Observation(type="weather.changed", source=self.name, observed_at=now,
                            expires_at=now + _TTL, payload=payload)]
