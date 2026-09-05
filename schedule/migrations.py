# schedule/migrations.py — 迁移/物化逻辑(从 api.py 剥离,Issue #398)
# 调用入口:ScheduleApi._guard() → self._ensure_migrations()

import json
import sys
from datetime import date

from chiguo_atomic import atomic_write


def ensure_migrations(api) -> None:
    """五子项全序(spec §3.1 M2/N3/F3,二十轮补 0):
    0. overrides 损坏重建 → ①. anniversaries 损坏重建 → ②. countdown→reminder(6c 激活)
    → ③. toml exam_weeks(批 4 激活) → ④. toml special_dates 合并(批 4 激活)。
    0 必须先于 ②:② 写 overrides,若重建在其后,刚迁入的 reminder 被空文件抹掉。"""
    # 0. overrides 损坏 → 重建(非 0 字节,二十轮 LOW 钉死)
    #   · 混合场景(坏/好条目共存):_load 已剔除坏条目、好条目保留在 _items →
    #     直接落盘保留(issue #308,禁止整集清空丢用户 override/reminder)
    #   · 整文件损坏场景:_load 解析失败 _items=[] → 写合法空文件(行为保持)
    if api.overrides.corrupt:
        n = len(api.overrides._items)
        api.overrides._save()
        api.overrides._corrupt = False
        print(f"[schedule.api] overrides 已重建({n} 条保留): {api.overrides.path}",
              file=sys.stderr)
    # ①. anniversaries 损坏 → 重建为默认生日(视同缺失路径,N1)
    if api.anniversary_mgr._corrupt or not api.anniversary_mgr._path.exists():
        _materialize_anniversaries(api)
    # ②. countdown→reminder 防御迁移(6c 激活;幂等 label+date 去重,F4/N3)
    _migrate_countdown(api)
    # ③④. toml 一次性迁移(批 4 激活,见 Task 10)
    _migrate_toml_exam_weeks(api)
    _migrate_toml_special_dates(api)


def _materialize_anniversaries(api):
    """① 与 api 首写物化共用:当前内存合并视图(默认 + 用户条目)落盘。
    文件缺失/损坏 → 默认生日;迁移写入即物化(R1)。
    默认条目无 id(A16 决议)→ 合成 anniv-{date}(旧 anniversary_manager 为 uuid 风格,
    此处取确定性合成保证重建幂等)。"""
    from schedule.anniversary import DEFAULT_ANNIVERSARIES, Anniversary
    raw = api.anniversary_mgr.visible_items()
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
    api.anniversary_mgr._items = [Anniversary(**it) for it in items]
    api.anniversary_mgr._save()
    api.anniversary_mgr._corrupt = False


def _migrate_countdown(api):
    """②:直读 anniversaries.json 原始文件(不经 _load 白名单,防数据丢失,M2)。"""
    p = api.anniversary_mgr._path
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
            api.apply_override({"kind": "reminder", "date": it["date"],
                                 "label": it.get("name", ""), "note": "from countdown"},
                                _from_migration=True)
            migrated += 1
    if migrated:
        kept = [it for it in items if it.get("type") != "countdown"]
        # M-9: 原子写(tmp + os.replace + 0600,Q23 收敛至共享 atomic_write),
        # 防迁移中途崩溃写坏正式文件丢纪念日。
        atomic_write(p, json.dumps({"anniversaries": kept}, ensure_ascii=False, indent=2),
                     mode=0o600)
        api.anniversary_mgr._load()


def _migrate_toml_exam_weeks(api):
    """③(Task 10 激活):toml exam_weeks → override,label="from toml 考试周"。"""
    sched = api.config.get("schedule", {})
    migrated = 0
    for r in sched.get("exam_weeks", []) or []:
        parts = [x.strip() for x in str(r).split(",")]
        if len(parts) == 1:
            try:
                s = date.fromisoformat(parts[0])
            except ValueError:
                continue
            e = s                              # F14:单日期条目 → 单日退化
        elif len(parts) == 2:
            try:
                s, e = date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
            except ValueError:
                continue
        else:
            continue
        api.apply_override({"kind": "exam_week", "date": s.isoformat(),
                             "end_date": e.isoformat(), "label": "from toml 考试周"},
                            _from_migration=True)
        migrated += 1
    if migrated:
        print(f"[schedule.api] toml exam_weeks 已迁移 {migrated} 条", file=sys.stderr)


def _migrate_toml_special_dates(api):
    """④(Task 10 激活):toml special_dates → 纪念日(迟菓生日为默认,其余 name="特殊日期 MM-DD")。"""
    sched = api.config.get("schedule", {})
    added = 0
    for mmdd in sched.get("special_dates", []) or []:
        if mmdd == "05-11":
            continue  # 默认生日(代码内置,物化时已含)
        if any(a.get("date") == mmdd and a.get("note") == "from toml"
               for a in api.anniversary_mgr.visible_items()):
            continue  # 幂等:④ 重复执行不重复合并
        api.anniversary_mgr.add("anniversary", f"特殊日期 {mmdd}", mmdd, note="from toml")
        added += 1
    if added:
        print(f"[schedule.api] toml special_dates 已迁移 {added} 条", file=sys.stderr)