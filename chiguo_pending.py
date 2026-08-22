# ============================================================
# chiguo_pending.py — 待接续话题纯逻辑（T10 补充抽离）
# 从状态主文件剥离的 pending_topics 管理纯函数，零反向依赖。
# ChiguoState 侧仅经 self.pending_topics 实例 + 薄包装保持 API 不变。
# ============================================================

from datetime import datetime, timedelta
from chiguo_time import CST


def pending_add(pending_topics: list[dict], topic: str, now: datetime, source: str = "analysis", cap: int = 20) -> list[dict]:
    """记录待接续话题（纯函数）。同话题视为已接续 → 移除旧条目后重新计时。"""
    if not isinstance(topic, str) or not topic.strip():
        return pending_topics
    topic = topic.strip()[:50]
    filtered = [t for t in pending_topics if not (isinstance(t, dict) and t.get("topic") == topic)]
    filtered.append({
        "topic": topic,
        "source": source,
        "created_at": now.isoformat(),
        "attempted": False,
        "untrusted": True,
    })
    if len(filtered) > cap:
        filtered = filtered[-cap:]
    return filtered


def pending_resolve(pending_topics: list[dict], topic: str | None, now: datetime) -> list[dict]:
    """topic_resolved=true → 移除对应话题。未指定 topic → 移除最旧一条。"""
    if isinstance(topic, str) and topic.strip():
        topic = topic.strip()[:50]
        return [t for t in pending_topics if not (isinstance(t, dict) and t.get("topic") == topic)]
    if pending_topics:
        return pending_topics[1:]
    return pending_topics


def pending_mark_attempted(pending_topics: list[dict], topic: str) -> None:
    """接话茬触发后标记已尝试（就地修改）。"""
    for t in pending_topics:
        if not isinstance(t, dict):
            continue
        if t.get("topic") == topic:
            t["attempted"] = True


def pending_prune(pending_topics: list[dict], now: datetime, max_age_hours: float = 48.0, cap: int = 20) -> list[dict]:
    """移除过期/已尝试话题,防状态膨胀。"""
    kept = []
    for t in pending_topics:
        if not isinstance(t, dict):
            continue
        if not isinstance(t.get("topic"), str):
            continue
        try:
            dt = datetime.fromisoformat(t.get("created_at", ""))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        age = (now - dt).total_seconds() / 3600
        if age <= max_age_hours and not t.get("attempted"):
            kept.append(t)
    if len(kept) > cap:
        kept = kept[-cap:]
    return kept
