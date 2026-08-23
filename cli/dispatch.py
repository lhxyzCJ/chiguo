"""cli.dispatch — daemon 入口分发（#376 表驱动：run 仅路由，逻辑在 handlers/）。

对外 CLI 行为完全不变：36 参数解析（见 cli.parser）、子命令分发顺序、
JSON 输出形状、exit code 语义与拆分前逐字一致。
"""
import sys
import json
import os
import fcntl
from datetime import datetime
from chiguo_time import CST
from pathlib import Path

from cli.parser import build_parser
from chiguo_version import VERSION

# bridge 发送链路已下沉至 ops.bridge_ops；此处 re-export 保持外部导入兼容
from ops.bridge_ops import bridge_post, push_alerts_via_wechat  # noqa: F401

# 兼容别名：历史名 _push_alerts_via_wechat 仍可从 cli.dispatch 导入
_push_alerts_via_wechat = push_alerts_via_wechat



def parse_args(argv=None):
    """解析命令行参数（36 参数）。供参数快照测试与 main 共用。"""
    parser = build_parser()
    return parser, parser.parse_args(argv)


# ── loop/cron 双形态运行期互斥守卫（Q28）─────────────────────────
def _cron_tick_lock_path() -> Path:
    lock_dir = os.environ.get("CHIGUO_LOCK_DIR") or os.path.join(
        os.path.expanduser("~"), ".chiguo", "run")
    return Path(lock_dir) / "chiguo-tick.lock"


def cron_form_active() -> bool:
    lock_path = _cron_tick_lock_path()
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def loop_form_active(base_dir: str) -> bool:
    pid_path = Path(base_dir) / "chiguo_loop.pid"
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def guard_mutual_form(base_dir: str, form: str) -> str | None:
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
    rc = startup_conflict(str(engine._base_dir), "cron")
    if rc == 2:
        sys.exit(0)
    if rc != 0:
        sys.exit(rc)
    decision = engine.evaluate()
    if compact and decision["action"] == "idle":
        print(json.dumps({"action": "idle", "version": VERSION,
                          "time": datetime.now(CST).isoformat()},
                         ensure_ascii=False))
        return
    from decision.core import json_default
    print(json.dumps(decision, ensure_ascii=False, indent=2, default=json_default))


def run(args):
    """分发逻辑：参数校验 + 表驱动路由（#376：CCN 72 → <10）。"""
    if args.loop is not None and args.loop < 60:
        print("[chiguo_daemon] interval < 60, using 60", file=sys.stderr)
    if args.ack and not args.alerts:
        print("[chiguo_daemon] --ack 需要 --alerts，已自动联动开启", file=sys.stderr)
        args.alerts = True

    # 无需引擎的早期分支（按原优先级）
    from cli.handlers.anniversary import handle_anniversary
    from cli.handlers.rotation import handle_rotation
    from cli.handlers.send import handle_record_send, handle_send_result
    from cli.handlers.conversation import handle_conversation
    from cli.handlers.break_cmd import handle_break
    from cli.handlers.tune import handle_tune
    from cli.handlers.consolidate import handle_consolidate
    from cli.handlers.monitor import handle_monitor
    from cli.handlers.health import handle_health
    from cli.handlers.light import handle_light

    if handle_anniversary(args):
        return
    if handle_rotation(args):
        return
    if handle_record_send(args):
        return
    if handle_send_result(args):
        return
    if handle_conversation(args):
        return
    if handle_break(args):
        return
    if handle_tune(args):
        return
    if handle_consolidate(args):
        return
    if handle_monitor(args):
        return
    if handle_health(args):
        return
    if handle_light(args):
        return

    # 需引擎的会话分支
    from decision.engine import DecisionEngine
    from cli.handlers.session import handle_status, handle_user_msg, handle_loop
    engine = DecisionEngine()
    if handle_status(args, engine):
        return
    if handle_user_msg(args, engine):
        return
    if handle_loop(args, engine):
        return
    _run_passive(engine, bool(args.compact))


def main(argv=None):
    """CLI 入口：parse → 分发。"""
    _parser, args = parse_args(argv)
    run(args)
