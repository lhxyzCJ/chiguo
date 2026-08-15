# ============================================================
# schedule/replan.py — 重分析链路(§7):读来源 → agent 分析 → 校验 → 原子写 plan
# 唯一写 plan 的模块;引擎永不写 plan;replan 永不写来源。
# ============================================================

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from schedule.sources import load_sources
from schedule.plan_store import PlanStore

from trigger_types import TriggerType, REPLAN_SCALE_KEYS

CST = timezone(timedelta(hours=8))
DIRTY_FILES = ("schedule_overrides.json", "holidays.json")   # mtime 文件集合(仅此二者,F12)
# 合法 trigger_scale 类型 = 枚举全部真实触发类型 + default（默认缩放键），单一事实来源见 trigger_types.py。
TRIGGER_TYPES = sorted(REPLAN_SCALE_KEYS)   # 含 comfort（Q3）；default 为缺席类型缩放键
MAX_MODIFIERS = 20
MAX_FIELD_LEN = 100
MAX_ITEM_BYTES = 4096


def check_dirty(base_dir: str, config: dict) -> bool:
    ps = PlanStore(base_dir)
    if not ps.exists():
        return True
    gen = ps.generated_at()
    if not gen:
        return True
    try:
        gen_ts = datetime.fromisoformat(gen).timestamp()
    except ValueError:
        return True
    for name in DIRTY_FILES:
        p = Path(base_dir) / name
        if p.exists() and p.stat().st_mtime > gen_ts:
            return True
    return False


def should_skip(sources, today: date | None = None) -> bool:
    """跳过条件(C4/F12):无区间事实且无当年日历的节假日。
    R1:按当年存在性判定——节假日区间 start/end 年份 == today.year 才构成"当年日历";
    2026 内嵌节假日对 2027 不构成日历,写空清悬挂持续有效。"""
    if sources.overrides.intervals():
        return False
    today = today or datetime.now(CST).date()
    for _name, (s, e) in sources.holiday.all_ranges().items():
        if s.year == today.year or e.year == today.year:
            return False
    return True


def validate_plan(plan: dict, sources=None) -> list[str]:
    """错误清单(全部命中才返回错误;空清单 = 通过)。ref 存在性/类型名/clamp/上限/字段。
    R3:ref 前缀检查恒跑;ref 存在性核对仅 sources 提供时执行(单参调用只做结构检查)。"""
    errs = []
    if not isinstance(plan, dict):
        return ["plan 非 dict"]   # 结构崩溃防御:非法输入直接拒绝
    mods = plan.get("modifiers", [])
    if not isinstance(mods, list):
        return ["modifiers 非 list"]
    if len(mods) > MAX_MODIFIERS:
        errs.append(f"modifiers > {MAX_MODIFIERS}")
    for m in mods:
        if not isinstance(m, dict) or set(m) - {"ref", "trigger_scale"}:
            errs.append(f"modifier 未知字段: {m}")
            continue
        ref = m.get("ref", "")
        if len(ref) > MAX_FIELD_LEN:
            errs.append(f"ref 超长(>{MAX_FIELD_LEN}): {ref[:20]}...")
        if not (ref.startswith("fact:") or ref.startswith("holiday:")):
            errs.append(f"ref 前缀未知: {ref}")
        elif sources is not None:
            if ref.startswith("fact:"):
                it = sources.overrides.by_id(ref[5:])
                if it is None or it["kind"] != "exam_week":
                    errs.append(f"ref 未知/不合格: {ref}")
            elif sources.holiday.range_of(ref[len("holiday:"):]) is None:
                errs.append(f"ref 未知: {ref}")
        ts = m.get("trigger_scale", {})
        if not isinstance(ts, dict):
            errs.append(f"trigger_scale 非 dict: {m.get('ref','')}")
            continue
        for k, v in ts.items():
            if len(str(k)) > MAX_FIELD_LEN:
                errs.append(f"类型名超长(>{MAX_FIELD_LEN}): {k[:20]}...")
            if k not in REPLAN_SCALE_KEYS:
                errs.append(f"未知类型名: {k}")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.1 <= float(v) <= 10:
                errs.append(f"trigger_scale clamp 越界: {k}={v}")
        if len(json.dumps(m, ensure_ascii=False).encode()) > MAX_ITEM_BYTES:
            errs.append(f"modifier JSON 总长 > 4KB: {ref}")
    return errs


def sanitize_plan(plan: dict, sources=None) -> tuple[list, list]:
    """校验失败降级(Q3 #265)：剔非法 modifier/trigger_scale key，保留合法部分。
    返回 (合法 modifiers 列表, 告警列表)。plan 非 dict / modifiers 非 list → 无可挽救部分。
    逐条剔除：非法字段/非法 ref/非 dict trigger_scale/超长/超限 clamp/越界值/总长超限。
    对合法条目仅剔除非法 scale key，其余 scale 保留。"""
    warns = []
    if not isinstance(plan, dict):
        return [], ["plan 非 dict,无可挽救"]
    mods = plan.get("modifiers", [])
    if not isinstance(mods, list):
        return [], ["modifiers 非 list,无可挽救"]
    kept = []
    for m in mods:
        if not isinstance(m, dict) or set(m) - {"ref", "trigger_scale"}:
            warns.append(f"剔 modifier 未知字段: {m if not isinstance(m, dict) else list(m)}")
            continue
        ref = m.get("ref", "")
        if len(ref) > MAX_FIELD_LEN:
            warns.append(f"剔 ref 超长: {ref[:20]}...")
            continue
        if not (ref.startswith("fact:") or ref.startswith("holiday:")):
            warns.append(f"剔 ref 前缀未知: {ref}")
            continue
        if sources is not None:
            if ref.startswith("fact:"):
                it = sources.overrides.by_id(ref[5:])
                if it is None or it["kind"] != "exam_week":
                    warns.append(f"剔 ref 未知/不合格: {ref}")
                    continue
            elif sources.holiday.range_of(ref[len("holiday:"):]) is None:
                warns.append(f"剔 ref 未知: {ref}")
                continue
        ts = m.get("trigger_scale", {})
        if not isinstance(ts, dict):
            warns.append(f"剔 trigger_scale 非 dict: {ref}")
            continue
        clean_ts = {}
        for k, v in ts.items():
            if len(str(k)) > MAX_FIELD_LEN:
                warns.append(f"剔类型名超长: {k[:20]}...")
                continue
            if k not in REPLAN_SCALE_KEYS:
                warns.append(f"剔未知类型名: {k}")
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.1 <= float(v) <= 10:
                warns.append(f"剔 clamp 越界: {k}={v}")
                continue
            clean_ts[k] = v
        clean_m = {"ref": ref}
        if clean_ts:
            clean_m["trigger_scale"] = clean_ts
        if len(json.dumps(clean_m, ensure_ascii=False).encode()) > MAX_ITEM_BYTES:
            warns.append(f"剔 modifier 总长 > 4KB: {ref}")
            continue
        kept.append(clean_m)
    if len(kept) > MAX_MODIFIERS:
        warns.append(f"modifiers > {MAX_MODIFIERS},截断保留前 {MAX_MODIFIERS} 条")
        kept = kept[:MAX_MODIFIERS]
    return kept, warns


def _lock(base_dir: str) -> bool:
    """lockfile:5s 超时退出让位;陈旧锁(mtime > 10min)强制接管(M15)。"""
    lock = Path(base_dir) / "replan.lock"
    deadline = time.time() + 5
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue
            if age > 600:
                lock.unlink(missing_ok=True)
                continue
            if time.time() >= deadline:
                print("[schedule.replan] lockfile 超时,让位", file=sys.stderr)
                return False
            time.sleep(0.1)


def replan_env(base: dict | None = None) -> dict:
    """replan 的 pi 子进程环境:注入独立 thinking 档位与超时,避免 [host].thinking_level=max
    拖垮调用(生产机实机 2 核 VPS 实测 max 单次 ~115s+,120s 必超时)。
    优先级:CHIGUO_REPLAN_THINKING(专用) > 环境 AGENTRUN_THINKING(显式) > 默认 high;
    AGENTRUN_TIMEOUT 与 replan_timeout() 同步(agent-run.mjs 内层超时读该变量)。"""
    env = dict(os.environ if base is None else base)
    env["AGENTRUN_THINKING"] = (env.get("CHIGUO_REPLAN_THINKING")
                             or env.get("AGENTRUN_THINKING") or "high")
    env["AGENTRUN_TIMEOUT"] = str(replan_timeout(env))
    return env


def replan_timeout(env: dict | None = None) -> int:
    """agent 调用超时(秒):默认 240(旧 120 在 thinking>high 时不够),CHIGUO_REPLAN_TIMEOUT 可覆盖,
    非法值/过小值兜底。"""
    env = os.environ if env is None else env
    try:
        return max(60, int(env.get("CHIGUO_REPLAN_TIMEOUT", 240)))
    except (TypeError, ValueError):
        return 240


def _run_replan(base_dir: str, config: dict, sources) -> dict | None:
    """agent 分析(facts + 类型清单 + clamp 边界),超时默认 240s;失败 → None(保留旧 plan + stale)。"""
    facts = [{"ref": f"fact:{it['id']}", "start": it["date"], "end": it.get("end_date") or it["date"],
              "label": it.get("label", "")} for it in sources.overrides.intervals()]
    for name, (s, e) in sources.holiday.all_ranges().items():
        facts.append({"ref": f"holiday:{name}", "start": s.isoformat(), "end": e.isoformat(), "label": name})
    prompt = json.dumps({
        "facts": facts,
        "trigger_types": list(TRIGGER_TYPES),
        "clamp": [0.1, 10],
        "rule": "产出 modifiers:ref 引用上述事实 id 或 holiday:名称;trigger_scale 为类型→乘数;"
                "只写特化类型;无需要调节的类型时返回空 modifiers;禁止输出日期字段与 importance;"
                "用 `<<REPLAN>>{...}<<END>>` 包裹,块内为 {\"modifiers\": [...]}"
                "(ref+trigger_scale 对象;无需要调节时 modifiers 为空数组)。"},
        ensure_ascii=False)
    repo = str(Path(__file__).resolve().parent.parent)
    env = replan_env()
    try:
        res = subprocess.run(
            ["node", f"{repo}/scripts/agent-run.mjs", "--prompt", prompt, "--schedule-replan"],
            capture_output=True, text=True, timeout=replan_timeout(env), env=env)
    except subprocess.TimeoutExpired:
        print("[schedule.replan] pi 超时 → 按失败处理,下轮重试", file=sys.stderr)
        return None
    except (FileNotFoundError, OSError) as e:
        # node 缺失/不可执行 → 裸 FileNotFoundError 穿透会每 15 分钟刷一条 traceback(M5)
        print(f"[schedule.replan] node 执行失败({e}) → 按失败处理,保留旧 plan,下轮重试",
              file=sys.stderr)
        return None
    try:
        out = json.loads(res.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not out.get("ok"):
        print(f"[schedule.replan] agent 失败: {out.get('error', '')[:200]}", file=sys.stderr)
        return None
    # agent-run.mjs replan 分支返回 {ok, parsed, raw}:parsed = <<REPLAN>> 块内容(plan dict)
    return out.get("parsed")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="schedule replan (--check)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "chiguo_proactive.toml"))
    args = ap.parse_args(argv)
    config_path = Path(args.config)
    with open(config_path, "rb") as f:
        import tomllib
        config = tomllib.load(f)
    base_dir = str(config_path.resolve().parent)
    if not _lock(base_dir):
        return 0
    try:
        sources = load_sources(base_dir, config)
        snap = {n: (Path(base_dir) / n).stat().st_mtime for n in DIRTY_FILES if (Path(base_dir) / n).exists()}
        if not check_dirty(base_dir, config):
            print("[schedule.replan] 不脏,零成本退出")
            return 0
        ps = PlanStore(base_dir)
        if should_skip(sources):
            cur = ps.load()
            if cur is None or cur.get("modifiers"):
                ps.save({"plan_version": 1, "generated_at": datetime.now(CST).isoformat(), "modifiers": []})
                print("[schedule.replan] 无区间事实且无当年节假日 → 写空(清悬挂/初始化)")
            else:
                print("[schedule.replan] plan 已空,保留不写")
            return 0
        plan = _run_replan(base_dir, config, sources)
        if plan is None:
            print("[schedule.replan] agent 失败 → 保留旧 plan + stale,下轮重试", file=sys.stderr)
            return 1
        errs = validate_plan(plan, sources)
        if errs:
            # Q3 (#265): 校验失败不再丢整份 plan —— 剔非法 key/条目,保留合法部分 + 告警。
            kept, warns = sanitize_plan(plan, sources)
            for w in warns:
                print(f"[schedule.replan] 剔非法部分: {w}", file=sys.stderr)
            if warns:
                print(f"[schedule.replan] 校验{len(errs)} 处失败,已剔除非法部分"
                      f"({len(kept)}/{len(plan.get('modifiers', []))} modifiers 保留)", file=sys.stderr)
            plan["modifiers"] = kept
        # TOCTOU 防护(M14/F2):写盘前重查来源 mtime 逐文件比对快照;变化 → 本轮放弃
        for n, snap_m in snap.items():
            p = Path(base_dir) / n
            if p.exists() and p.stat().st_mtime != snap_m:
                print(f"[schedule.replan] 来源 mtime 已变({n})→ 本轮放弃,下轮重试", file=sys.stderr)
                return 1
        plan.setdefault("plan_version", 1)
        plan["generated_at"] = datetime.now(CST).isoformat()
        ps.save(plan)
        print(f"[schedule.replan] plan 已写入({len(plan.get('modifiers', []))} modifiers)")
        return 0
    finally:
        (Path(base_dir) / "replan.lock").unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
