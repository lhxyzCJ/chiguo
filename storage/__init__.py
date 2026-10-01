"""storage — Chiguo v2 持久化层（SQLite canonical state store）。

- storage.sqlite.db       连接/PRAGMA/事务/完整性/备份
- storage.sqlite.migrations  顺序迁移（schema_migrations + checksum 防漂移）
- storage.events          EventStore：事件日志（append-only）+ 因果链
"""
