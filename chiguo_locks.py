# ============================================================
# chiguo_locks.py — 跨进程文件锁（fcntl.flock，可重入）共享模块
#
# Q21: 合并 chiguo_state.py(v6 跨进程锁) 与 scripts/agent_health.py
# 中重复的 flock 实现。锁文件常驻，os.replace 换 inode 不影响锁。
# 防止 cron + 手动运行多实例竞争写导致文件互相覆盖。
#
# 可重入：模块级 _LOCK_FDS/_LOCK_DEPTH 保证同进程共享同一 fd 与
# 深度计数，内外层调用混用不阻塞、不提前释放。flock 为 fd 级互斥，
# 深度只需表达 0/1 持有态。
#
# 跨进程互斥；同进程重入直接通过；非 POSIX 或超时 → 降级无锁。
# ============================================================

import time as _time
from contextlib import contextmanager

# lock_path(str) → fd(int, 持锁中)。同进程所有调用共享同一 fd，
# 避免对同一文件二次 open 造成 flock 自死锁。
_LOCK_FDS: dict[str, int] = {}
# lock_path(str) → 深度(0/1 持有态)。
_LOCK_DEPTH: dict[str, int] = {}

# 获取超时（秒）。与既有实现在 5s 内拿不到 → 降级无锁保持一致。
LOCK_TIMEOUT_S = 5.0


def acquire(lock_path: str, *, timeout: float = LOCK_TIMEOUT_S,
            on_timeout=None) -> bool:
    """获取进程级独占锁（可重入）。返回 True 表示本次真正获得锁（需配套 release）。

    重入（同进程已持有）直接通过且不递增深度。非 POSIX 或 timeout 内
    拿不到锁 → 降级无锁。on_timeout 可选回调（收到 lock_path），供调用方
    记录审计/告警，仅在超时放弃时调用一次。
    """
    if _LOCK_DEPTH.get(lock_path, 0) > 0:
        return False
    fd = _LOCK_FDS.get(lock_path)
    if fd is None:
        try:
            import fcntl
        except ImportError:
            return False  # 非 POSIX → 降级无锁
        try:
            fd = open(lock_path, "a+")
        except OSError:
            return False
        try:
            deadline = _time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if _time.monotonic() >= deadline:
                        if on_timeout is not None:
                            on_timeout(lock_path)
                        fd.close()
                        return False
                    _time.sleep(0.1)
        except OSError:
            try:
                fd.close()
            except OSError:
                pass
            return False
        _LOCK_FDS[lock_path] = fd
    _LOCK_DEPTH[lock_path] = 1
    return True


def release(lock_path: str):
    """释放锁（仅持有者调用）。释放 fd 并清空持有标记。"""
    if _LOCK_DEPTH.get(lock_path, 0) <= 0:
        return
    _LOCK_DEPTH.pop(lock_path, None)
    fd = _LOCK_FDS.pop(lock_path, None)
    if fd is not None:
        try:
            import fcntl
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            fd.close()
        except OSError:
            pass


def in_lock(lock_path: str) -> bool:
    """当前进程是否已持有该锁（供调用方判断重入场景）。"""
    return _LOCK_DEPTH.get(lock_path, 0) > 0


@contextmanager
def lock(lock_path: str, *, timeout: float = LOCK_TIMEOUT_S, on_timeout=None):
    """持有锁的上下文管理器；yield 出本次是否真正获得锁(bool)。

    同进程重入直接通过；跨进程互斥；timeout 内获取失败则降级无锁。
    未获得锁（timeout/非 POSIX）时 on_timeout 若给出会被调用一次。
    """
    acquired = acquire(lock_path, timeout=timeout, on_timeout=on_timeout)
    try:
        yield acquired
    finally:
        if acquired:
            release(lock_path)
