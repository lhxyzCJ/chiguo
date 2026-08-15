"""cli.dispatch — daemon 入口分发（拆自 chiguo_daemon.py main()）。

对外 CLI 行为完全不变：35 参数解析（见 cli.parser）、子命令分发顺序、
JSON 输出形状、exit code 语义与拆分前逐字一致。子命令分优先级：
  纪念日 → rotate → record_send → send_result → 对话/导出 → break → tune →
  consolidate → 监控 → health → 轻量子命令 → status → user_msg → loop → 默认单次评估
"""
import sys
import json
import os
import time
import fcntl
from datetime import datetime, timezone, timedelta
from pathlib import Path

from cli.parser import build_parser
from cli.commands import (_load_light_config, _cmd_attention,
                          _cmd_schedule_recall, _cmd_schedule_change, _cmd_memory_search)
from runner.loop import run_loop
from chiguo_version import VERSION

CST = timezone(timedelta(hours=8))


def parse_args(argv=None):
    """解析命令行参数（35 参数）。供参数快照测试与 main 共用。"""
    parser = build_parser()
    return parser, parser.parse_args(argv)


# ── loop/cron 双形态运行期互斥守卫（Q28）─────────────────────────
# loop 常驻（--loop）与 cron tick（scripts/chiguo-tick.sh）都会走主动发送链，
# 若同时存活会双发消息。deploy 层（install_agent.sh 阶段 6）只在切换形态时做一次互斥，
# 运行期再补一道自防锁互认：各自的自我防护锁能让对方识别 ——
#   * loop 形态的自我防护锁 =  chiguo_loop.pid（已有 PID 双开锁，cron 侧据此识别）
#   * cron 形态的自我防护锁 =  chiguo-tick.lock 的 flock（R16 tick 重入锁，loop 侧据此识别）
def _cron_tick_lock_path() -> Path:
    """chiguo-tick.sh 的并发 flock 锁文件路径（R16：$CHIGUO_LOCK_DIR 或 ~/.chiguo/run）。"""
    lock_dir = os.environ.get("CHIGUO_LOCK_DIR") or os.path.join(
        os.path.expanduser("~"), ".chiguo", "run")
    return Path(lock_dir) / "chiguo-tick.lock"


def cron_form_active() -> bool:
    """cron 形态是否此刻在跑：chiguo-tick.sh 是否持有 chiguo-tick.lock flock。

    持锁时 flock(LOCK_EX|LOCK_NB) 抛 BlockingIOError → 判定 cron 活跃；
    锁文件缺失 / 能拿到锁 → cron 未在跑。非阻塞单次探测，不会等待。
    """
    lock_path = _cron_tick_lock_path()
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False  # 锁文件不存在 → cron 未运行
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def loop_form_active(base_dir: str) -> bool:
    """loop 形态是否在跑：chiguo_loop.pid 记录的进程存活（过期 pid 视为未运行）。"""
    pid_path = Path(base_dir) / "chiguo_loop.pid"
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False  # 进程已退出 → 过期 pid，视为未运行
    return True


def guard_mutual_form(base_dir: str, form: str) -> str | None:
    """loop/cron 双形态互斥守卫（Q28）。返回冲突描述，无冲突返回 None。

    form = "loop" | "cron"：
      loop 启动时检测 cron（chiguo-tick flock）是否在跑；
      cron 单次主动评估时检测 loop（chiguo_loop.pid）是否在跑。
    """
    if form == "loop":
        if cron_form_active():
            return "cron 形态（chiguo-tick）正在运行"
        return None
    if form == "cron":
        if loop_form_active(base_dir):
            return "loop 形态（chiguo-daemon --loop）正在运行"
        return None
    raise ValueError(f"未知形态: {form!r}")


def startup_conflict(base_dir: str, form: str) -> int:
    """启动防线：冲突则打 stderr 诊断并返回区分语义的哨兵码，调用方据此分流。

    返回语义（Q28 P0 修复，区分「拒启」与「跳过」两种冲突）：
      0 = 无冲突，放行；
      1 = loop 冲突，拒绝启动常驻（exit 1）；
      2 = cron 冲突，跳过本 tick 单次主动评估（exit 0，不输出决策，不拖垮健康检查）。
    """
    conflict = guard_mutual_form(base_dir, form)
    if conflict is None:
        return 0
    if form == "loop":
        print(f"[chiguo_daemon] {conflict}，拒绝启动 loop 形态（防双发送）",
              file=sys.stderr)
        return 1
    print(f"[chiguo_daemon] {conflict}，跳过本次单次主动评估（防双发送）",
          file=sys.stderr)
    return 2


def _run_passive(engine, compact: bool) -> None:
    """cron 形态的单次主动评估入口（--compact 亦经此）。

    Q28 P0 修复：评估前先查 loop 冲突。startup_conflict 返回 2（cron 冲突=跳过）
    时直接 sys.exit(0)——跳过本 tick、**不调用 engine.evaluate()、不输出任何决策
    JSON**，防止 loop 与 cron 双发送，同时以 0 退出不拖垮 cron 健康检查。
    """
    rc = startup_conflict(str(engine._base_dir), "cron")
    if rc == 2:
        sys.exit(0)  # cron 冲突：跳过本 tick（与 loop 冲突 exit 1 语义区分）
    if rc != 0:
        sys.exit(rc)  # 防御：未知非零哨兵照常退出

    decision = engine.evaluate()
    if compact and decision["action"] == "idle":
        # 紧凑模式 idle 时输出最小 heartbeat（用于 cron 健康检查）
        print(json.dumps({"action": "idle", "version": VERSION,
                          "time": datetime.now(CST).isoformat()},
                         ensure_ascii=False))
        return
    print(json.dumps(decision, ensure_ascii=False, indent=2))


def run(args):
    """分发逻辑（不自行 parse_args；参数校验 + 各子命令分支）。"""
    # ── 参数校验 ──
    if args.loop is not None and args.loop < 60:
        print("[chiguo_daemon] interval < 60, using 60", file=sys.stderr)
    # v8: --ack 是告警确认参数，自动联动开启 alerts 处理（不再静默忽略）
    if args.ack and not args.alerts:
        print("[chiguo_daemon] --ack 需要 --alerts，已自动联动开启", file=sys.stderr)
        args.alerts = True

    # ── 纪念日 CRUD（独立分支，不影响决策引擎；批 5 改调 ScheduleApi） ──
    if args.anniversary:
        from decision.engine import DecisionEngine
        from schedule.api import ScheduleApi
        cfg = _load_light_config()
        api = ScheduleApi(cfg["_base_dir"], cfg)
        parts = args.anniversary.split()
        cmd = parts[0] if parts else ""
        try:
            if cmd == "add" and len(parts) >= 4:
                result = api.add_anniversary(parts[1], " ".join(parts[3:]), parts[2])
            elif cmd == "remove" and len(parts) >= 2:
                result = api.remove_anniversary(parts[1])
            elif cmd == "list":
                result = api.list_anniversaries()
            elif cmd == "update" and len(parts) >= 3:
                kwargs = {}
                for kv in parts[2:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        kwargs[k] = v
                result = api.update_anniversary(parts[1], **kwargs)
            else:
                result = {"error": f"未知子命令: {args.anniversary}",
                          "usage": "add anniversary <DATE> <NAME> / remove <ID> / list / update <ID> key=val"}
        except ValueError as e:
            result = {"action": "anniversary_added", "ok": False, "error": str(e)} \
                if cmd == "add" else {"action": "anniversary_updated", "ok": False, "error": str(e)}
        print(json.dumps(result, ensure_ascii=False))
        if result.get("error") or result.get("ok") is False:
            sys.exit(1)
        return

    # ── v5: 日志轮转（v8: 锚定 base_dir，从任意 cwd 运行都轮转项目文件）──
    if args.rotate:
        from decision.engine import DecisionEngine
        from chiguo_rotation import force_rotate
        engine = DecisionEngine()
        force_rotate(
            [str(engine._base_dir / "chiguo_decisions.jsonl"),
             str(engine._base_dir / "chiguo_messages.jsonl")],
            archive_dir=str(engine._base_dir / "archive"),
        )
        print(json.dumps({"action": "rotate", "ok": True}, ensure_ascii=False))
        return

    # ── v5: 记录已发送消息 ──
    if args.record_send:
        from decision.engine import DecisionEngine
        if not args.text:
            print(json.dumps({"error": "--text required with --record-send"}, ensure_ascii=False))
            sys.exit(1)
        engine = DecisionEngine()
        engine.record_send_text(args.record_send, args.text, args.trigger, args.intensity,
                                fallback=args.fallback)
        print(json.dumps({"action": "send_text_recorded", "msg_id": args.record_send, "ok": True}, ensure_ascii=False))
        return

    # ── v6: 反馈闭环──
    if args.send_result:
        from decision.engine import DecisionEngine
        if not args.send_status:
            print(json.dumps({"error": "--send-status required with --send-result"}, ensure_ascii=False))
            sys.exit(1)
        engine = DecisionEngine()
        r = engine.record_send_result(args.send_result, args.send_status, args.error)
        print(json.dumps(r, ensure_ascii=False))
        return

    # ── v5: 对话查询 & 导出（v10: 锚定 base_dir，从任意 cwd 运行都读写项目文件）──
    if args.conversation or args.conversation_days or args.export:
        from decision.engine import DecisionEngine
        from chiguo_monitor import ChiguoMonitor
        engine = DecisionEngine()
        mon = ChiguoMonitor(
            log_path=str(engine._base_dir / "chiguo_decisions.jsonl"),
            state_path=str(engine._base_dir / "chiguo_state.json"),
            break_state_path=str(engine._base_dir / "break_state.json"),
            config_path=str(engine._base_dir / "chiguo_proactive.toml"),
            messages_log_path=str(engine._base_dir / "chiguo_messages.jsonl"),
        )
        if args.export:
            result = mon.export(format=args.export)
            print(result)
        elif args.conversation_days:
            msgs = mon.conversation(days=args.conversation_days)
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        elif args.conversation:
            msgs = mon.conversation(date_str=args.conversation)
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        return

    # ── 寒暑假模式切换（批 5 改调 ScheduleApi.set_break，输出形状逐键一致） ──
    if args.break_cmd:
        from schedule.api import ScheduleApi
        cfg = _load_light_config()
        api = ScheduleApi(cfg["_base_dir"], cfg)
        result = api.set_break(args.break_cmd)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("error") or result.get("ok") is False:
            sys.exit(1)
        return

    # ── 参数校准 ──
    if args.tune:
        from decision.engine import DecisionEngine
        engine = DecisionEngine()
        latencies = engine.state.cooldown.reply_latencies
        if len(latencies) < 5:
            print(json.dumps({
                "action": "tune",
                "error": f"需要至少 5 次交互数据，当前 {len(latencies)} 次",
                "hint": "发送几条消息并等待哥哥回复，积累数据后再试",
            }, ensure_ascii=False))
        else:
            import statistics
            median_h = statistics.median(latencies)
            avg_h = sum(latencies) / len(latencies)
            # 理想回复延迟 ~0.5h（30分钟内回复说明消息受欢迎）
            # 太高 → 太频繁 → 降低 base_lambda
            # 太低 → 太冷淡 → 提高 base_lambda
            current_base = engine.config.get("poisson", {}).get("base_lambda", 0.25)
            if median_h < 0.3:
                suggestion = "increase"
                new_base = min(0.5, current_base * 1.3)
                reason = f"哥哥回复很快（中位数 {median_h:.1f}h），可以更频繁"
            elif median_h > 3.0:
                suggestion = "decrease"
                new_base = max(0.05, current_base * 0.7)
                reason = f"哥哥回复较慢（中位数 {median_h:.1f}h），减少频率"
            else:
                suggestion = "keep"
                new_base = current_base
                reason = f"回复节奏适中（中位数 {median_h:.1f}h），保持当前参数"
            print(json.dumps({
                "action": "tune",
                "latency_count": len(latencies),
                "median_hours": round(median_h, 2),
                "avg_hours": round(avg_h, 2),
                "current_base_lambda": current_base,
                "suggestion": suggestion,
                "suggested_base_lambda": round(new_base, 3),
                "reason": reason,
                "hint": f"手动修改 chiguo_proactive.toml [poisson] base_lambda = {new_base:.3f}",
            }, ensure_ascii=False, indent=2))
        return

    # ── C1: 确定性记忆巩固（零 LLM；[memory].consolidate_* 参数；停机维护专用）──
    if args.consolidate:
        from decision.engine import DecisionEngine
        sys.exit(DecisionEngine().cli_consolidate())

    # ── 监控系统（stats / alerts / monitor）（v10: 锚定 base_dir，从任意 cwd 运行都读写项目文件）──
    if args.stats is not None or args.alerts or args.monitor:
        from decision.engine import DecisionEngine
        from chiguo_monitor import ChiguoMonitor, AlertManager
        engine = DecisionEngine()
        mon = ChiguoMonitor(
            log_path=str(engine._base_dir / "chiguo_decisions.jsonl"),
            state_path=str(engine._base_dir / "chiguo_state.json"),
            break_state_path=str(engine._base_dir / "break_state.json"),
            config_path=str(engine._base_dir / "chiguo_proactive.toml"),
            messages_log_path=str(engine._base_dir / "chiguo_messages.jsonl"),
        )
        if args.alerts:
            am = AlertManager(state_path=str(engine._base_dir / "chiguo_alerts.json"))
            # --ack ALERT_ID
            if args.ack:
                ok = am.acknowledge(args.ack)
                print(json.dumps({"action": "ack", "alert_id": args.ack, "ok": ok}, ensure_ascii=False))
                return
            # ingest fresh alerts into persistent store
            fresh = mon.alerts()
            am.ingest(fresh)
            if args.alerts_all:
                result = am.list_all()
            else:
                result = am.list_active()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.monitor:
            report = mon.report(days=args.stats if args.stats is not None else 7)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            # --stats (with optional days)
            stats = mon.stats(days=args.stats)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    # ── 健康检查 ──
    if args.health:
        from decision.engine import DecisionEngine
        # v7: 基于引擎 _base_dir 锚定，不依赖 cwd
        health_engine = DecisionEngine()
        sp = health_engine.state.state_path
        healthy = False
        last_tick = None
        hours_ago = None
        error = None
        if sp.exists():
            try:
                data = json.loads(sp.read_text())
                last_tick = data.get("last_tick")
                if last_tick:
                    lt = datetime.fromisoformat(last_tick)
                    if lt.tzinfo is None:
                        lt = lt.replace(tzinfo=CST)  # naive → 补 CST，避免 aware-naive 相减 TypeError
                    hours_ago = (datetime.now(CST) - lt).total_seconds() / 3600
                    healthy = hours_ago < 6  # 6小时内有过 tick
                    if not healthy:
                        error = f"last_tick {hours_ago:.1f}h ago (threshold: 6h)"
                else:
                    error = "no last_tick in state file"
            except Exception as e:
                error = f"state file read error: {e}"
        else:
            error = "state file not found (daemon never run?)"
        print(json.dumps({
            "healthy": healthy,
            "last_tick": last_tick,
            "hours_ago": round(hours_ago, 1) if hours_ago else None,
            "error": error,
        }, ensure_ascii=False))
        return

    # ── 批 5 轻量子命令（不构造 DecisionEngine/ChiguoState，毫秒级） ──
    if args.attention:
        _cmd_attention()
        return
    if args.schedule_recall:
        _cmd_schedule_recall(args.schedule_recall)
        return
    if args.schedule_change:
        _cmd_schedule_change(args.schedule_change)
        return
    if args.memory_search:
        _cmd_memory_search(args.memory_search)
        return

    from decision.engine import DecisionEngine
    engine = DecisionEngine()

    if args.status:
        snap = engine.snapshot()
        print(json.dumps({
            "character": "迟菓",
            "time": snap["time"],
            "dominant_layer": snap["dominant_layer"],
            "emotion": snap["emotion"],
            "cooldown": snap["cooldown"],
        }, ensure_ascii=False, indent=2))
        return

    if args.user_msg_file:
        try:
            args.user_msg = Path(args.user_msg_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as e:
            print(json.dumps({"error": f"读取消息文件失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
    if args.analysis_file:
        try:
            args.analysis = Path(args.analysis_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as e:
            print(json.dumps({"error": f"读取分析文件失败: {e}"}, ensure_ascii=False))
            sys.exit(1)

    if args.user_msg:
        engine.record_user_message(args.user_msg, args.analysis, getattr(args, "recv_id", None))
        # 用户刚发消息 → 立即评估一次（情绪最新，最佳联系窗口）
        decision = engine.evaluate()
        # v1.11+R2 (review R2): --user-msg 由 bridge 在回复链中调用，其实际回复经 agent
        # 另路生成并发送；此路径 evaluate() 若命中 send 分支，booking 的状态
        # （能量/每日额度/未回复计数/Hawkes 事件）无人消费 → 幻影记账。
        # 立即按"未送达"退款回滚（复用 v6 反馈闭环），等真实发送路径再记账。
        # 回滚范围（refund_send）：energy/anxiety/messages_today/messages_without_reply/
        # Hawkes 事件/last_longing_break_at；一次性标记不回滚（接受取舍）：
        # morning_sent/night_sent/last_triggered_at/trigger_history/adapt_personality
        # 在本次 evaluate 已写入，退回后仍保留（防下次 tick 重复触发仪式/改人格）。
        if decision["action"] == "send":
            engine.record_send_result(decision.get("msg_id", ""), "failed",
                                      error="phantom_send_reply_path")
        if args.compact and decision["action"] == "idle":
            compact = {"action": "idle", "time": datetime.now(CST).isoformat()}
            if "next_evaluation_at" in decision:
                compact["next_evaluation_at"] = decision["next_evaluation_at"]
            print(json.dumps(compact, ensure_ascii=False))
        else:
            print(json.dumps(decision, ensure_ascii=False, indent=2))
        return

    if args.loop:
        # ── Q28: loop 启动时检测 cron 形态是否在跑，冲突拒启（防双发送）──
        rc = startup_conflict(str(engine._base_dir), "loop")
        if rc == 1:  # loop 冲突 → 拒绝启动常驻（无冲突返回 0；loop 不产生 cron 跳过 2）
            sys.exit(rc)
        run_loop(engine, args.loop, args.compact)
        return

    # 默认：单次评估（cron 形态的主动评估入口，--compact 亦经此）
    # Q28: cron 单次主动评估前检测 loop 形态是否在跑，冲突则跳过（防双发送）。
    _run_passive(engine, bool(args.compact))
    return


def main(argv=None):
    """CLI 入口：parse → 分发。退出码经 sys.exit 语义（成功返回 None）。"""
    _parser, args = parse_args(argv)
    run(args)
