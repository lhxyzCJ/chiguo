# ============================================================
# schedule/anniversary.py — 纪念日管理(迁自 anniversary_manager.py,批次 2)
# 变更:显式 base_dir 参数(不再依赖 __file__/cwd 解析);新增 mmdd_to_date 工具;
#       DEFAULT_ANNIVERSARIES 默认集合 + 读路径内存合并(不落盘);
#       countdown 已废弃,6c 同批删除(白名单仅 anniversary:读入即丢非 anniversary 类型,
#       countdown 分支全部删除,cleanup 整方法删);
#       历史 countdown 由 api 迁移入口(②)直读原始文件迁为 reminder,不受白名单影响。
# ============================================================

import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

# CST 锚定不需要(纯日期,无时区)——本文件不引入 datetime timezone。

DEFAULT_ANNIVERSARIES = [
    {"type": "anniversary", "name": "迟菓生日", "date": "05-11"},
]


def mmdd_to_date(date_str: str, year: int) -> date:
    """MM-DD → date。02-29 仅在目标年非闰年兜底为 02-28;目标年恰为闰年保留 02-29。"""
    month, day = (int(x) for x in date_str.split("-"))
    if (month, day) == (2, 29):
        try:
            return date(year, 2, 29)
        except ValueError:
            return date(year, 2, 28)
    return date(year, month, day)


@dataclass
class Anniversary:
    id: str
    type: str          # "anniversary"(唯一合法类型;countdown 已废弃)
    name: str          # 人类可读名称
    date: str          # "MM-DD"
    note: str = ""
    created_at: str = ""


class AnniversaryManager:
    """纪念日/倒计时 CRUD 管理。持久化到 JSON 文件。"""

    def __init__(self, base_dir: str):
        self._path = Path(base_dir) / "anniversaries.json"
        self._items: list[Anniversary] = []
        self._corrupt = False
        self._load()

    # ── 持久化 ──────────────────────────────────────────

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                # 顶层非 dict(list 等历史脏形状)→ 视同损坏:合并默认、不崩 daemon 启动(R12)
                if not isinstance(data, dict):
                    self._corrupt = True
                    self._items = []
                else:
                    anns = data.get("anniversaries", [])
                    # 白名单:type 仅 anniversary;countdown 条目读入即丢(M20/6c,
                    # 历史数据已由 api 迁移入口②直读原始文件迁为 reminder)
                    # 显式取值构造:未知键(extra 等历史脏键)忽略,不误判 corrupt
                    items = []
                    for a in anns:
                        if not self._valid(a) or a.get("type") != "anniversary":
                            continue
                        items.append(Anniversary(
                            id=a["id"], type=a["type"], name=a["name"], date=a["date"],
                            note=a.get("note", ""), created_at=a.get("created_at", "")))
                    self._items = items
            except (json.JSONDecodeError, TypeError, AttributeError):
                self._corrupt = True
                self._items = []

    def _save(self):
        data = json.dumps({
            "anniversaries": [asdict(a) for a in self._items],
        }, indent=2, ensure_ascii=False)
        tmp = Path(str(self._path) + ".tmp")
        tmp.write_text(data)
        os.chmod(tmp, 0o600)  # Q13: 纪念日属隐私文件 → tmp 即 0600，os.replace 后正式文件同权限
        os.replace(tmp, self._path)

    @staticmethod
    def _valid(a: dict) -> bool:
        """基础字段校验"""
        return all(k in a for k in ("id", "type", "name", "date"))

    def visible_items(self) -> list[dict]:
        """读路径视图:文件缺失/损坏 → 内存合并默认集合(不落盘);文件存在 → 原文条目。"""
        if not self._path.exists():
            return list(DEFAULT_ANNIVERSARIES)
        if not self._items and self._corrupt:
            return list(DEFAULT_ANNIVERSARIES)
        return [asdict(a) for a in self._items]

    # ── CRUD ────────────────────────────────────────────

    def add(self, type_: str, name: str, date_str: str, note: str = "") -> Anniversary:
        """
        添加纪念日(6c:countdown 已废弃,仅收 anniversary)。
        Raises ValueError on invalid format.
        """
        if type_ != "anniversary":
            raise ValueError("type must be 'anniversary'")

        # 校验日期格式(MM-DD;用闰年测试合法性)
        date.fromisoformat(f"2024-{date_str}")

        a = Anniversary(
            id=uuid.uuid4().hex[:12],
            type=type_,
            name=name.strip(),
            date=date_str.strip(),
            note=note.strip(),
            created_at=date.today().isoformat(),
        )
        self._items.append(a)
        self._save()
        return a

    def remove(self, id_: str) -> bool:
        """按 id 删除。返回是否找到并删除。"""
        before = len(self._items)
        self._items = [a for a in self._items if a.id != id_]
        if len(self._items) < before:
            self._save()
            return True
        return False

    def list_all(self) -> list[Anniversary]:
        """返回所有纪念日副本（按日期排序）。"""
        return sorted(self._items, key=lambda a: a.date)

    def update(self, id_: str, **kwargs) -> Anniversary | None:
        """
        更新纪念日。可更新字段：name, date, type, note。
        如果更新 date，会重新校验格式。
        Returns updated Anniversary or None if not found.
        """
        for a in self._items:
            if a.id == id_:
                if "name" in kwargs:
                    a.name = kwargs["name"].strip()
                if "note" in kwargs:
                    a.note = kwargs["note"].strip()
                if "type" in kwargs:
                    if kwargs["type"] != "anniversary":
                        raise ValueError("type must be 'anniversary'")
                    a.type = kwargs["type"]
                if "date" in kwargs:
                    d = kwargs["date"].strip()
                    date.fromisoformat(f"2024-{d}")
                    a.date = d
                self._save()
                return a
        return None

    # ── 查询 ────────────────────────────────────────────

    def get_today(self, today: date) -> list[Anniversary]:
        """
        返回今天匹配的所有纪念日。
        anniversary: 月和日匹配
        """
        mmdd = today.strftime("%m-%d")
        return [a for a in self._items if a.date == mmdd]

    def get_upcoming(self, today: date, days: int = 7) -> list[tuple[Anniversary, int]]:
        """
        返回未来 days 天内的纪念日，按距离今天的天数升序排列。
        正确处理年边界：12月查1月纪念日 → 查下一年。
        """
        result = []
        today_ord = today.toordinal()

        for a in self._items:
            # 今年
            try:
                d_this = date.fromisoformat(f"{today.year}-{a.date}")
            except ValueError:
                # 2月29日在非闰年 → 当2月28日
                if a.date == "02-29":
                    d_this = date(today.year, 2, 28)
                else:
                    continue
            delta = d_this.toordinal() - today_ord
            if 0 < delta <= days:
                result.append((a, delta))
            elif delta <= 0:
                # 今年已过，查明年
                try:
                    d_next = date.fromisoformat(f"{today.year + 1}-{a.date}")
                except ValueError:
                    if a.date == "02-29":
                        d_next = date(today.year + 1, 2, 28)
                    else:
                        continue
                delta = d_next.toordinal() - today_ord
                if 0 < delta <= days:
                    result.append((a, delta))

        result.sort(key=lambda x: x[1])
        return result
