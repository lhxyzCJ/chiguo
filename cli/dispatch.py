"""cli.dispatch — daemon 入口分发（拆自 chiguo_daemon.py main()）。

对外 CLI 行为完全不变：36 参数解析（见 cli.parser）、子命令分发顺序、
JSON 输出形状、exit code 语义与拆分前逐字一致。子命令分优先级：
  纪念日 → rotate → record_send → send_result → 对话/导出 → break → tune →
  consolidate → 监控 → health → 轻量子命令 → status → user_msg → loop → 默认单次评估
"""
import sys
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from cli.parser import build_parser
from cli.commands import (_load_light_config, _cmd_attention,
                          _cmd_schedule_recall, _cmd_schedule_change, _cmd_memory_search)
from runner.loop import run_loop
from chiguo_version import VERSION

CST = timezone(timedelta(hours=8))


def bridge_post(bridge_url: str, token: str, path: str, body: dict,
                timeout: float = 10.0) -> dict:
    """POST JSON 至 wechat-bridge，返回解析后的响应 dict。

    Q24 (#275) 移植：从 daemon _loop_send 嵌套 _post 提取的模块级复用入口——
    主动发送（runner/loop）与告警微信推送（--alerts-push / scripts/alert-cron.sh）
    共用同一发送链路，保证 token 注入 + 回环代理绕过（B5）行为一致。
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{bridge_url}{path}", data=data,
        headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Bridge-Token", token)
    # B5: 回环 bridge 调用绕系统代理（同 chiguo_envcheck._urlopen：本机有
    # http_proxy 时 localhost 直连不被劫持，防回环请求走代理失败降级）
    host = urllib.request.urlsplit(req.full_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        resp = opener.open(req, timeout=timeout)
    else:
        resp = urllib.request.urlopen(req, timeout=timeout)
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def _push_alerts_via_wechat(engine: "DecisionEngine", new_alerts: list[dict]) -> list[dict]:
    """把 collect_new_alerts_to_push 返回的告警经微信 bridge /send 推送。

    Q24 (#275): 解复用 loop 发送侧的 bridge 链路（bridge_post + token 注入 + 收件人解析）。
    告警文案非 LLM 生成（运维/系统事件直发，与 agent_health transition 告警同性质）。
    推送失败不阻断：返回已实际投递成功的告警（失败静默，下次 cron 不再重推——去重语义）。
    返回告警附加的 `delivered` 为 CLI 输出专用元数据，不持久化（chiguo_alerts.json
    在 collect_new_alerts_to_push 的 ingest() 阶段已落盘；cron 全新建进程不会回写）。
    """
    if not new_alerts:
        return []
    wechat = (engine.config.get("wechat", {}) or {})
    to = wechat.get("wechat_recipient", "")
    if not to:
        print("[chiguo_daemon] --alerts-push: wechat_recipient 未配置，跳过微信推送",
              file=sys.stderr)
        return []
    loop_cfg = engine.config.get("loop", {}) or {}
    bridge_url = str(loop_cfg.get("bridge_url", "http://127.0.0.1:18790")).rstrip("/")
    token = os.environ.get("WECHAT_BRIDGE_TOKEN") or str(loop_cfg.get("bridge_token", "") or "")
    pushed: list[dict] = []
    for alert in new_alerts:
        severity = alert.get("severity", "info")
        alert_id = alert.get("alert_id", "?")
        text = f"🚨 迟菓告警 [{severity}] {alert.get('type', 'unknown')}：{alert.get('message', '')}"
        try:
            resp = bridge_post(bridge_url, token, "/send", {"to": to, "text": text}, 10.0)
            if not resp.get("ok"):
                raise RuntimeError(str(resp.get("error") or "bridge /send ok=false"))
            alert["delivered"] = True  # 仅 CLI 输出用元数据，不持久化（见函数 docstring）
            pushed.append(alert)
        except Exception as e:  # noqa: BLE001 - 单条推送失败不阻断其余告警
            print(f"[chiguo_daemon] --alerts-push 推送失败 alert_id={alert_id}: {e}",
                  file=sys.stderr)
    return pushed


def parse_args(argv=None):
    """解析命令行参数（36 参数）。供参数快照测试与 main 共用。"""
    parser = build_parser()
    return parser, parser.parse_args(argv)


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

    # ── 监控系统（stats / alerts / monitor / alerts-push）（v10: 锚定 base_dir，从任意 cwd 运行都读写项目文件）──
    if args.stats is not None or args.alerts or args.monitor or args.alerts_push:
        from decision.engine import DecisionEngine
        from chiguo_monitor import ChiguoMonitor, AlertManager
        engine = DecisionEngine()
        mon = ChiguoMonitor(
            log_path=str(engine._base_dir / "chiguo_decisions.jsonl"),
            state_path=str(engine._base_dir / "chiguo_state.json"),
            break_state_path=str(engine._base_dir / "break_state.json"),
            config_path=str(engine._base_dir / "chiguo_proactive.toml"),
            messages_log_path=str(engine._base_dir / "chiguo_messages.jsonl"),
            alerts_path=str(engine._base_dir / "chiguo_alerts.json"),
            events_path=str(engine._base_dir / "chiguo_events.jsonl"),
        )
        # ── Q24 (#275): 告警微信推送（cron 化入口，--alerts-push 独立命中）──
        if args.alerts_push:
            from chiguo_monitor import collect_new_alerts_to_push
            am = AlertManager(state_path=str(engine._base_dir / "chiguo_alerts.json"))
            new_alerts = collect_new_alerts_to_push(mon, am)
            pushed = _push_alerts_via_wechat(engine, new_alerts)
            print(json.dumps({"action": "alerts_push", "pushed": len(pushed),
                              "alerts": pushed}, ensure_ascii=False, indent=2))
            return
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
        run_loop(engine, args.loop, args.compact)
        return

    # 默认：单次评估
    decision = engine.evaluate()
    if args.compact and decision["action"] == "idle":
        # 紧凑模式 idle 时输出最小 heartbeat（用于 cron 健康检查）
        print(json.dumps({"action": "idle", "version": VERSION, "time": datetime.now(CST).isoformat()}, ensure_ascii=False))
        return
    print(json.dumps(decision, ensure_ascii=False, indent=2))


def main(argv=None):
    """CLI 入口：parse → 分发。退出码经 sys.exit 语义（成功返回 None）。"""
    _parser, args = parse_args(argv)
    run(args)
