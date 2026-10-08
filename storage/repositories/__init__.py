"""storage.repositories — v2 canonical store 的仓储层（纯 sqlite3，无 ORM）。

约定：
- 每个仓储类持有 `Database` 实例（`Repo(db)`），不自行缓存连接；
- 表的一行 → 一个 frozen dataclass，字段与表列一一对应；
- JSON 列在仓储边界做 dict ⇄ TEXT 转换，时间列统一 datetime + CST isoformat 存取；
- 写操作单条 INSERT/UPDATE 直接 execute；跨表操作走 `db.transaction()`
  （BEGIN IMMEDIATE）保证原子；
- 查询未命中返回 None / 空列表；不吞 sqlite3 错误（FK/约束自然抛出）。
"""
