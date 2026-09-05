# ============================================================
# chiguo_concurrent.py — 线程超时助手单源（#397 收敛）
# 收敛原先三处重复的 threading 计时器实现：
#   chiguo_topics._call_with_timeout（超时 None）、
#   ops/engine_ops._call_with_timeout（超时 (False, None)）、
#   memory/mem0_backend._call_with_timeout（超时哨兵）。
# 统一哨兵语义：超时返回 TIMEOUT（专用对象，与被包装函数的合法
# None 返回值不碰撞）；异常原样重抛；成功返回 fn() 结果。
# 超时后放弃等待，遗留 daemon 线程在底层返回后自然结束。
# ============================================================

import threading

# 超时哨兵：专用对象替代 None，避免与被包装函数的合法 None 碰撞。
TIMEOUT = object()


def call_with_timeout(fn, timeout: float, name: str = "call-with-timeout"):
    """在 daemon 线程执行 fn；超时返回 TIMEOUT；异常重抛；正常返回 fn() 结果。"""
    box = {}

    def runner():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001 — 跨线程重抛，保持调用方异常语义
            box["e"] = e

    t = threading.Thread(target=runner, daemon=True, name=name)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return TIMEOUT
    if "e" in box:
        raise box["e"]
    return box.get("v")
