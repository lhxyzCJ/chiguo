"""state.pending — pending 话题薄包装域（Issue #379 自 state.interaction 拆出）。"""
from datetime import datetime
from chiguo_pending import pending_add, pending_resolve, pending_mark_attempted, pending_prune


class PendingMixin:
    def add_pending_topic(self, topic: str, now: datetime, source: str = "analysis"):
        """薄包装：委托 chiguo_pending.pending_add（纯函数），保持 API 与行为不变。"""
        self.pending_topics = pending_add(self.pending_topics, topic, now, source)

    def resolve_pending_topic(self, topic: str | None, now: datetime):
        """薄包装：委托 pending_resolve。"""
        self.pending_topics = pending_resolve(self.pending_topics, topic, now)

    def mark_pending_topic_attempted(self, topic: str):
        """薄包装：委托 pending_mark_attempted（就地）。"""
        pending_mark_attempted(self.pending_topics, topic)

    def prune_pending_topics(self, now: datetime, max_age_hours: float = 48.0):
        """薄包装：委托 pending_prune。"""
        self.pending_topics = pending_prune(self.pending_topics, now, max_age_hours)

    def _cap_pending_topics(self, cap: int = 20):
        if len(self.pending_topics) > cap:
            self.pending_topics = self.pending_topics[-cap:]
