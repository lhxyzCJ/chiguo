"""ops.engine_ops — DecisionEngine 记账/审计责任（AccountingMixin）。拆自 chiguo_daemon.py。
"""
import sys
import os
import json
import time
import threading
import hashlib
from datetime import datetime, timezone, timedelta

from decision.base import DecisionEngineBase
from chiguo_state import emotion_tag_snapshot

CST = timezone(timedelta(hours=8))

# F-A21-002 (#336): autowrite 同文本去重窗口（小时）。窗口内同文本二次写入跳过，
# 防 bridge 补报/重发同条消息重复触发 LLM 事实提取 → messages 表无界增长。
_MEM0_AUTOWRITE_DEDUP_HOURS = 24.0


# ── F-A21-002: autowrite 24h 文本 hash 去重（进程内最近写入 FIFO）──
# 存于 self._mem0_autowrite_hashes（{sha256(text): iso_ts}），惰性初始化。
# 模块级函数取 self 作第一参数：既适用于真实 DecisionEngine 实例，也兼容
# 测试里以 SimpleNamespace 注入的 engine（_mem0_autowrite_hashes 仅用 getattr/
# setattr，SimpleNamespace 同样支持）。

def _mem0_autowrite_hashes_dict(self) -> dict:
    """薄包装转发至 ops.analysis_ops（AUD-003 薄包装委托，保持兼容）。"""
    from ops.analysis_ops import _mem0_autowrite_hashes_dict as _impl
    return _impl(self)


def _mem0_autowrite_deduped(self, text: str) -> bool:
    from ops.analysis_ops import _mem0_autowrite_deduped as _impl
    return _impl(self, text)


def _mem0_autowrite_record(self, text: str):
    from ops.analysis_ops import _mem0_autowrite_record as _impl
    return _impl(self, text)

# F-A5-07 / F-RT-013 (#309): 锁内 IO 收口——consolidate 在 evaluate state_lock 临界区
# 内执行（决策流程锁内），qdrant get_all/update/delete 若无上限会无限阻塞锁 → 并发
# 进程 5s 拿不到锁降级无锁 → lost update 前置。给该调用加线程超时预算，超时按失败
# 降级（残留线程在底层返回后自然结束，与 mem0_backend._call_with_timeout 同语义）。
_CONSOLIDATE_TIMEOUT_S = 30.0


def _call_with_timeout(fn, timeout):
    """在守护线程执行 fn；超时返回 (False, None)；正常返回 (True, 结果)；异常重抛。"""
    box = {}

    def runner():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001 — 跨线程重抛，保持调用方异常语义
            box["e"] = e

    t = threading.Thread(target=runner, daemon=True, name="consolidate-timeout")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    if "e" in box:
        raise box["e"]
    return True, box.get("v")

class AccountingMixin(DecisionEngineBase):
        def record_user_message(self, text: str, analysis_json: str | None = None,
                                recv_id: str | None = None):
            """记录哥哥消息（确定性回传）。
            U5 (#233, D1): recv_id 精确去重——bridge 对每条主人消息本地生成
            crypto.randomUUID() 作为 --recv-id，recordUserMsg 与 upgradeAnalysis 两次
            调用携带同一 id → daemon 以 id 精确判定补报升级（同 id → 只补分析账，免
            450s 窗口）。无 recv_id（CLI 手动/测试/老调用）→ 回退 text_sha+窗口逻辑
            （RECV_DEDUP_WINDOW_S 不变），行为向后兼容。recv_id 仅去重流，不进 agent prompt。"""
            now = datetime.now(CST)
            msg_id = self._make_msg_id()
            from ops.analysis_ops import parse_analysis_json
            analysis_dict = parse_analysis_json(analysis_json)
    
            # ── v11 (#75): RMW 临界区全程持跨进程锁，防并发丢更新。
            # v1.14 (#139): 锁内先重载磁盘最新状态再处理——构造加载（S0）与拿到锁
            # 之间，cron evaluate（锁内 _load+推进+save）可能已把 S1 落盘；若基于
            # S0 陈旧快照 RMW+save，S0→S1 的情绪推进/cooldown 更新被整体回滚
            # （tick_seq CAS 只防序列回退，不保护 emotion/cooldown 字段）。
            # 与 record_send_result v12-R2 同一修复。旧注释「单次 CLI 进程不重载、
            # 避免覆盖调用方在构造后的内存修改」理由不成立：唯一生产调用点
            # （main 1855 行 engine 构造后立即调用）进锁前无任何进程内修改，
            # 重载幂等且安全。──
            with self.state.state_lock() as lock_acquired:
                # F-A16-01 (#309): 降级无锁进入 → 告警 + audit；save() 侧降级重读
                # 校验兜底防覆盖（正常持锁路径行为与现状一致）。
                if not lock_acquired:
                    print("[chiguo_daemon] state_lock 降级：record_message 无锁执行"
                          "（并发持锁 >5s），已进入降级保护", file=sys.stderr)
                    self.state.audit("state_lock_degraded", "record_message")
                try:
                    self.state._load()
                except (ValueError, TypeError, OSError):  # noqa: BLE001 - 重载失败维持现有内存状态
                    pass
                # ── v9: recv 去重（升级语义）──────────────────────
                # 同一条消息会被记录两次：bridge 先确定性 --user-msg（无分析），
                # standing order 随后补 --user-msg --analysis。基础回复效果
                # （延迟/情绪骤降/好感/元气）只应应用一次；第二次只补分析微调。
                # 仅当上一条同文本记录"无分析"且时间差极短（<RECV_DEDUP_WINDOW_S）时，本条才视为
                # bridge 补报的升级副本；其余同文本（已升级过的、或时间差较长的）
                # 一律视为用户真实重发 → 走完整 on_user_message。
                text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                dedup = self.state.cooldown.recv_dedup
                from ops.analysis_ops import is_recv_upgrade
                is_upgrade = is_recv_upgrade(
                    analysis_dict, dedup, text_sha, recv_id, now, self.RECV_DEDUP_WINDOW_S)
    
                if is_upgrade:
                    if analysis_dict:
                        self.state._apply_analysis_impact(analysis_dict, now)
                        self.state.cooldown.recv_dedup = {
                            "text_sha": text_sha,
                            "at": now.isoformat(),
                            "analysis": True,
                            "recv_id": recv_id,
                        }
                        if not self.state.save():
                            print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
                        self._monotonic_at_save = time.monotonic()  # v5
                        self._log({
                            "action": "recv_upgrade",
                            "msg_id": msg_id,
                            "message_text": text,
                            "user_emotion_analysis": analysis_dict,
                            "state": self.state.snapshot(now),
                        })
                    return
    
                # ── v4: 保存发送前状态用于追踪 ──
                prev_send_was_replied = self.state.cooldown.messages_without_reply > 0
                self.state.on_user_message(now, len(text), analysis=analysis_dict)
    
                # ── v9: 更新去重标记（记录是否已含分析；U5: recv_id 精确去重持久化）──
                self.state.cooldown.recv_dedup = {
                    "text_sha": text_sha,
                    "at": now.isoformat(),
                    "analysis": analysis_dict is not None,
                    "recv_id": recv_id,
                }
    
                # ── v4: 人格自适应（收到回复后）──
                if prev_send_was_replied:
                    # A2: 分类型回复率统计——本次用户消息是对上一条发送的回复 →
                    # FIFO 归因队列最旧的未回复发送触发类型的 replied+1（数据源供
                    # chiguo_trigger 反馈闭环）。门控 reply_feedback_enabled（默认 0 恒等）。
                    if self.config.get("trigger", {}).get("reply_feedback_enabled", False):
                        self.state.record_trigger_replied()
                    try:
                        self.state.adapt_personality({
                            "type": "character_send",
                            "was_replied": True,
                            "trigger": "user_reply",
                        })
                        # v1.11 ④: 情绪基线漂移（被回复 → 零漂移，保持接口对称）
                        self.state.update_emotion_baseline({
                            "type": "character_send",
                            "was_replied": True,
                            "trigger": "user_reply",
                        })
                    except (ValueError, TypeError, OSError):
                        pass
    
                if not self.state.save():
                    print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
                self._monotonic_at_save = time.monotonic()  # v5
    
            # ── v5: 记录 recv 到决策日志 ──
            recv_entry = {
                "action": "recv",
                "msg_id": msg_id,
                "message_text": text,
                "message_length": len(text),
                "state": self.state.snapshot(now),
            }
            if analysis_dict:
                recv_entry["user_emotion_analysis"] = analysis_dict
            self._log(recv_entry)
    
            # ── v5: 记录到对话归档 ──
            self._log_message(
                msg_id=msg_id,
                direction="recv",
                text=text,
                user_emotion_analysis=analysis_dict,
            )
    
            # ── v10: 对话后自动写入 mem0（事实提取）──
            # mem0 从用户话语提取长期记忆；短消息（寒暄/无信息量）跳过。
            # 失败静默（LLM 超时/ollama 未启动等不影响 --user-msg 主链路）。
            self._mem0_autowrite(text)

        def _mem0_autowrite(self, text: str):
            """daemon 对话后自动写入 mem0（LLM 提取事实；见 memory/mem0_backend.py）。
            B2: [memory].emotion_tagging=True（默认 False 恒等）时，把当前情绪快照
            （loneliness/affection/anxiety/energy 离散档 + user_mood）写进 metadata 的
            emotion_tag——供读侧按情绪相近加权（emotion_tag_weight）。
            F-A21-002: 同文本 24h 内去重（进程内最近写入 hash FIFO）——bridge 补报/重发
            同一条消息不会重复触发 add_messages → LLM 提取 + messages 表不无界增长。
            设 CHIGUO_MEM0_AUTOWRITE=0 可跳过自动写入（部署验证/测试用途，防止
            验证消息混入生产记忆库）。"""
            if os.environ.get("CHIGUO_MEM0_AUTOWRITE", "1") != "1":
                return  # 部署验证/测试可设 0 防污染生产记忆库
            if len(text.strip()) < 8:
                return  # 短消息（寒暄/无信息量）不写，也避免无谓的可用性探测
            if _mem0_autowrite_deduped(self, text):
                return  # 同文本 24h 内刚写过 → 跳过（不重复提取/不增长 messages 表）
            try:
                mem = self.state.memory_bridge
                if not getattr(mem, "available", False) or not getattr(mem, "add_messages", None):
                    return
                metadata = {"category": "conversation", "scope": "global", "source": "daemon"}
                if self.config.get("memory", {}).get("emotion_tagging", False):
                    tag = emotion_tag_snapshot(self.state.emotion)
                    mood = self.state.cooldown.user_mood
                    if isinstance(mood, dict) and str(mood.get("mood", "")).strip():
                        tag["user_mood"] = str(mood.get("mood"))
                    metadata["emotion_tag"] = tag
                # ── C4: 写全对话轮次（[memory].write_full_turns 默认 False 恒等）──
                # 开启时带上最近一条 assistant 回复组成 user+assistant 两轮，
                # mem0 据此提取"迟菓回应了什么"的上下文事实。默认单条 user 写入恒等。
                messages = [{"role": "user", "content": text}]
                if self.config.get("memory", {}).get("write_full_turns", False):
                    recent = self.recent_sent_texts(n=1)
                    if recent:
                        messages.append({"role": "assistant", "content": recent[0]})
                if mem.add_messages(messages, metadata=metadata):
                    # F-A21-002: 写入成功才记 hash，供 24h 内同文本去重
                    _mem0_autowrite_record(self, text)
            except (ValueError, TypeError, OSError):
                pass  # 记忆写入失败不影响主流程

        def _log_message(self, msg_id: str, direction: str, text: str,
                         trigger: str = None, intensity: str = None,
                         user_emotion_analysis: dict = None,
                         fallback: bool = False):
            """追加人类可读对话记录到 chiguo_messages.jsonl。"""
            now = datetime.now(CST)
            snap = self.state.snapshot(now)
            record = {
                "msg_id": msg_id,
                "ts": now.isoformat(),
                "direction": direction,
                "text": text,
            }
            if direction == "send":
                record["trigger"] = trigger
                record["intensity"] = intensity
                # A8: agent 生成失败 → composer 确定性兜底 → 打标记（health 仍记 success）
                if fallback:
                    record["fallback"] = True
                record["emotion_snapshot"] = {
                    "loneliness": round(snap["emotion"]["loneliness"], 0),
                    "affection": round(snap["emotion"]["affection"], 0),
                    "anxiety": round(snap["emotion"]["anxiety"], 0),
                    "energy": round(snap["emotion"]["energy"], 0),
                    "tsundere_index": round(snap["emotion"]["tsundere_index"], 0),
                }
            else:
                if user_emotion_analysis:
                    record["user_emotion_analysis"] = user_emotion_analysis
            try:
                from chiguo_atomic import append_jsonl_0600
                append_jsonl_0600(self.messages_log_path, record)
            except Exception as e:
                print(f"[warn] 写入 {self.messages_log_path} 失败: {e}", file=sys.stderr)  # 消息归档失败不影响主流程

        def record_send_text(self, msg_id: str, text: str,
                             trigger: str = None, intensity: str = None,
                             fallback: bool = False):
            """记录已发送消息文本（发送侧回调）。
            仅写 chiguo_messages.jsonl —— decisions.jsonl 已有 send 决策条目。
            trigger/intensity 从 send 决策中提取，使 messages.jsonl 自包含。
            fallback（A8）: composer 确定性兜底生成的标记（agent 生成失败时 True）。
            A2: 发送确认时对该触发类型 sent+1（分类型回复率统计数据源）。
            """
            # A2: 确认已发送 → 该触发类型 sent+1（与 --user-msg 的 replied 归因配对）。
            # 门控 reply_feedback_enabled（默认 0）：关闭时不做记账也不写盘——状态文件
            # 不新增 reply_stats 键、发送路径不产生额外 save（默认关闭恒等）。
            if trigger and self.config.get("trigger", {}).get("reply_feedback_enabled", False):
                try:
                    # ── v1.15 (#157): RMW 临界区全程持跨进程锁。锁内先 _load 重载
                    # 磁盘最新状态，再 record_trigger_sent + save——cron --record-send
                    # 为一次性进程，不 save 则 sent 计数随进程退出丢失；与 --user-msg
                    # 并发时基于最新落盘状态记账，防止覆盖丢更新。
                    with self.state.state_lock() as lock_acquired:
                        # F-A16-01 (#309): 降级无锁进入 → 告警 + audit。
                        if not lock_acquired:
                            print("[chiguo_daemon] state_lock 降级：record_sent 无锁"
                                  "执行（并发持锁 >5s），已进入降级保护", file=sys.stderr)
                            self.state.audit("state_lock_degraded", "record_sent")
                        self.state._load()
                        self.state.record_trigger_sent(trigger)
                        # RF6（L3-1）: save 返回 False（降级/写盘失败）→ sent 计数静默
                        # 丢失。感知并告警（对齐 record_message/record_send_result 模式），
                        # 保持 best-effort 语义不抛。
                        if not self.state.save():
                            print("[chiguo_daemon] state_save_failed: record_sent 未落盘"
                                  "（sent 计数丢失，下轮重试）", file=sys.stderr)
                except (ValueError, TypeError, OSError):
                    pass  # 统计失败不影响发送记录主链路
            self._log_message(
                msg_id=msg_id,
                direction="send",
                text=text,
                trigger=trigger,
                intensity=intensity,
                fallback=fallback,
            )

        def record_send_result(self, msg_id: str, status: str, error: str = None):
            """v6 反馈闭环: 发送后回传结果。
            success → 记录 delivered；failed → 退款（情绪消耗/额度回滚）+ 记录。
            幂等: 同一 msg_id 重复上报不再退款（日志按 msg_id 去重）。
            time 用 %Y-%m-%d %H:%M 格式（与 snapshot 一致，monitor _extract_time 依赖）。"""
            now = datetime.now(CST)
            refunded = False
            # ── v11 (#75): RMW 临界区全程持跨进程锁，防并发重复退款或丢更新。
            # v12: already_reported 检查与 result 日志写入均移入锁内——并发进程
            # 锁外各自读到「未上报」后依次进锁，第二个进程锁内重查可见第一条
            # 日志 → duplicate=true 不再退款（修复锁外检查的 TOCTOU 双退款）。
            # 注意：state_lock 为单线程语义（同进程第二线程视为重入直接通过、
            # 不互斥）；本方法生产调用路径（--loop 主循环 / CLI --send-result）
            # 均单线程，并发去重保障针对跨进程（flock）场景。
            # 本方法为单次 CLI 进程（启动时已加载最新状态），不重载 _load。──
            with self.state.state_lock() as lock_acquired:
                # F-A16-01 (#309): 降级无锁进入 → 告警 + audit；save 侧降级重读
                # 校验防覆盖（本方法 v12-R2 锁内重载，但降级时锁内 _load 读到的
                # 仍是并发进程落盘前的旧快照，需靠 save 兜底）。
                if not lock_acquired:
                    print("[chiguo_daemon] state_lock 降级：record_send_result 无锁"
                          "执行（并发持锁 >5s），已进入降级保护", file=sys.stderr)
                    self.state.audit("state_lock_degraded", "record_send_result")
                # v12-R2: 锁内重载磁盘最新状态再执行退款——CLI --send-result 与
                # cron evaluate 并发时，若基于构造时（T0）陈旧快照 refund 后 save，
                # 会覆盖 evaluate 已落盘的情绪推进（tick_seq CAS 只防序列回退，
                # 不保护 emotion/cooldown 字段）。安全前提：本方法调用路径进锁前
                # 均无未保存的进程内修改（evaluate 各出口无条件 save；--loop 的
                # _loop_send 在 evaluate 锁释放后才执行）。
                try:
                    self.state._load()
                except (ValueError, TypeError, OSError):  # noqa: BLE001 - 重载失败维持现有内存状态
                    pass
                already_reported = self._has_send_result(msg_id)
                if status == "failed" and not already_reported:
                    # ── v6 修复: 仅当 msg_id 能在在途 Hawkes 事件中定位（或全部为 legacy
                    #    事件）时才退款——未知 msg_id 不产生副作用（防凭空刷新逃生阀冷却/
                    #    误删最后一条事件）。legacy/匹配判定已收敛到 state.refund_send 单处，
                    #    由返回值决定是否落盘（Q30 legacy 事件两处复制收敛）。──
                    if self.state.refund_send(now, msg_id=msg_id):
                        if not self.state.save():
                            # v12-R1: save 失败 → 不写 send_result 日志、refunded 保持
                            # False。幂等依据是日志：日志不写 = 可重试，下一进程会再次
                            # 尝试退款直至落盘；若此处照写日志，退款将永久丢失。
                            print("[chiguo_daemon] state_save_failed: 退款未落盘，下轮重试", file=sys.stderr)
                            return {
                                "action": "send_result",
                                "msg_id": msg_id,
                                "status": status,
                                "error": error,
                                "time": now.strftime("%Y-%m-%d %H:%M"),
                                "refunded": False,
                                "duplicate": False,
                            }
                        refunded = True
                    else:
                        print(
                            f"[chiguo_daemon] refund skipped: msg_id={msg_id!r} "
                            f"not found in {len(self.state.cooldown.event_timestamps)} in-flight events",
                            file=sys.stderr,
                        )
                elif status == "uncertain" and not already_reported:
                    # RF11 (M2): /send 结果不确定（timeout_uncertain / 非 JSON 体）——
                    # 轻量清算：只回滚本消息 +1 的未回复计数，**不**完整退款（不清额度/冷却/
                    # 不恢复重发窗口，防已送达时制造重复消息；未回复计数不无限累积致 silent）。
                    # _has_send_result 对同 msg_id 去重 → 每消息只清一次；save 幂等落盘。
                    self.state.clear_unreplied(now)
                    if not self.state.save():
                        print("[chiguo_daemon] state_save_failed: 未回复清算未落盘，下轮重试",
                              file=sys.stderr)
                        return {
                            "action": "send_result",
                            "msg_id": msg_id,
                            "status": status,
                            "error": error,
                            "time": now.strftime("%Y-%m-%d %H:%M"),
                            "refunded": False,
                            "duplicate": False,
                        }
                    refunded = True
                result = {
                    "action": "send_result",
                    "msg_id": msg_id,
                    "status": status,
                    "error": error,
                    "time": now.strftime("%Y-%m-%d %H:%M"),
                    "refunded": refunded,
                    "duplicate": already_reported,
                }
                # v12: 日志写入与去重检查同一临界区（并发进程的 duplicate 判定
                # 基于锁内可见的最新日志；state_lock 可重入，_log 不持其他锁）
                self._log(result)
            return result

        def recent_sent_texts(self, n: int = 5) -> list[str]:
            """A9 查重数据源：最近 n 条已发送消息文本（chiguo_messages.jsonl 倒序取）。
            记录由 --record-send --text 写入（含 direction=send + text 字段），
            不新增文件。文件缺失/损坏行 → 静默跳过（查重降级为不启用）。"""
            # Q29 移植（#279 tail-read）：只读文件尾部最近 500 行（倒序原语），
            # 不全量 readlines：日志随运行时间线性增长，全量扫描无必要（复用
            # _has_send_result 的尾部读先例，seek 到末尾分块向前累计到窗口即停），
            # 行为等价：仍只返回最近 n 条 direction=send 且有 text 的文本。
            tail: list[str] = []
            try:
                with open(self.messages_log_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    pos = f.tell()
                    buf = b""
                    while pos > 0 and len(tail) < 500:
                        step = min(65536, pos)
                        pos -= step
                        f.seek(pos)
                        buf = f.read(step) + buf
                        tail = buf.decode("utf-8", errors="replace").splitlines()
                    tail = tail[-500:]
            except OSError:
                return []
            texts: list[str] = []
            for line in reversed(tail):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("direction") == "send" and rec.get("text"):
                    texts.append(rec["text"])
                    if len(texts) >= n:
                        break
            return texts

        def _has_send_result(self, msg_id: str) -> bool:
            """日志中是否已有该 msg_id 的 send_result 条目（幂等防护）。
            从文件尾部倒序扫描最近 500 行（窗口不足自动扩展），
            避免全量 O(n) 扫描：send_result 紧邻对应 send 决策，无需看更早日志。"""
            tail: list[str] = []
            try:
                with open(self.log_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    pos = size
                    buf = b""
                    while pos > 0 and len(tail) < 500:
                        step = min(65536, pos)
                        pos -= step
                        f.seek(pos)
                        buf = f.read(step) + buf
                        tail = buf.decode("utf-8", errors="replace").splitlines()
                    tail = tail[-500:]
            except OSError:
                return False
            for line in reversed(tail):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("action") == "send_result" and entry.get("msg_id") == msg_id:
                    return True
            return False

        def _maybe_consolidate(self, now: datetime):
            """C1: 空闲静默路径的确定性记忆巩固（零 LLM；[memory].consolidate_enabled 默认关闭）。
    
            门控：consolidate_enabled + 清醒沉默 ≥ consolidate_idle_silent_hours +
            距上次巩固 ≥ consolidate_min_interval_hours（持久化 cooldown.consolidate_last_at）。
            consolidate() 内部对不可用/无写能力的后端只出报告不写库，双保险不碰主链路。
            也可手动 `chiguo_daemon.py --consolidate`（停机维护专用——勿与常驻进程并行，见 #181）。
            """
            mem_cfg = self.config.get("memory", {})
            if not mem_cfg.get("consolidate_enabled", False):
                return
            attempted = False
            try:
                silent_h = self.state.cooldown.silent_hours(now)
                if silent_h < float(mem_cfg.get("consolidate_idle_silent_hours", 24.0)):
                    return
                last = self.state.cooldown.consolidate_last_at
                if last:
                    last_dt = datetime.fromisoformat(last)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=CST)
                    min_iv = float(mem_cfg.get("consolidate_min_interval_hours", 168.0))
                    if (now - last_dt).total_seconds() / 3600.0 < min_iv:
                        return
                bridge = self.state.memory_bridge
                if not getattr(bridge, "consolidate", None):
                    return  # 后端不支持巩固 → 静默跳过
                attempted = True
                # F-RT-013 (#309): consolidate 在 state_lock 锁内执行，用线程超时
                # 预算封顶持锁时长（qdrant 挂起不再无限阻塞锁）。超时按失败降级 →
                # 下方 except 打 stderr；finally 仍推进 consolidate_last_at 防 hot-loop。
                _ok, report = _call_with_timeout(
                    bridge.consolidate, _CONSOLIDATE_TIMEOUT_S)
                if not _ok:
                    raise TimeoutError(
                        f"mem0 consolidate 超时（>{_CONSOLIDATE_TIMEOUT_S}s）")
                n_demoted = len((report or {}).get("demoted", []))
                n_expired = len((report or {}).get("expired", []))
                if n_demoted or n_expired:
                    print(f"[chiguo_daemon] memory consolidate: demoted={n_demoted} "
                          f"expired={n_expired}", file=sys.stderr)
            except Exception as e:
                # 巩固失败不影响 idle 主链路（记忆整理是锦上添花）
                print(f"[chiguo_daemon] memory consolidate skipped: {e}", file=sys.stderr)
            finally:
                # 防 hot-loop：无论成功失败，只要真正尝试过巩固就推进 last_at（间隔门控防
                # 后端/配置错误导致每 15 分钟全量 get_all+扫描重试）。失败原因已打到 stderr，
                # 显式 --consolidate CLI 仍可手动兜底。
                if attempted:
                    self.state.cooldown.consolidate_last_at = now.isoformat()
                    if not self.state.save():
                        print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)

        def cli_consolidate(self) -> int:
            """C1: `--consolidate` CLI 单进程逻辑（self 即引擎，测试注入 fake state.memory_bridge）。
    
            exit 0 = 后端可用且 consolidate ok；exit 1 = 后端不支持/异常/报告 ok=False。
            stdout 只出 JSON（供 cron 落日志）；隐私：报告剔除 text 字段——记忆行是 LLM
            提取的用户私聊事实，重定向到日志文件会泄露完整对话内容（自动路径本就只打计数）。
            阈值走 Mem0Backend.consolidate 内的 _finite_float 兜底，字符串/NaN 配置不崩。
            """
            # #181: 停机维护守卫——daemon --loop 常驻进程持有嵌入式 qdrant 单进程锁，
            # 并发 --consolidate 会争锁导致巩固静默失效。检测 loop PID 文件，进程存活则拒绝。
            pid_path = self._base_dir / "chiguo_loop.pid"
            loop_pid = None
            try:
                loop_pid = int(pid_path.read_text().strip())
            except (FileNotFoundError, ValueError, OSError):
                pass  # 无 pid 文件 / 损坏 → 视为未运行
            if loop_pid is not None:
                try:
                    os.kill(loop_pid, 0)
                    print(f"[error] daemon 常驻进程运行中 (PID {loop_pid})，"
                          f"--consolidate 仅停机维护使用", file=sys.stderr)
                    return 1
                except OSError:
                    pass  # 进程已退出 → 过期 pid 文件，忽略
            mem_cfg = self.config.get("memory", {})
            bridge = self.state.memory_bridge
            if not getattr(bridge, "consolidate", None):
                print(json.dumps({"action": "consolidate", "ok": False,
                                  "error": "记忆后端不支持 consolidate"}, ensure_ascii=False))
                return 1
            try:
                report = bridge.consolidate(
                    sim_threshold=mem_cfg.get("consolidate_sim_threshold", 0.85),
                    min_importance=mem_cfg.get("consolidate_min_importance", 0.3),
                    max_age_hours=mem_cfg.get("consolidate_max_age_hours", 720.0),
                )
            except Exception as e:
                # 配置/后端异常 → 结构化错误（不裸 traceback），exit 1
                print(json.dumps({"action": "consolidate", "ok": False,
                                  "error": f"consolidate failed: {e}"},
                                 ensure_ascii=False, default=str))
                return 1
            safe_report = dict(report)
            for k in ("demoted", "expired", "kept"):
                safe_report[k] = [{kk: vv for kk, vv in r.items() if kk != "text"}
                                  for r in report.get(k, [])]
            print(json.dumps({"action": "consolidate", **safe_report}, ensure_ascii=False,
                             default=str))
            return 0 if report.get("ok") else 1

        # v9+R1+R2: 补报升级判定窗口（须覆盖 bridge 两次 daemon 调用间的 agent 分析往返：
        #  recall 两趟 agent 路径最坏 420s：getAttention≤30s + askAgent 第一趟≤180s
        #  + --schedule-recall daemon≤30s + runAgentRun 第二趟 agent≤180s；余量 30s）
        RECV_DEDUP_WINDOW_S = 450
