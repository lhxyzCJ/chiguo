# ============================================================
# chiguo_eventbus.py — 轻量事件总线
# 发布/订阅模式，零依赖，同步执行。
# 参考 xiaoyou-core EventBus 设计。
# ============================================================

import threading
import traceback
from typing import Callable, Any


class EventBus:
    """轻量发布/订阅事件总线。同步执行，零依赖。"""

    def __init__(self):
        self._subscribers: dict[str, list[Callable[..., Any]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, handler: Callable[..., Any]):
        """订阅事件。handler 接收 **kwargs。"""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)

    def publish(self, event_type: str, **data) -> list[Any]:
        """发布事件。返回所有 handler 的结果列表。失败 handler 的结果为 None。"""
        with self._lock:
            handlers = list(self._subscribers.get(event_type, []))
        results = []
        for handler in handlers:
            try:
                result = handler(**data)
                results.append(result)
            except Exception:
                traceback.print_exc()
                results.append(None)
        return results


_bus: EventBus | None = None


def get_eventbus() -> EventBus:
    """获取全局 EventBus 单例。"""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_eventbus():
    """重置全局 EventBus（测试用）。"""
    global _bus
    _bus = EventBus()
