#!/usr/bin/env python3
"""test_eventbus.py — EventBus 单元测试"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chiguo_eventbus import EventBus, get_eventbus, reset_eventbus


def test_subscribe_and_publish():
    """基本订阅/发布"""
    bus = EventBus()
    received = []

    def handler(**kwargs):
        received.append(kwargs)

    bus.subscribe("test", handler)
    results = bus.publish("test", msg="hello", count=42)

    assert len(received) == 1
    assert received[0]["msg"] == "hello"
    assert received[0]["count"] == 42
    print("  OK test_subscribe_and_publish")


def test_multiple_subscribers():
    """多订阅者"""
    bus = EventBus()
    results = []

    def h1(**kw): results.append(1)
    def h2(**kw): results.append(2)
    def h3(**kw): results.append(3)

    bus.subscribe("e", h1)
    bus.subscribe("e", h2)
    bus.subscribe("e", h3)

    bus.publish("e")
    assert results == [1, 2, 3]
    print("  OK test_multiple_subscribers")


def test_unsubscribe():
    """取消订阅"""
    bus = EventBus()
    results = []

    def h(**kw): results.append(1)

    bus.subscribe("e", h)
    bus.publish("e")
    assert len(results) == 1

    bus.unsubscribe("e", h)
    bus.publish("e")
    assert len(results) == 1  # 不再增加
    print("  OK test_unsubscribe")


def test_no_subscribers_no_crash():
    """发布到无订阅者的事件不崩溃"""
    bus = EventBus()
    results = bus.publish("nonexistent", x=1)
    assert results == []
    print("  OK test_no_subscribers_no_crash")


def test_has_subscribers():
    """检查是否有订阅者"""
    bus = EventBus()
    assert not bus.has_subscribers("e")

    def h(**kw): pass
    bus.subscribe("e", h)
    assert bus.has_subscribers("e")

    bus.unsubscribe("e", h)
    assert not bus.has_subscribers("e")
    print("  OK test_has_subscribers")


def test_handler_exception_isolated():
    """一个 handler 异常不影响其他"""
    bus = EventBus()
    results = []

    def bad(**kw): raise RuntimeError("boom")
    def good(**kw): results.append(1)

    bus.subscribe("e", bad)
    bus.subscribe("e", good)
    bus.publish("e")

    assert results == [1]
    print("  OK test_handler_exception_isolated")


def test_clear():
    """清空所有订阅"""
    bus = EventBus()
    results = []

    def h(**kw): results.append(1)
    bus.subscribe("e", h)
    bus.clear()
    bus.publish("e")
    assert results == []
    print("  OK test_clear")


def test_global_singleton():
    """全局单例正确工作"""
    reset_eventbus()
    bus1 = get_eventbus()
    bus2 = get_eventbus()
    assert bus1 is bus2

    results = []
    def h(**kw): results.append(1)
    bus1.subscribe("e", h)
    bus2.publish("e")
    assert results == [1]

    reset_eventbus()
    bus3 = get_eventbus()
    assert bus3 is not bus1  # 重置后不同
    print("  OK test_global_singleton")


def test_publish_returns_results():
    """发布返回所有 handler 结果"""
    bus = EventBus()

    def h1(**kw): return "a"
    def h2(**kw): return "b"

    bus.subscribe("e", h1)
    bus.subscribe("e", h2)
    results = bus.publish("e")
    assert results == ["a", "b"]
    print("  OK test_publish_returns_results")


def test_multiple_events():
    """不同事件类型隔离"""
    bus = EventBus()
    r1, r2 = [], []

    bus.subscribe("e1", lambda **kw: r1.append(1))
    bus.subscribe("e2", lambda **kw: r2.append(2))

    bus.publish("e1")
    assert r1 == [1] and r2 == []
    bus.publish("e2")
    assert r1 == [1] and r2 == [2]
    print("  OK test_multiple_events")


if __name__ == "__main__":
    print("test_eventbus.py\n")
    tests = [
        test_subscribe_and_publish,
        test_multiple_subscribers,
        test_unsubscribe,
        test_no_subscribers_no_crash,
        test_has_subscribers,
        test_handler_exception_isolated,
        test_clear,
        test_global_singleton,
        test_publish_returns_results,
        test_multiple_events,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} eventbus tests passed.")
