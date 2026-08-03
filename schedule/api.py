# ============================================================
# schedule/api.py — 全部时间安排的唯一写入口(§6,唯一写方)
# 写 = 校验 → 原子写 → 确认文案;迁移/物化守卫挂写类调用点惰性首执。
# 铁律:读路径(day_plan/T1/--attention/--schedule-recall/recall/引擎)永不进此模块的写路径。
# Task 6(批次 2c):when 全七形态经 resolve_when 纯换算;形态约束 → 学期边界 → 分端点过去校验
# → to_date 五态 → move 源槽/快照;仍兼容批 2b 旧协议显式 date/end_date 字段。
# ============================================================

import json
import os
import sys
from datetime import date, datetime

from schedule.override_store import OverrideStore, OverrideError, CST
from schedule.plan_store import PlanStore
from schedule import anniversary
from schedule.confirm import build_confirmation, build_question
from schedule.resolve_when import resolve_when, ResolveReject
from schedule.day_plan import week_number


class ApiRejection(Exception):
    """确定性拒绝。category 供 daemon 映射 H5 文案(H5_TEMPLATES)。"""

    def __init__(self, category: str, detail: str = ""):
        super().__init__(f"{category}: {detail}".rstrip(": "))
        self.category = category


class ScheduleApi:
    def __init__(self, base_dir: str, config: dict | None = None, today: date | None = None):
        self.base_dir = os.path.abspath(base_dir)  # 绝对锚定,防 cwd 依赖(批 2 遗留修正)
        self.config = config or {}
        self.today = today or date.today()
        self.overrides = OverrideStore(self.base_dir)
        self.anniversary_mgr = anniversary.AnniversaryManager(self.base_dir)
        self.plan_store = PlanStore(self.base_dir)
        self._migrated = False
        # 迁移子项激活开关(批次注记):② 激活批次 = 6c(Task 14);③④ 激活批次 = 4(Task 10)
        self._enable_countdown_migration = False
        self._enable_toml_migrations = False

    # ── 迁移/物化守卫(惰性首执,只读子命令永不触发,H1)──

    def _guard(self):
        if not self._migrated:
            self.ensure_migrations()
            self._migrated = True

    def ensure_migrations(self):
        """五子项全序(spec §3.1 M2/N3/F3,二十轮补 0):
        0. overrides 损坏重建 → ①. anniversaries 损坏重建 → ②. countdown→reminder(6c 激活)
        → ③. toml exam_weeks(批 4 激活) → ④. toml special_dates 合并(批 4 激活)。
        0 必须先于 ②:② 写 overrides,若重建在其后,刚迁入的 reminder 被空文件抹掉。"""
        # 0. overrides 损坏 → 重建为合法空文件(非 0 字节,二十轮 LOW 钉死)
        if self.overrides.corrupt:
            self.overrides._items = []  # noqa: 内部重建;写经 _save
            self.overrides._corrupt = False
            self.overrides._save()
            print(f"[schedule.api] overrides 损坏已重建为空集: {self.overrides.path}", file=sys.stderr)
        # ①. anniversaries 损坏 → 重建为默认生日(视同缺失路径,N1)
        if self.anniversary_mgr._corrupt or not self.anniversary_mgr._path.exists():
            self._materialize_anniversaries()
        # ②. countdown→reminder 防御迁移(激活 = 6c;幂等 label+date 去重,F4/N3)
        if self._enable_countdown_migration:
            self._migrate_countdown()
        # ③④. toml 一次性迁移(激活 = 批 4,见 Task 10)
        if self._enable_toml_migrations:
            self._migrate_toml_exam_weeks()
            self._migrate_toml_special_dates()

    def _materialize_anniversaries(self):
        """① 与 api 首写物化共用:当前内存合并视图(默认 + 用户条目)落盘。
        文件缺失/损坏 → 默认生日;迁移写入即物化(R1)。
        默认条目无 id(A16 决议)→ 合成 anniv-{date}(旧 anniversary_manager 为 uuid 风格,
        此处取确定性合成保证重建幂等)。"""
        from schedule.anniversary import DEFAULT_ANNIVERSARIES
        raw = self.anniversary_mgr.visible_items()
        if not raw:
            raw = list(DEFAULT_ANNIVERSARIES)
        items = []
        for a in raw:
            it = dict(a) if isinstance(a, dict) else {
                "id": a.id, "type": a.type, "name": a.name, "date": a.date,
                "note": a.note, "created_at": a.created_at}
            if "id" not in it:
                it = dict(it, id=f"anniv-{it['date']}")
            items.append(it)
        self.anniversary_mgr._items = [anniversary.Anniversary(**it) for it in items]
        self.anniversary_mgr._save()
        self.anniversary_mgr._corrupt = False

    def _migrate_countdown(self):
        """②:直读 anniversaries.json 原始文件(不经 _load 白名单,防数据丢失,M2)。"""
        p = self.anniversary_mgr._path
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return  # 损坏已由 ① 处理(解析失败落入 ① 重建,F3)
        items = raw.get("anniversaries", [])
        migrated = 0
        for it in items:
            if it.get("type") == "countdown":
                try:
                    date.fromisoformat(it["date"])
                except (KeyError, ValueError):
                    continue
                self.apply_override({"kind": "reminder", "date": it["date"],
                                     "label": it.get("name", ""), "note": "from countdown"},
                                    _from_migration=True)
                migrated += 1
        if migrated:
            kept = [it for it in items if it.get("type") != "countdown"]
            p.write_text(json.dumps({"anniversaries": kept}, ensure_ascii=False, indent=2))
            self.anniversary_mgr._load()

    def _migrate_toml_exam_weeks(self):
        """③(Task 10 激活):toml exam_weeks → override,label="from toml 考试周"。"""
        sched = self.config.get("schedule", {})
        for r in sched.get("exam_weeks", []) or []:
            parts = [x.strip() for x in str(r).split(",")]
            if len(parts) != 2:
                continue
            try:
                s, e = date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
            except ValueError:
                continue
            self.apply_override({"kind": "exam_week", "date": s.isoformat(),
                                 "end_date": e.isoformat(), "label": "from toml 考试周"},
                                _from_migration=True)

    def _migrate_toml_special_dates(self):
        """④(Task 10 激活):toml special_dates → 纪念日(迟菓生日为默认,其余 name="特殊日期 MM-DD")。"""
        sched = self.config.get("schedule", {})
        for mmdd in sched.get("special_dates", []) or []:
            if mmdd == "05-11":
                continue  # 默认生日(代码内置,物化时已含)
            if any(a.get("date") == mmdd and a.get("note") == "from toml"
                   for a in self.anniversary_mgr.visible_items()):
                continue  # 幂等:④ 重复执行不重复合并
            self.anniversary_mgr.add("anniversary", f"特殊日期 {mmdd}", mmdd, note="from toml")

    # ── when 换算(完整七形态;形态约束先于学期边界,保证单日 kind 收区间 → shape_mismatch 与学期状态无关)──

    def _semester_dates(self) -> tuple[date, date | None]:
        sched = self.config.get("schedule", {})
        try:
            ss = date.fromisoformat(str(sched.get("semester_start", "")))
        except ValueError:
            ss = date(2026, 2, 23)
        se = None
        try:
            if sched.get("semester_end"):
                se = date.fromisoformat(str(sched["semester_end"]))
        except ValueError:
            pass
        return ss, se

    def apply_override(self, item: dict, _from_migration: bool = False) -> dict:
        """协议形态 item → resolve_when 全令牌换算 → 形态约束 → 学期边界 → 分端点过去校验
        → to_date 五态 → move 源槽/快照 → 幂等写 → 写后清理 → 确认文案。
        兼容批 2b 旧协议显式 date/end_date(顶层或 when 两键),Task 6 七形态全收。"""
        if not isinstance(item, dict):
            raise ApiRejection("invalid_value", "item 非 dict")
        kind = item.get("kind")
        if kind == "remove":
            raise ApiRejection("invalid_value", "apply_override 拒绝 kind=remove(路由 remove_override)")
        if kind not in ("cancel", "move", "add", "exam_week", "reminder"):
            raise ApiRejection("invalid_value", f"未知 kind: {kind!r}")
        unknown = set(item) - {"kind", "when", "date", "end_date", "period", "to_period",
                               "to_date", "course", "label", "note"}
        if unknown:
            raise ApiRejection("invalid_value", f"未知字段: {sorted(unknown)}")
        if not _from_migration:
            self._guard()
        today = self.today
        semester_start, semester_end = self._semester_dates()
        when = item.get("when")
        entry = {k: v for k, v in item.items() if k not in ("when",) and v is not None}
        if when is None:
            if "date" not in item:
                raise ApiRejection("ambiguous", "when/date 缺失 → 歧义拒绝")
            when = {"date": item["date"]}   # 批 2b 旧协议顶层 date 形态 → 归一进 when 管线(start/end/过去校验全走)
        if when is not None:
            if not isinstance(when, dict):
                raise ApiRejection("invalid_value", "when 非 dict")
            try:
                if set(when) == {"date", "end_date"}:
                    start, end = resolve_when({"date": when["date"]}, today, semester_start)
                    entry["end_date"] = when["end_date"]   # 批 2b 旧协议区间(显式 end_date 保持)
                    is_interval = True
                else:
                    start, end = resolve_when(when, today, semester_start)
                    is_interval = ("week_offset" in when and "weekday" not in when) \
                        or set(when) == {"start", "end"}
            except ResolveReject as e:
                raise ApiRejection(e.category, str(e))
            # ── 形态约束(§4.2 C3/F1/F-C)──
            if kind in ("reminder", "move") and is_interval:
                raise ApiRejection("shape_mismatch", f"{kind} 不收区间形态(单日 kind)")
            # ── 学期边界(二十轮对称化):week_offset 非 cancel 类 → 学期前/目标周超学期周数拒绝 ──
            if "week_offset" in when and kind in ("reminder", "add", "exam_week", "move"):
                if today < semester_start:
                    raise ApiRejection("before_semester", "学期前 week_offset 语义失效")
                if semester_end and week_number(start, semester_start) > week_number(semester_end, semester_start):
                    raise ApiRejection("after_semester", "目标周超出学期周数")
            # ── 字段落点(§4.2 F-B):(start,end) 落 date/end_date,不落 to_date ──
            if kind in ("cancel", "add"):
                entry["date"] = start.isoformat()
                if is_interval and entry.get("end_date") is None:
                    entry["end_date"] = end.isoformat()
            elif kind == "exam_week":
                entry["date"] = start.isoformat()
                if entry.get("end_date") is None:
                    entry["end_date"] = end.isoformat()   # 单日 → 退化 date=end_date(F-D)
            else:  # reminder / move
                entry["date"] = start.isoformat()
            # ── 过去日期分端点校验(L2/C3/F1):课程例外与区间事实查 end;单日查 date ──
            if kind in ("cancel", "add", "exam_week"):
                try:
                    check = date.fromisoformat(entry.get("end_date") or end.isoformat())
                except ValueError:
                    raise ApiRejection("invalid_value", f"end_date 非法: {entry.get('end_date')!r}")
                if check < today:
                    raise ApiRejection("past_date", f"结果 {check} < today")
            elif kind == "reminder":
                if start < today:
                    raise ApiRejection("past_date", f"结果 {start} < today")
            # move:源日可为过去(快照派生语义;过去性由 to_date 检查与写后清理链路约束)
        # ── to_date(move 独立字段;五态单日形态,不收 week_offset 单/start-end,C2/M4)──
        if item.get("to_date") is not None:
            if kind != "move":
                raise ApiRejection("shape_mismatch", "to_date 仅 move 可用")
            td = item["to_date"]
            if isinstance(td, dict):
                if set(td) == {"week_offset"} or set(td) == {"start", "end"} or not td:
                    raise ApiRejection("shape_mismatch", "to_date 不收 week_offset 单/start-end")
                try:
                    ts, te = resolve_when(td, today, semester_start)
                except ResolveReject as e:
                    raise ApiRejection(e.category, str(e))
                if ts != te:
                    raise ApiRejection("shape_mismatch", "to_date 必须为单日")
            else:
                try:
                    ts = te = date.fromisoformat(str(td))
                except ValueError:
                    raise ApiRejection("invalid_value", f"to_date 非法: {td!r}")
            if ts < today:
                raise ApiRejection("past_date", "to_date 已过去(查 date 语义)")
            if ts < date.fromisoformat(entry["date"]):
                raise ApiRejection("shape_mismatch", "to_date < date(倒序调课)")
            entry["to_date"] = ts.isoformat()
        if kind == "move":
            if (entry.get("to_date") == entry.get("date")
                    and item.get("to_period") == item.get("period")):
                raise ApiRejection("shape_mismatch", "to_date==date 且 to_period==period(无变化)")
            if entry.get("period") is not None:
                src_course = self._move_source_course(entry)   # 参照系 = 基底课表 + 已应用 add 例外
                if src_course is None:
                    raise ApiRejection("no_source_class", "move 源槽无课")
                if not entry.get("course"):
                    entry["course"] = {k: v for k, v in src_course.items()
                                       if k in ("course", "teacher", "weeks", "weeks_raw",
                                                "location", "alternates")}
        # ── move/add 课程快照 weeks 派生(M7):weeks=[当日周次]/weeks_raw/alternates=[] ──
        if kind in ("move", "add") and entry.get("course"):
            c = dict(entry["course"])
            wk = week_number(date.fromisoformat(entry["date"]), semester_start)
            if isinstance(c.get("weeks"), set):
                c["weeks"] = sorted(c["weeks"])   # 内存规范形(set)→ 存储形(list)
            c.setdefault("weeks", [wk])
            c.setdefault("weeks_raw", f"第{wk}周")
            alts = []
            for a in (c.get("alternates") or []):
                a = dict(a)
                if isinstance(a.get("weeks"), set):
                    a["weeks"] = sorted(a["weeks"])
                alts.append(a)
            c["alternates"] = alts
            entry["course"] = c
        try:
            e2, replaced = self.overrides.add(entry, datetime.now(CST))
        except OverrideError as ex:
            raise ApiRejection("invalid_value", str(ex))
        self.overrides.cleanup(today)
        return {"ok": True, "action": "schedule_change", "replaced": replaced,
                "item": e2, "text": build_confirmation(e2)}

    def _move_source_course(self, entry: dict) -> dict | None:
        """move 源槽课程:基底课表(week_courses)或已应用 add 例外;None = 源槽无课。"""
        from schedule.sources import load_sources
        from schedule.day_plan import week_courses
        src = load_sources(self.base_dir, self.config)
        d = date.fromisoformat(entry["date"])
        w = week_number(d, src.semester_start)
        base = week_courses(src.schedule, src.semester_start, w).get(d.weekday(), {})
        if entry["period"] in base:
            return base[entry["period"]]
        for ov in src.overrides.for_date(d):
            if ov["kind"] == "add" and ov.get("period") == entry["period"]:
                return ov.get("course")
        return None

    def remove_override(self, match: dict) -> dict:
        """match 三选一 {id} | {date,period} | {date,label},不组合(LOW);
        match.date 为 when 令牌(单日形态,复用 resolve_when,F7);区间形态 → shape_mismatch。"""
        self._guard()
        if not isinstance(match, dict) or not match:
            raise ApiRejection("invalid_value", "match 非 dict 或为空")
        if set(match) - {"id", "date", "period", "label"}:
            raise ApiRejection("shape_mismatch", "match 必须为 {id} | {date,period} | {date,label} 之一")
        if "id" in match:
            if len(match) != 1:
                raise ApiRejection("shape_mismatch", "match 三选一,不组合")
            cond = {"id": match["id"]}
        else:
            if "date" not in match or not (("period" in match) ^ ("label" in match)):
                raise ApiRejection("shape_mismatch", "match 必须为 {date,period} | {date,label}")
            dt = match["date"]
            if isinstance(dt, dict):
                if not dt or set(dt) == {"week_offset"} or set(dt) == {"start", "end"} \
                        or set(dt) - {"date", "days", "weekday", "week_offset"}:
                    raise ApiRejection("shape_mismatch", "match 日期只收单日形态(F7:区间形态拒绝)")
                try:
                    s, e = resolve_when(dt, self.today, self._semester_dates()[0])
                except ResolveReject as ex:
                    raise ApiRejection(ex.category, str(ex))
                if s != e:
                    raise ApiRejection("shape_mismatch", "match 日期必须为单日")
                cond = {"date": s.isoformat()}
            elif isinstance(dt, str):
                cond = {"date": dt}
            else:
                raise ApiRejection("invalid_value", "match.date 非法")
            cond.update({"period": match["period"]} if "period" in match else {"label": match["label"]})
        ok = self.overrides.remove_exact(cond, self.today)
        if not ok:
            raise ApiRejection("not_found", f"零匹配: {match!r}")
        return {"ok": True, "action": "schedule_change", "removed": cond,
                "text": "好,那条安排取消掉了,记下了。"}

    # ── 纪念日/寒暑假(批 5 daemon 改调,本批给直通)──

    def add_anniversary(self, type_, name, date_str, note=""):
        self._guard()
        a = self.anniversary_mgr.add(type_, name, date_str, note=note)
        return {"action": "anniversary_added", "ok": True, "id": a.id, "name": a.name,
                "date": a.date, "type": a.type}

    def remove_anniversary(self, id_):
        self._guard()
        return {"action": "anniversary_removed", "ok": self.anniversary_mgr.remove(id_)}

    def list_anniversaries(self):
        return {"action": "anniversary_list", "anniversaries": [
            {"id": a.id, "type": a.type, "name": a.name, "date": a.date,
             "note": a.note, "created_at": a.created_at}
            for a in self.anniversary_mgr.list_all()], "count": len(self.anniversary_mgr.list_all())}

    def update_anniversary(self, id_, **kwargs):
        self._guard()
        a = self.anniversary_mgr.update(id_, **kwargs)
        return {"action": "anniversary_updated", "ok": a is not None,
                "anniversary": {"id": a.id, "type": a.type, "name": a.name, "date": a.date} if a else None}

    # set_break 落 Task 11(批 5):daemon --break 处理器整体迁入(行为保持现 --break 语义,
    # 写路径归 api 唯一写口,§3.1/§6)
