"""runner.loop — DecisionEngine loop/cron 发送形态。

拆自 chiguo_daemon.py：
  - LoopSenderMixin：loop 发送侧内聚（_loop_send 生成→发送→记账 + U2 健康记账）
                  + _dynamic_sleep_interval 动态休眠调度。
  - run_loop()：--loop 常驻循环编排（PID 锁 + 动态休眠 while 主循环）。
cron 形态（chiguo-tick.sh 每 15 分钟单次 spawn）的任务交给 cli/dispatch 默认分支。
"""
import os
import sys
import json
import time
import math
import random
from datetime import datetime, timedelta
from chiguo_time import CST

from decision.base import DecisionEngineBase
from chiguo_version import VERSION
from chiguo_net import build_no_proxy_opener, is_local_host


class LoopSenderMixin(DecisionEngineBase):
        def _dynamic_sleep_interval(self, now, decision: dict) -> float:
            """
            计算最优休眠时间（秒）。
            参考 Sebastian 的 proactive_scheduler.py 动态休眠设计。
            """
            reason = decision.get("reason", "")
            cfg_cooldown = self.config.get("cooldown", {})
    
            # 1. 若上次决策是 send → sleep min_interval + 缓冲
            if decision.get("action") == "send":
                min_int = cfg_cooldown.get("min_interval_minutes", 30)
                return (min_int + 5) * 60
    
            # 2. quiet_hours → sleep 到 quiet_end
            if reason == "quiet_hours":
                qs, qe = self.state.cooldown.quiet_window()
                # 跨午夜窗口 (qs > qe) 且已过 qs → 明天；非跨午夜或未到 qs → 今天
                if qe < qs and now.hour >= qs:
                    tomorrow = now.date() + timedelta(days=1)
                    nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, qe, 2, tzinfo=CST)
                else:
                    nxt = datetime(now.year, now.month, now.day, qe, 2, tzinfo=CST)
                return max(60, (nxt - now).total_seconds())
    
            # 3. daily_limit → sleep 到明天 8:00
            if reason == "daily_limit":
                tomorrow = now.date() + timedelta(days=1)
                nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 5, tzinfo=CST)
                return max(60, (nxt - now).total_seconds())
    
            # 4. low_energy → sleep 到能量恢复
            if reason == "low_energy":
                nxt_iso = decision.get("next_evaluation_at")
                if nxt_iso:
                    try:
                        nxt = datetime.fromisoformat(nxt_iso)
                        return max(60, (nxt - now).total_seconds())
                    except (ValueError, TypeError):
                        pass
    
            # 5. Bayesian sleeping/busy → sleep 到状态可能改变
            if reason in ("user_sleeping", "user_busy"):
                # 等 1-2 小时
                return 3600 + random.uniform(0, 3600)
    
            # 6. no_trigger → 基于 λ 算期望等待时间
            if reason == "no_trigger":
                lam = self.state.current_lambda(now)
                if lam > 0.001:
                    expected_wait_h = min(math.log(2) / lam, 2.0)
                    return max(300, expected_wait_h * 3600)
    
            # 7. 默认 fallback
            return 900  # 15 分钟

        def _record_health(self, outcome: str, reason: str, loop_cfg: dict) -> dict | None:
            """U2/v1.16 (#227): 生成段 health 记账——subprocess 调 agent_health.py record。
            --state 由 loop_cfg.health_state（测试隔离）或默认 <base_dir>/agent_health.json 注入。
            返回 record stdout JSON dict（含 state/transition/message/fail_streak）；失败静默返回 None（不阻断发送）。
            对齐 scripts/chiguo-tick.sh 的 record_health（同 agent_health.py 状态机，transition 仅翻转各一次）。"""
            import subprocess
            state_path = str(loop_cfg.get("health_state") or self._base_dir / "agent_health.json")
            runner = os.environ.get("AGENT_HEALTH_SCRIPT") \
                or str(self._base_dir / "scripts" / "agent_health.py")
            try:
                cmd = [sys.executable, runner, "record", "--outcome", outcome,
                       "--state", state_path]
                if reason:
                    cmd += ["--reason", reason]
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                data = json.loads(p.stdout or "{}")
                if isinstance(data, dict):
                    return data
            except (ValueError, TypeError, OSError):  # noqa: BLE001 - 记账失败静默，不阻断发送
                pass
            return None

        def _health_should_probe(self, loop_cfg: dict) -> bool:
            """U2/v1.16 (#227): loop 发送侧降频探测 / down 暂停判定（cron 形态走 tick.sh）。
            语义（用户拍板 Q2）：
              - down 状态 → 暂停（本次不尝试；恢复靠重启 loop，重启后首次 probe 放行）
              - 累计失败（1 ≤ fail_streak < threshold）→ 距上次失败 < probe_interval 跳过（降频），≥ 则 probe
              - 否则（健康/无状态文件/重启后首次）→ 放行尝试
            进程内首次调用恒放行（_loop_first_probe = True → 即「重启即恢复」的首次机会）。"""
            if getattr(self, "_loop_first_probe", True):
                self._loop_first_probe = False
                return True
            state_path = str(loop_cfg.get("health_state") or self._base_dir / "agent_health.json")
            try:
                with open(state_path) as f:
                    data = json.load(f)
            except (ValueError, TypeError, OSError):  # noqa: BLE001 - 无状态文件/未记账 → 放行
                return True
            if not isinstance(data, dict):
                return True
            if data.get("state") == "down":
                return False  # 暂停探测
            try:
                streak = int(data.get("fail_streak", 0) or 0)
                threshold = int((self.config.get("health", {}) or {}).get(
                    "fail_threshold", 3) or 3)
            except (TypeError, ValueError):
                streak, threshold = 0, 3
            if threshold <= 0:
                threshold = 3
            if 1 <= streak < threshold:
                probe_interval = float(loop_cfg.get("probe_interval_seconds", 3600) or 3600)
                last_fail = data.get("last_fail_at")
                try:
                    if last_fail:
                        last = datetime.fromisoformat(last_fail)
                        if (datetime.now(CST) - last).total_seconds() < probe_interval:
                            return False  # 未到降频探测节奏
                except (ValueError, TypeError, OSError):  # noqa: BLE001
                    pass
            return True

        def _loop_send(self, decision: dict, loop_cfg: dict) -> dict:
            """v1.11 C: --loop 常驻的发送侧内聚（替代 cron tick.sh 的 send 动作）。
            U2/v1.16 (#227): 生成失败不再 composer 兜底——sleep retry_delay 后整链重试一次
            （抖动缓冲，重试成功不计 fail_streak）；仍失败 → agent_health record fail（fail_streak+1，
            达 threshold 状态 down + transition 告警）并返回 generated=false。生成成功 → record success +
            transition（down→up 恢复）经 /send 发告警/恢复。发送段仍走 record_send_result 退款闭环。
            异常全部捕获返回结果 dict，不抛出（loop 循环不中断）。"""
    
            import urllib.request
    
            out: dict = {"generated": False, "sent": False}
            bridge_url = str(loop_cfg.get("bridge_url", "http://127.0.0.1:18790")).rstrip("/")
            # token：env 优先（wechat-bridge.sh 生成，不进 git），回退 toml [loop]（向后兼容）
            token = os.environ.get("WECHAT_BRIDGE_TOKEN") or str(loop_cfg.get("bridge_token", "") or "")
            # M-2: token 缺失时 /send 会 403 → 每轮显式告警（不改变发送行为，仅告知运维）
            if not token:
                print("[warn] _loop_send: 未配置 WECHAT_BRIDGE_TOKEN/[loop].bridge_token → bridge /send 将 403，"
                      "请先装 bridge 或配置 token", file=sys.stderr)
            try:
                timeout = max(10.0, float(loop_cfg.get("agent_timeout_ms", 125000)) / 1000.0)
            except (TypeError, ValueError):
                timeout = 125.0
            msg_id = decision.get("msg_id", "")
            trigger = decision.get("trigger")
            intensity = decision.get("intensity")
    
            def _post(path: str, body: dict, t: float) -> dict:
                data = json.dumps(body).encode("utf-8")
                req = urllib.request.Request(
                    f"{bridge_url}{path}", data=data,
                    headers={"Content-Type": "application/json"})
                if token:
                    req.add_header("X-Bridge-Token", token)
                # B5: 回环 bridge 调用绕系统代理（同 chiguo_envcheck._urlopen）
                host = urllib.request.urlsplit(req.full_url).hostname or ""
                if is_local_host(host):
                    resp = build_no_proxy_opener().open(req, timeout=t)
                else:
                    resp = urllib.request.urlopen(req, timeout=t)
                with resp:
                    return json.loads(resp.read().decode("utf-8"))
    
            def _try_generate() -> tuple:
                """完整生成链（RPC 优先 → spawn 回退）。返回 (text, err)。"""
                import subprocess
                text: str | None = None
                gen_err = ""
                try:
                    r = _post("/agent/prompt",
                              {"text": json.dumps(decision, ensure_ascii=False), "mode": "send"},
                              timeout)
                    if r.get("ok") and r.get("text"):
                        text = r["text"]
                    else:
                        gen_err = str(r.get("error") or "RPC 空回复")
                except Exception as e:  # noqa: BLE001 - 回退路径
                    gen_err = str(e)
                if not text:
                    runner = os.environ.get("AGENT_RUN_SCRIPT") \
                        or str(self._base_dir / "scripts" / "agent-run.mjs")
                    node_bin = os.environ.get("NODE_BIN") or "node"  # 必须用 node，AGENT_BIN 是 pi/agent 二进制
                    # R7 (F-A17-001): spawn 回退注入发送侧会话 env（对齐 tick.sh L127）——
                    # AGENTRUN_SESSION 取 toml [host].send_session_id（缺省 chiguo-send），
                    # AGENTRUN_ROTATE_SESSION=1 使 send 每轮全新。否则 agent-run.mjs:42
                    # 默认回落回复侧 chiguo-main 且不轮换（孤儿上下文/会话增长）。
                    spawn_env = {**os.environ,
                                 "AGENTRUN_SESSION": (self.config.get("host", {}) or {}).get(
                                     "send_session_id", "chiguo-send") or "chiguo-send",
                                 "AGENTRUN_ROTATE_SESSION": "1"}
                    try:
                        p = subprocess.run(
                            [node_bin, runner, "--prompt",
                             json.dumps(decision, ensure_ascii=False), "--send-mode"],
                            capture_output=True, text=True, timeout=timeout, env=spawn_env)
                        parsed = json.loads(p.stdout)
                        if parsed.get("ok") and parsed.get("text"):
                            text = parsed["text"]
                        else:
                            gen_err = f"{gen_err}; spawn: {parsed.get('error') or '空回复'}"
                    except Exception as e:  # noqa: BLE001
                        gen_err = f"{gen_err}; spawn: {e}"
                return text, gen_err
    
            # ── ① 生成（完整链；失败 sleep retry_delay → 整链重试一次，抖动缓冲不计 fail）──
            text, gen_err = _try_generate()
            if not text:
                retry_delay = float(loop_cfg.get("retry_delay_seconds", 5) or 0)
                if retry_delay > 0:
                    time.sleep(retry_delay)
                text, gen_err = _try_generate()
            def _send_transition_alert(rec):
                """transition（up/down）发生时经 /send 发告警/恢复（对齐 tick.sh record_health；仅翻转各一次）。"""
                if not rec:
                    return
                transition = rec.get("transition")
                message = rec.get("message")
                if transition in ("up", "down") and message:
                    alert_to = (self.config.get("wechat", {}) or {}).get("wechat_recipient", "")
                    if alert_to:
                        try:
                            _post("/send", {"to": alert_to, "text": message}, 10.0)
                        except (ValueError, TypeError, OSError):  # noqa: BLE001 - 告警失败不阻断主消息
                            pass
    
            if not text:
                out["error"] = gen_err
                # U2 (#227): 无 composer 兜底——记录 health fail（达 threshold → state down + transition 告警）
                _send_transition_alert(self._record_health("fail", gen_err, loop_cfg))
                # RF9 (F-RTS-001): 生成失败必须退款——evaluate 已对该 send 决策记账
                # （energy/quota/messages_without_reply+1/Hawkes/逃生阀冷却），只 record_health
                # fail 不退款会让未回复计数残留，连续失败 → silent 禁发链（backoff_level==2 →
                # evaluate_triggers return None → 恢复后永久不发）。record_send_result failed
                # 触发 refund_send 回滚未回复/额度/Hawkes；对齐收件人缺失分支（下方）的退款闭环。
                # record_health 语义不变（生成失败仍推进 fail_streak，健康状态机不回归）。
                if msg_id:
                    self.record_send_result(msg_id, "failed", f"generate_failed: {gen_err}")
                return out
            out["generated"] = True
            # F-A6-2: 不再生成即记 success——成功必须是生成+发送都 OK，success 移到发送
            # 成功分支（避免发送前清零导致发送失败 send_fail 永不累积、health 恒 up）。
            # ② 发送 + ③ 记账
            to = (self.config.get("wechat", {}) or {}).get("wechat_recipient", "")
            if not to:
                # ── v12: 收件人未配置 → 消息并未发出。不能走 record_send_text
                # （未发送文本若归档为 send，会污染 chiguo_messages.jsonl——
                # recent_sent_texts 的 A9 查重数据源，这些文本将被当「最近发过」
                # 抑制复用）；必须走 failed 退款闭环（Hawkes 事件清账/额度回滚）
                # 并告警，否则 msg_id 永不清账。──
                err = "wechat_recipient not configured"
                print(f"[chiguo_daemon] send skipped: {err} (msg_id={msg_id})",
                      file=sys.stderr)
                out["send_error"] = err
                if msg_id:
                    self.record_send_result(msg_id, "failed", err)
                return out
            try:
                # ── v1.15 (#164): /send 超时 10s→35s（微信 bridge 网络抖动下
                # 10s 易误判失败退款）；并校验返回体 ok 字段——bridge 返回
                # ok=false 视为发送失败走退款闭环，不再假记账 sent+1。
                # 35s 与 scripts/chiguo-tick.sh 主消息发送 curl --max-time 35 保持一致
                # （#261/CR-2: 对齐 cron / loop 双路径超时；改此值须同步改 tick.sh）。
                resp = _post("/send", {"to": to, "text": text}, 35.0)
                # R8 (F-A17-003): bridge /send 超时不确定（timeout_uncertain）——bot.send
                # 不可取消，超时不代表未送达。若按失败退款会恢复额度清冷却，制造下次 tick
                # 重发窗口 → 用户可能收到两条。故不退款、不记 send_fail、不重发（本 tick
                # 结束，下轮 evaluate 自然再试）；保留 out 标记供观测。
                if resp.get("timeout_uncertain"):
                    out["sent"] = False
                    out["send_timeout_uncertain"] = True
                    # RF11 (M2): 不退款不重发（防已送达重复消息），但做**轻量清算**——把本
                    # 消息 +1 的未回复计数回滚（record_send_result uncertain 分支只清
                    # messages_without_reply，不清额度/冷却/不重发）。否则持续超时（实际
                    # 未送达）未回复计数无限累积 → backoff_level==2 silent 永久禁发。
                    if msg_id:
                        self.record_send_result(msg_id, "uncertain", "timeout_uncertain")
                    print(f"[chiguo_daemon] /send timeout_uncertain (msg_id={msg_id}): "
                          f"不退款不重发，仅清算未回复计数，下轮自然再试", file=sys.stderr)
                    return out
                if not resp.get("ok"):
                    raise RuntimeError(str(resp.get("error") or "bridge /send ok=false"))
                out["sent"] = True
                # RF8 (L6-3): 本地 JSONL 记账（record_send_text，写 chiguo_messages.jsonl）
                # 失败（磁盘满等）**不能**把已发送的消息记成 send_fail + 退款——消息已送达，
                # 记 send_fail 会让健康状态机误降。故 record_send_text 单点异常分流：本地
                # 记账失败只告警 stderr，不影响 success 记账（下方 record_health success）。
                try:
                    self.record_send_text(msg_id, text, trigger, intensity)
                except Exception as e:  # noqa: BLE001 - 本地归档失败不影响主链路成功语义（磁盘满等 RuntimeError 亦分流）
                    print(f"[chiguo_daemon] record_send_text 失败 msg_id={msg_id}: {e}"
                          f" 消息已发送但本地 JSONL 归档未写（不影响健康记账）", file=sys.stderr)
                # F-A6-2: 发送成功 —— 生成+发送双成功才算健康；记 success 清零 +
                # down→up 恢复 transition 经 /send 发恢复（对齐 tick.sh）
                _send_transition_alert(self._record_health("success", "", loop_cfg))
            except Exception as e:  # noqa: BLE001
                out["send_error"] = str(e)
                if msg_id:
                    self.record_send_result(msg_id, "failed", str(e))
                # F-A6-2: 发送失败也记 health——生成已 OK 但 bridge /send 失败的轮次视为一次
                # 失败，记 send_fail 推进 fail_streak；连续 3 次发送失败 → down + transition 告警
                # + 暂停（对齐 tick.sh 发送失败分支；transition 告警经 /send，失败静默）。
                _send_transition_alert(self._record_health("send_fail", f"bridge send failed: {e}", loop_cfg))
            return out



def run_loop(engine, max_interval: int, compact: bool):
    """--loop 常驻运行编排。启动一次性评估后按动态休眠持续评估。

    发送侧（_loop_send）内聚在引擎（LoopSenderMixin），此处只做循环调度与终止清理。
    """
    def run():
        decision = engine.evaluate()
        if decision["action"] == "send":
            loop_cfg = engine.config.get("loop", {}) or {}
            try:
                if engine._health_should_probe(loop_cfg):
                    decision["_loop"] = engine._loop_send(decision, loop_cfg)
                else:
                    # R7 (F-RT-001): 抑制发送（health down/降频区间）不能只 print+return——
                    # evaluate() 已对该 send 决策记账（energy/messages/Hawkes/逃生阀
                    # last_longing_break_at）。走 failed 退款闭环回滚，复用收件人缺失
                    # 分支（_loop_send L254-259）同款 record_send_result，避免幻影记账
                    # 与逃生阀冷却被白扣。msg_id 从 decision 取。
                    decision["_loop"] = {"generated": False, "sent": False,
                                         "suppressed": True}
                    _msg_id = decision.get("msg_id", "")
                    if _msg_id:
                        engine.record_send_result(_msg_id, "failed", "suppressed")
            except Exception as e:
                decision["_loop_error"] = str(e)
            print(json.dumps(decision, ensure_ascii=False))
        elif not compact:
            print(json.dumps(decision, ensure_ascii=False))
        else:
            print(json.dumps({"action": "idle", "version": VERSION,
                              "time": datetime.now(CST).isoformat()},
                             ensure_ascii=False))
        sys.stdout.flush()
        return decision

    pid_path = engine._base_dir / "chiguo_loop.pid"
    try:
        fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            old_pid = int(pid_path.read_text().strip())
            try:
                os.kill(old_pid, 0)
                print(f"❌ 已有实例运行 (PID {old_pid})，拒绝启动", file=sys.stderr)
                sys.exit(1)
            except OSError:
                pass
        except (ValueError, OSError):
            pass
        try:
            pid_path.unlink(missing_ok=True)
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            print("❌ 无法获取 PID 锁，拒绝启动", file=sys.stderr)
            sys.exit(1)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    print(f"🔒 PID 锁: {os.getpid()}", file=sys.stderr)

    print(f"🔄 决策引擎每 ≤{max_interval}s 动态评估一次 (v4 动态休眠)", file=sys.stderr)

    decision = run()
    try:
        while True:
            now = datetime.now(CST)
            dynamic_sec = engine._dynamic_sleep_interval(now, decision)
            sleep_sec = min(dynamic_sec, max_interval)
            sleep_sec = max(60, sleep_sec)
            time.sleep(sleep_sec)
            decision = run()
    except KeyboardInterrupt:
        print("\n💤 已停止", file=sys.stderr)
        if not engine.state.save():
            print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
