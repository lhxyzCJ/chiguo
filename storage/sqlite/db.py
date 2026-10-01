"""storage.sqlite.db — SQLite 连接管理（PRAGMA / 事务 / 完整性 / 备份）。

单用户本地应用的 canonical structured state store：
- WAL + synchronous=NORMAL + foreign_keys=ON + busy_timeout=5000；
- 写事务统一 BEGIN IMMEDIATE（显式事务，隔离级别 autocommit）；
- 数据库文件（含 -wal/-shm）隐私收紧到 0600；
- 损坏/非数据库文件 → StorageError（fail-fast，不静默重建）。
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class StorageError(Exception):
    """storage 层基类异常（损坏 / 迁移漂移 / IO 失败）。"""


class SchemaTooNewError(StorageError):
    """数据库 schema 版本高于当前代码（旧代码打开新库）→ 拒绝，防误写。"""


class Database:
    """一个 SQLite 数据库的连接持有者（进程内单连接，懒建）。

    并发模型：单写者事务（BEGIN IMMEDIATE）+ WAL 多读者；
    跨进程并发由 busy_timeout=5000 兜底。
    """

    def __init__(self, path):
        self.path = Path(os.path.expanduser(str(path)))
        self._conn: sqlite3.Connection | None = None

    # ── 连接 ─────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """打开（或复用）连接；PRAGMA 就位；损坏即抛 StorageError。"""
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            conn = sqlite3.connect(str(self.path), isolation_level=None,
                                   timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            # 强制读 header：损坏/非 SQLite 文件在此失败（而非首次查询才失败）
            conn.execute("PRAGMA schema_version").fetchone()
        except sqlite3.DatabaseError as e:
            raise StorageError(
                f"SQLite 数据库无法打开（损坏或非数据库文件）: {self.path}: {e}") from e
        self._conn = conn
        self._harden_permissions()
        return conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _harden_permissions(self):
        """主库与 WAL/SHM 文件收紧 0600（对话/记忆为隐私数据）。"""
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass

    # ── 事务 / 查询辅助 ───────────────────────────────────

    @contextmanager
    def transaction(self):
        """写事务：BEGIN IMMEDIATE → yield conn → COMMIT / 异常 ROLLBACK。"""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        conn.execute("COMMIT")

    def schema_version(self) -> int:
        """schema_migrations 最大版本；表不存在/空 → 0。"""
        conn = self.connect()
        try:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row["v"] or 0)

    def table_names(self) -> list[str]:
        conn = self.connect()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    # ── 运维（CLI 支撑）───────────────────────────────────

    def integrity(self) -> dict:
        """integrity_check + foreign_key_check（两者都通过才 ok=True）。"""
        conn = self.connect()
        ic = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        fk = [tuple(r) for r in conn.execute("PRAGMA foreign_key_check").fetchall()]
        return {
            "ok": ic == ["ok"] and not fk,
            "integrity": ic,
            "foreign_key_violations": fk,
        }

    def backup(self, dest) -> Path:
        """在线备份（sqlite3 backup API）到 dest；目标 0600。返回目标路径。"""
        dest = Path(os.path.expanduser(str(dest)))
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        src = self.connect()
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        return dest
