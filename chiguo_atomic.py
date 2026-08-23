# ============================================================
# chiguo_atomic.py — 原子写统一助手共享模块
#
# Q23: 合并 11 个文件各自实现的原子写（tmp → os.replace）模式。
# 统一语义：先写 .tmp 暂存，再 os.replace() 原子覆盖目标，避免写崩
# 损坏正式文件；可选一步到位 0600（os.open 建 tmp 即 0600，无先写后
# chmod 的泄露窗口）、fsync、写出前校验回调。
#
# 行为约定：
#   - 内容先写到 <path>.tmp，成功后 os.replace 覆盖 path。
#   - mode 给定 → tmp 以该权限一步创建（os.open(O_CREAT)）；mode=None →
#     以默认 umask 创建（用于 holidays.json 等非隐私数据）。
#   - fsync=True → replace 前 fsync 落盘。
#   - verify 给定 → replace 前对 tmp 调 verify(tmp_path)；抛异常则中止
#     replace，并自动清理残留 .tmp 后向上抛出（由调用方决定终止/告警）。
#   - 低层 OSError 抛出，由调用方决定终止/告警/降级。
# ============================================================

import os
from pathlib import Path


def atomic_write(path, data, *, mode=None, fsync=False, verify=None,
                 tmp_path=None):
    """Atomic write `data` (str or bytes) to `path` via tmp → os.replace.

    Args:
        path: 目标路径（str 或 os.PathLike）。
        data: 要写入的内容（str 用文本模式，bytes 用二进制模式）。
        mode: 非 None → tmp 以该模式一步创建（os.open(O_CREAT, mode)，
            0600 一步到位，无先写后 chmod 窗口）。None → 默认 umask。
        fsync: True → replace 前对 tmp fd fsync 落盘。
        verify: 可选 callable(tmp_path)，在 replace 前调用；抛异常则中止
            replace，且 helper 自动清理残留 .tmp 后把异常向上抛出。
        tmp_path: 可选，覆盖默认 <path>.tmp。
    """
    path = os.fspath(path)
    tmp = os.fspath(tmp_path) if tmp_path is not None else (str(path) + ".tmp")

    if mode is not None:
        # 一步到位权限：os.open(O_CREAT) 以 mode 建 tmp（尊重 umask 的 min）。
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        if isinstance(data, bytes):
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(data)
        # os.fdopen 已接管 fd（异常时也会关闭）
    else:
        # 默认 umask（非隐私数据，如 holidays.json）
        ptmp = Path(tmp)
        if isinstance(data, bytes):
            ptmp.write_bytes(data)
        else:
            ptmp.write_text(data)

    if fsync:
        try:
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
        except OSError:
            pass

    if verify is not None:
        try:
            verify(tmp)
        except BaseException:
            # 校验失败 → 清理暂存，避免残留无效 .tmp 被恢复路径误读；再抛出。
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    os.replace(tmp, path)


def append_jsonl_0600(path, obj):
    """Append one JSON object as a JSONL line with atomic 0600 creation (no chmod window).

    Uses os.open(O_CREAT, 0o600) → fdopen → write to avoid the µs window of
    open("a") → chmod 0600 → write where the file is briefly 0644. Subsequent
    appends reuse the existing 0600 file without extra chmod calls. Ensures
    parent directory exists.

    Args:
        path: JSONL file path (str or PathLike).
        obj: JSON-serializable object to append as one line.
    """
    import json
    p = Path(os.fspath(path))
    # Ensure parent exists (no-op if already there).
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    # O_CREAT with 0o600 ensures first-create is already 0600 (umask-respected min).
    # O_WRONLY | O_APPEND | O_CREAT: create if missing, append if exists.
    fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # Best-effort: ensure existing file is 0600 (covers pre-existing 0644 from old code).
        try:
            st = os.fstat(fd)
            if (st.st_mode & 0o777) != 0o600:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
        except OSError:
            pass
        # 循环写完：os.write 可能部分写入（PIPE_BUF/信号中断），需补写剩余。
        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n == 0:
                raise OSError("os.write returned 0 bytes (append_jsonl_0600)")
            written += n
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
