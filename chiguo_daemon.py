#!/usr/bin/env python3
# ============================================================
# chiguo_daemon.py — 迟菓主动消息 决策引擎
#
# 只做一件事：评估状态 → 输出触发决策（JSON）。
# 不生成消息，不调用 LLM，不发送。
# 消息生成和发送由 agent 后端（scripts/agent-run.mjs）完成。
#
# 用法：
#   python3 chiguo_daemon.py              # 检查并输出决策 JSON
#   python3 chiguo_daemon.py --status     # 查看状态
#   python3 chiguo_daemon.py --user-msg "…"  # 记录哥哥消息
#   python3 chiguo_daemon.py --loop 120   # v1.11 C: 持续运行（调试用）——send 分支内聚发送侧：生成→发送→记账 + U2 health 记账（见 --loop 常驻形态）
#
# cron 集成：
#   系统 crontab 每 15 分钟经 scripts/chiguo-tick.sh 执行本脚本。
#   若 stdout 输出含 "action":"send"，chiguo-tick.sh 调 agent 读取 context
#   字段生成消息，经 wechat-bridge 发出。
#   若输出 "action":"idle"，什么都不做。
# ============================================================

import sys
import json
import math
import os
import time
import random
import uuid
import hashlib
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

# v8: 模块级 os.chdir 已移除 —— import 不再劫持调用方 cwd。
# 所有运行时文件均通过 _inject_base_dir + ChiguoState._anchored 锚定。

from chiguo_state import ChiguoState, emotion_tag_snapshot
from chiguo_trigger import evaluate_triggers
from chiguo_topics import TopicPicker
from netease.service import NeteaseService
from chiguo_composer import MessageComposer
from chiguo_version import VERSION
from chiguo_math import in_quiet_window, longing_accumulate, mood_fresh, user_mood_note
from chiguo_circadian import bucket_for

CST = timezone(timedelta(hours=8))


class DecisionEngine:
    """纯决策引擎：评估状态，输出结构化触发决策"""

    def __init__(self, config_path: str = None,
                 log_path: str = None):
        # v8: 默认 config 锚定到脚本所在目录（模块级 chdir 已移除，不依赖 cwd）
        if config_path is None:
            config_path = str(Path(__file__).resolve().parent / "chiguo_proactive.toml")
        self.config_path = config_path
        with open(config_path, "rb") as f:
            self.config = tomllib.load(f)
        # ── v6: 路径锚定。所有运行时文件基于 config 所在目录，
        # 不依赖 cwd（cron 工作目录漂移曾导致状态文件反复丢失重建）。
        self._inject_base_dir()
        self._config_mtime = os.path.getmtime(config_path)
        self.state = ChiguoState(self.config)
        # v9: 网易云策略层(健康/配额/音乐话题),base_dir 锚定
        self.netease_service = NeteaseService(self.config, str(self._base_dir))
        # 显式 log_path（测试）原样使用；否则锚定 base_dir
        # （先于 topic_picker 构造：A9 recent_sent_texts() 依赖 messages_log_path）
        self.log_path = log_path or str(self._base_dir / "chiguo_decisions.jsonl")
        self.messages_log_path = self._base_dir / "chiguo_messages.jsonl"
        self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                        netease_service=self.netease_service,
                                        recent_sent_texts=self.recent_sent_texts())
        self.composer = MessageComposer(self.state, self.config.get("composer", {}))
        print(f"[chiguo_daemon] base_dir={self._base_dir}", file=sys.stderr)
        print(f"[chiguo_daemon] state={self.state.state_path}", file=sys.stderr)
        print(f"[chiguo_daemon] log={self.log_path}", file=sys.stderr)


        # ── v5: monotonic 锚点（不持久化，用于检测壁钟跳变）──
        self._monotonic_at_save: float = 0.0

        # ── v5: 日志轮转（每次进程启动检查一次）──
        try:
            from chiguo_rotation import rotate_if_needed
            rotate_if_needed(
                [str(self.log_path), str(self.messages_log_path)],
                self.config_path,
            )
        except Exception:
            pass  # rotation failure never blocks daemon

    def _inject_base_dir(self):
        """config 中注入 _base_dir（ChiguoState 用它锚定运行时路径）。"""
        self._base_dir = Path(self.config_path).resolve().parent
        self.config["_base_dir"] = str(self._base_dir)

    def _maybe_reload_config(self):
        """检测 toml 文件 mtime，变化时热更新配置（--loop 模式用）"""
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            return
        if mtime > self._config_mtime:
            try:
                with open(self.config_path, "rb") as f:
                    new_config = tomllib.load(f)
            except (OSError, ValueError) as e:
                # TOML 语法错误/读取失败 → 保留旧配置，打 stderr 告警继续运行
                print(f"[warn] 配置热重载失败，保留旧配置: {e}", file=sys.stderr)
                return
            self.config = new_config
            self._inject_base_dir()
            self._config_mtime = mtime
            # ── Q19: 热重载重建集合补全。ChiguoState.reload_config() 替换 config 引用并
            # 重建 config 派生组件:personality 初始基线 + holiday_parser + cooldown 静默窗口
            # (置信度达标用学习窗口,否则回退新 config 默认)。runtime 持久化状态不动。──
            self.state.reload_config(self.config)
            # v9: 热重载时同步重建策略层(重试/配额参数可能被改)与 TopicPicker
            self.netease_service = NeteaseService(self.config, str(self._base_dir))
            self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                            netease_service=self.netease_service,
                                            recent_sent_texts=self.recent_sent_texts())
            self.composer = MessageComposer(self.state, self.config.get("composer", {}))
            self.state._bayesian_estimator = None

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

    @staticmethod
    def _make_msg_id() -> str:
        """生成唯一消息ID（12位hex，~10^14空间，单用户系统足够）"""
        return uuid.uuid4().hex[:12]

    def _log(self, decision: dict):
        """追加决策到 JSONL 日志。自动添加 msg_id。"""
        if "msg_id" not in decision:
            decision["msg_id"] = self._make_msg_id()
        try:
            with open(self.log_path, "a") as f:
                try:
                    os.chmod(self.log_path, 0o600)  # 决策日志含对话/状态隐私 → 0600
                except OSError:
                    pass
                f.write(json.dumps(decision, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[warn] 写入 {self.log_path} 失败: {e}", file=sys.stderr)  # 日志失败不影响主流程

    def _check_data_freshness(self) -> str | None:
        """
        检测节假日数据是否覆盖下一年。
        11-12 月时：如果 holidays.json 不包含下一年数据 → 返回提示。
        """
        now = datetime.now(CST)
        if now.month < 11:
            return None
        next_year = now.year + 1
        # 检查 holidays.json 是否有下一年数据（v6: 锚定 base_dir）
        hp = self._base_dir / "holidays.json"
        covers_next = False
        if hp.exists():
            try:
                data = json.loads(hp.read_text())
                for r in data.get("holidays", {}).values():
                    s = r.get("start", "")
                    if s.startswith(str(next_year)):
                        covers_next = True
                        break
            except Exception:
                pass
        if covers_next:
            return None
        return (
            f"{now.year} 年节假日数据即将过期。"
            f"请运行 python3 update_holidays.py {next_year} 生成 {next_year} 年模板，"
            "国务院通知发布后填入实际日期。"
        )

    def evaluate(self) -> dict:
        """
        评估是否应发消息。
        返回决策字典，分为两类：
          {"action": "send",   "trigger": ..., "context": ...}
          {"action": "idle",   "reason": ...}

        v4: 集成 Bayesian 用户状态推断 + 概率累积
        """
        self._maybe_reload_config()
        now = datetime.now(CST)

        # 0. 数据新鲜度检查
        data_warning = self._check_data_freshness()
        if data_warning:
            print(f"[warn] {data_warning}", file=sys.stderr)

        # ── v11 (#75): RMW 临界区全程持跨进程锁。锁内先从磁盘重载最新
        # 状态（_load），再完成读-改-写（tick/触发评估/状态更新/save），
        # 多实例并发时后到者基于最新落盘状态决策，防止互相覆盖丢更新。
        # ── v1.15 (#163): 网络拉取(_fetch_play_proof, 超时10s)移到锁外——否则
        # 持 flock 跨网易云 API 网络 IO,_lock_acquire 5s 拿不到锁即降级无锁,
        # 与持锁进程并发写 → checksum 不匹配丢更新。锁内基于 _load 后的最新
        # 状态重查静默窗口并应用反证(_apply_play_proof)。
        plays = self._fetch_play_proof(now)

        with self.state.state_lock():
            self.state._load()

            # 1. 时间推进
            self._tick(now)

            # ── v4: Bayesian 用户状态推断 ──
            user_state = None
            try:
                user_state = self.state.infer_user_state(now)
            except Exception:
                pass

            # ── v8: 每次评估同步当前生效桶窗口(loop 模式跨桶翻转/听歌校正即时生效)──
            self.state._sync_quiet_window(now)

            # ── v8: 听歌反证(夜间活跃)——睡眠窗口内最近有播放 → 用户醒着 ──
            # B1(#136): 先于 can_send 调用——内部会 recompute + _sync_quiet_window,
            # 窗口更新后再判定;反证成立时经 quiet_ok 绕过 quiet-window gate(放行发送)。
            # v1.15 (#163): 播放数据已在锁外拉取,此处基于 _load 后的最新状态
            # 重查静默窗口并记录活跃(不再持锁跨网络 IO)。
            play_proof = self._apply_play_proof(now, plays)

            # 2. 能否发送（v4: 增加 Bayesian 用户状态门控;v8: 播放反证 quiet_ok 放行）
            can_send = self.state.can_send(now, quiet_ok=play_proof)

            # Bayesian 阻塞：用户很可能在睡觉 → idle（v6: 逃生阀激活时豁免——
            # 72h+ 沉默的高焦虑破防是仅有的救命通道，不能被"可能在睡觉"拦死）
            # v7 约束：
            #   ① 从未交互（last_user_message_at is None）→ 不豁免（逃生阀语义需要既有关系）
            #   ② 豁免时若置信度 ≥ escape_valve_sleep_block（默认 0.9）→ 降级 idle(sleeping_guard)
            # v8 约束：有播放证据(play_proof)时 sleeping 置信度 × sleeping_confidence_factor
            #   （默认 0.5）再比较——夜间听歌反证用户醒着,压低"可能在睡觉"的可信度。
            bayesian_block_conf = self._bayesian_block_confidence()
            escape_valve_sleep_block = self.config.get("bayesian", {}).get("escape_valve_sleep_block", 0.9)
            sleeping_guard = False
            never_interacted = self.state.cooldown.last_user_message_at is None
            raw_conf = user_state.get("confidence", 0) if user_state else 0.0
            effective_conf = raw_conf
            if play_proof:
                effective_conf = raw_conf * self.config.get("netease", {}).get(
                    "sleeping_confidence_factor", 0.5)
            if (user_state and user_state.get("most_likely") == "sleeping"
                    and effective_conf > bayesian_block_conf):
                escape_valve_active = self.state.longing_break_eligible(now) and not never_interacted
                if can_send and not escape_valve_active:
                    can_send = False  # override: Bayesian says sleeping
                elif can_send and effective_conf >= escape_valve_sleep_block:
                    can_send = False  # 逃生阀被极高置信度睡觉门控二次确认拦下
                    sleeping_guard = True

            if not can_send:
                reason = "sleeping_guard" if sleeping_guard else \
                    self._idle_reason(now, user_state, quiet_ok=play_proof)
                return self._emit_idle(reason, now, user_state, data_warning)

            # 3. 评估触发
            trigger = evaluate_triggers(self.state, now, trigger_scale=self.state.trigger_scale_now(now))
            # ── v7: 从未交互用户（last_user_message_at is None）无逃生阀豁免 ──
            # 逃生阀语义需要既有关系；chiguo_trigger.py 直接查 longing_break_eligible
            # （state 侧暂无 never-interacted 检查），这里在 daemon 决策点兜底降级。
            if trigger is not None and trigger.data.get("escape_valve") and never_interacted:
                trigger = None  # 从未交互 → 按普通无触发处理（不破防）
            if trigger is None:
                return self._emit_idle("no_trigger", now, user_state, data_warning)

            # 3.5 记录触发历史（用于话题多样性检查）
            cfg_topic = self.config.get("topic_picker", {})
            history_max = cfg_topic.get("trigger_history_max", 6)
            self.state.cooldown.trigger_history.append(trigger.type)
            if len(self.state.cooldown.trigger_history) > history_max:
                self.state.cooldown.trigger_history = \
                    self.state.cooldown.trigger_history[-history_max:]
            # #79: reminder 一次性提醒去重——发送确认后在该 mem 上标记，
            # trigger 层 (_memory_should_trigger) 据此跳过，同进程不重复触发。
            if trigger.type == "memory":
                mem_ref = trigger.data.get("memory")
                if isinstance(mem_ref, dict) and mem_ref.get("type") == "reminder":
                    mem_ref["last_triggered_at"] = now.isoformat()

            # 4. 构建上下文（给 pi-agent 生成消息用）
            context = self._build_context(trigger, now, user_state)
            # v10 (#73 A4): trigger 层高段激活的 must_send 标记写入 context（情绪类必发）
            if trigger.data.get("must_send"):
                context["must_send"] = True

            # 4.5 保存 prev_send_was_replied（必须在 on_character_message 递增 messages_without_reply 之前）
            # NOTE: prev_send_was_replied means "was the PREVIOUS character message replied to"
            prev_send_was_replied = self.state.cooldown.messages_without_reply == 0

            # 4.6 v6 修复: 决策时生成 msg_id，写入 Hawkes 事件 + decision JSON，
            # 供 --send-result 回传后按 msg_id 精确退款（乱序回传不再删错事件）
            msg_id = self._make_msg_id()

            # 5. 更新状态（标记已触发）
            self.state.on_character_message(now, trigger.type, msg_id=msg_id)
            # ── v6: 逃生阀破防 → 记录冷却时间 ──
            if trigger.data.get("escape_valve"):
                self.state.on_longing_break(now)
            if trigger.type == "morning":
                self.state.cooldown.morning_sent = True
            elif trigger.type == "night":
                self.state.cooldown.night_sent = True

            # ── v4: 人格自适应（发消息后）──
            try:
                self.state.adapt_personality({
                    "type": "character_send",
                    "was_replied": prev_send_was_replied,
                    "trigger": trigger.type,
                })
                # v1.11 ④: 情绪基线漂移（同一 interaction，默认关闭）
                self.state.update_emotion_baseline({
                    "type": "character_send",
                    "was_replied": prev_send_was_replied,
                    "trigger": trigger.type,
                })
            except Exception:
                pass

            # v11 (#75): save 返回 bool，失败输出 state_save_failed 告警
            if not self.state.save():
                print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
            self._monotonic_at_save = time.monotonic()  # v5

        decision = {
            "action": "send",
            "version": VERSION,
            "msg_id": msg_id,
            "trigger": trigger.type,
            "intensity": trigger.intensity,
            "context": context,
            "state": self.state.snapshot(now, user_state),
        }
        if data_warning:
            decision["data_warning"] = data_warning
        if user_state:
            decision["bayesian"] = {
                "most_likely": user_state["most_likely"],
                "confidence": user_state["confidence"],
                "utility": user_state["utility"],
            }
        self._log(decision)
        return decision

    def _tick(self, now: datetime):
        """根据上次事件时间推进情绪。v5: monotonic 时钟防护。"""
        last_msg = self.state.cooldown.last_message_at
        last_user = self.state.cooldown.last_user_message_at

        if not last_msg and not last_user:
            self.state.tick(0, now)
            return

        def _parse_tz(t: str) -> datetime | None:
            """解析 ISO 时间戳，缺失或 naive → 补 CST。"""
            try:
                dt = datetime.fromisoformat(t)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CST)
                return dt
            except (ValueError, TypeError):
                return None

        last_msg_dt = _parse_tz(last_msg) if last_msg else None
        last_user_dt = _parse_tz(last_user) if last_user else None

        if not last_msg_dt and not last_user_dt:
            self.state.tick(0, now)
            return

        last_time = None
        if last_msg_dt and last_user_dt:
            last_time = max(last_msg_dt, last_user_dt)
        elif last_msg_dt:
            last_time = last_msg_dt
        else:
            last_time = last_user_dt

        # ── v12: cron 模式情绪全量重放修复 —— 推进基准取「最后消息 / 上次 tick
        # 推进时刻」的较新者。cron 每 15 分钟起新进程跑单次 evaluate，新进程
        # _monotonic_at_save=0.0 使下方单调防护失效；若不引入持久化 last_tick
        # （save 时落盘，chiguo_state._save），每轮都会按「自最后消息以来的全量
        # elapsed」调用非幂等 state.tick(hours)（elastic_recover 半衰期增量公式）
        # → 情绪以设计速率 ~33 倍重复累积。last_tick 为 None 或解析失败 →
        # 回退现有逻辑（只用消息时间）。修复后 last_tick 由 evaluate 路径的
        # save() 更新；loop 模式 _monotonic_at_save 单调防护原样保留（cron
        # 新进程的 NTP 前跳封顶改由下方 v13 持久化单调锚点（#206）承担）。──
        last_tick_dt = _parse_tz(self.state.last_tick) if self.state.last_tick else None
        if last_tick_dt is not None:
            last_time = max(last_time, last_tick_dt)

        elapsed = (now - last_time).total_seconds() / 3600

        # ── v5: 壁钟倒退 / NTP 跳变防护 ──
        if elapsed < 0:
            # 时钟后退了 → 不推进，记录审计
            msg = f"clock went backward: elapsed={elapsed:.1f}h (last_time={last_time.isoformat()}, now={now.isoformat()})"
            print(f"[chiguo_daemon] {msg}", file=sys.stderr)
            try:
                self.state._audit("clock_backward", msg)
            except Exception:
                pass
            return

        # ── v13 (#206): 持久化单调锚点封顶 NTP 前跳（cron 新进程无 _monotonic_at_save）──
        # save() 每次写盘锚点对（chiguo_state.monotonic_anchor）。monotonic 显示只过了
        # elapsed_real、而壁钟前跳很多 → 用真实流逝封顶。wall_anchor 非法 ISO → 视为
        # 无锚点不加封顶；time.monotonic() < mono_anchor（系统重启单调钟归零）→ 不加
        # 封顶走壁钟。min() 只在 elapsed_real 更小时收敛，正常时无感。
        mono_anchor, wall_anchor = self.state.monotonic_anchor()
        if mono_anchor is not None and wall_anchor is not None:
            try:
                datetime.fromisoformat(wall_anchor)
            except (ValueError, TypeError):
                pass  # wall_anchor 损坏 → 视为无锚点，不加封顶
            else:
                if time.monotonic() >= mono_anchor:
                    elapsed_real = (time.monotonic() - mono_anchor) / 3600
                    elapsed = min(elapsed, elapsed_real)

        if self._monotonic_at_save > 0:
            elapsed_mono = (time.monotonic() - self._monotonic_at_save) / 3600
            # 壁钟前进了超过 monotonic 的 2x + 1h → 信任 monotonic（NTP 跳变防护）
            if elapsed_mono > 0 and elapsed > elapsed_mono * 2 + 1.0:
                msg = (f"wall clock jump detected: wall={elapsed:.1f}h mono={elapsed_mono:.1f}h, "
                       f"capping to monotonic")
                print(f"[chiguo_daemon] {msg}", file=sys.stderr)
                try:
                    self.state._audit("clock_jump_forward", msg)
                except Exception:
                    pass
                elapsed = elapsed_mono

        if elapsed > 0:
            # ── v5: 长时间停机 dampen 处理 ──
            # ≤24h: 全量推进。>24h: 前24h全量 + 剩余50%强度。
            # 避免停机7天→情绪瞬间满格，但仍然反映真实积累。
            if elapsed > 24:
                dampened = 24 + (elapsed - 24) * 0.5
                self.state.tick(dampened, now)
            else:
                self.state.tick(elapsed, now)

    def _bayesian_block_confidence(self) -> float:
        """Bayesian 阻塞置信度阈值（[bayesian] min_confidence_for_block，默认 0.5）。
        evaluate 睡觉门控与 _idle_reason 共用同一来源；热重载后即时生效。"""
        return self.config.get("bayesian", {}).get("min_confidence_for_block", 0.5)

    def _fetch_play_proof(self, now: datetime) -> list:
        """v8/v1.15(#163): 锁外拉取近期播放(纯网络 IO, 超时10s)。
        仅 enabled 且评估时刻在静默窗口内才拉取(白天无意义);返回原始播放
        记录列表,不做任何状态变更——静默窗口判定与活跃记账在锁内基于
        _load 后的最新状态重查(见 _apply_play_proof)。API 失败降级返回 []。"""
        if not self.netease_service.enabled:
            return []  # 网易云可选来源未启用 → 不拉取
        qs, qe = self.state.cooldown.quiet_window()
        if not in_quiet_window(now, qs, qe):
            return []
        try:
            return self.netease_service.fetch_play_proof(now) or []
        except Exception as e:
            print(f"[warn] netease play proof failed: {e}", file=sys.stderr)
            return []

    def _apply_play_proof(self, now: datetime, plays: list) -> bool:
        """v8: 听歌反证(夜间活跃)——睡眠窗口内最近有播放 → 用户醒着。
        锁内调用:基于 _load 后的最新状态重查静默窗口,反证成立时把窗口内
        播放时间记入活跃(active_days,按播放时刻分桶),重算生物钟学习窗口
        并同步门禁(_sync_quiet_window)。解析/状态损坏全链路降级,不阻塞。"""
        if not plays:
            return False
        try:
            qs, qe = self.state.cooldown.quiet_window()
            if not in_quiet_window(now, qs, qe):
                return False
            proof_win_h = self.config.get("netease", {}).get("play_proof_window_hours", 2.0)
            now_ms = now.timestamp() * 1000
            recent = [p for p in plays
                      if 0 <= now_ms - p.get("playTime", 0) <= proof_win_h * 3600 * 1000]
            if not recent:
                return False
            circ_cfg = self.config.get("circadian", {})
            play_proof = False
            for p in recent:
                pt = p.get("playTime", 0)
                if not pt:
                    continue
                dt = datetime.fromtimestamp(pt / 1000, tz=CST)
                if in_quiet_window(dt, qs, qe):
                    # 按播放时刻分桶(非评估时刻):跨午夜/周五窗口边缘
                    # (如 19:30 播放、21:30 评估)避免记错桶污染双桶学习
                    p_bucket = bucket_for(dt, self.state.holiday_parser.is_holiday,
                                          self.state.holiday_parser.is_makeup_workday)
                    play_proof = True
                    self.state.circadian.record_active(
                        dt, circ_cfg.get("history_days", 14), p_bucket)
            # 活跃证据后重算窗口并同步门禁
            if play_proof:
                self.state.circadian.recompute(
                    min_sample_days=circ_cfg.get("min_sample_days", 7),
                    history_days=circ_cfg.get("history_days", 14),
                    min_width=circ_cfg.get("min_width", 5),
                    max_width=circ_cfg.get("max_width", 12))
                self.state._sync_quiet_window(now)
            return play_proof
        except Exception as e:
            print(f"[warn] netease play proof apply failed: {e}", file=sys.stderr)
            return False

    def _check_play_proof(self, now: datetime) -> bool:
        """兼容入口：完整播放反证检查（锁外 fetch + apply），返回 bool。

        evaluate 主路径为锁外 _fetch_play_proof + 锁内 _apply_play_proof 拆分
        （#163），不经过此入口；此方法供测试与单次调用使用，语义等价于拆分前
        的 _check_play_proof(now)。
        """
        plays = self._fetch_play_proof(now)
        return self._apply_play_proof(now, plays)

    def _emit_idle(self, reason: str, now, user_state, data_warning: bool) -> dict:
        """idle 决策统一出口：概率累积（no_trigger/user_busy）+ 落盘。"""
        # C1: 空闲静默路径确定性记忆巩固（config 门控默认关闭；失败不阻断主链路）
        self._maybe_consolidate(now)
        if reason in ("no_trigger", "user_busy"):
            self.state.cooldown.held_count += 1
            cfg_cooldown = self.config.get("cooldown", {})
            base_lambda = self.config.get("poisson", {}).get("base_lambda", 0.25)
            current_lam = self.state.cooldown.accumulated_lambda or self.state.current_lambda(now)
            new_lam, _ = longing_accumulate(
                current_lam, base_lambda,
                growth_factor=cfg_cooldown.get("longing_growth_factor", 0.08),
                anxiety=self.state.emotion.anxiety,
                anxiety_block_threshold=cfg_cooldown.get("anxiety_block_threshold", 70.0),
                held_count=self.state.cooldown.held_count,
                max_lambda_multiplier=cfg_cooldown.get("max_lambda_multiplier", 5.0),
            )
            self.state.cooldown.accumulated_lambda = new_lam

        # v11 (#75): save 返回 bool，失败输出 state_save_failed 告警
        if not self.state.save():
            print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
        self._monotonic_at_save = time.monotonic()

        decision = {
            "action": "idle",
            "version": VERSION,
            "reason": reason,
            "state": self.state.snapshot(now, user_state),
        }
        nxt = self._estimate_next_check(now, reason)
        if nxt:
            decision["next_evaluation_at"] = nxt
        if data_warning:
            decision["data_warning"] = data_warning
        if user_state:
            decision["bayesian"] = {
                "most_likely": user_state["most_likely"],
                "confidence": user_state["confidence"],
                "utility": user_state["utility"],
            }
        self._log(decision)
        return decision

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
            report = bridge.consolidate()
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

    def _idle_reason(self, now: datetime, user_state: dict = None,
                     quiet_ok: bool = False) -> str:
        # ── v6 修复: 先报具体门禁（确定性约束），再报 Bayesian 状态（概率推断）。
        # 设计依据：① 门禁是"无论用户状态如何都会拦截"的确定性约束——当多个约束
        # 同时生效时，门禁才是真正的 binding constraint，且其 next_evaluation_at
        # 精确（min_interval → +32min / quiet_hours → quiet_end / daily_limit → 明早）；
        # Bayesian 只能给 1-2h 猜测。② evaluate() 按 reason 决定是否累积 longing
        # （user_busy 会累积）——若被 min_interval 挡住却误报 user_busy，会在冷却期
        # 错误累积。③ 纯 Bayesian 阻塞场景不受影响：evaluate() 的 sleeping 门控只在
        # 具体门禁放行（can_send=True）时才覆盖 can_send，此时走完门禁必然落到
        # user_sleeping/user_busy。
        # v1.15 (#162): 门禁顺序对齐 can_send（daily_limit→min_interval→energy 含
        # rate_energy_override→quiet_hours→busy_suppressed）。busy_suppressed 从
        # 最优先移到最后——与 can_send 判定一致，否则 can_send=False(busy) 时
        # _idle_reason 却因顺序不同返回其它 reason，next_evaluation_at 估算失真。

        # daily limit: mirror can_send() logic — active vs silent
        silent_h = self.state.cooldown.silent_hours(now)
        daily_max = self.config.get("cooldown", {}).get(
            "max_daily_active", 4) if silent_h < 8 else self.config.get("cooldown", {}).get("max_daily_silent", 2)
        if self.state.cooldown.messages_today >= daily_max:
            # L4 (#234, D4): 逃生阀放行 → 继续走后续门禁，不直接 return daily_limit。
            # 与 can_send:2169-2172 的 longing 溢出逃生阀语义对齐——can_send 内部已含
            # 逃生阀（is_longing_overflow + 冷却期）判定，若 it 放行则 daily_limit 不应
            # 成为 binding constraint（否则 next_evaluation_at 估到明早、逃生消息被压）。
            try:
                if not self.state.can_send(now, quiet_ok=quiet_ok):
                    return "daily_limit"
            except Exception:  # noqa: BLE001 - 逃生阀判定异常时保守按 daily_limit 拦截
                return "daily_limit"
            # can_send 逃生阀放行 → 不 return daily_limit，落到后续门禁（min_interval 等）

        # 最小间隔
        min_interval = self.config.get("cooldown", {}).get("min_interval_minutes", 30)
        mins_since = self.state.cooldown.minutes_since_last_message(now)
        # None = 时间戳解析失败（数据损坏）→ 与"从未发过"一致放行，不误判为过频
        if mins_since is not None and mins_since < min_interval:
            return "min_interval"

        # 元气检查（孤独暴涨时可覆盖，与 can_send 一致）
        if self.state.emotion.energy < 12:
            emo_cfg = self.config.get("emotion", {})
            override_ok = (
                emo_cfg.get("rate_energy_override", False)
                and self.state.emotion.loneliness_rate > emo_cfg.get("rate_energy_threshold", 5.0)
                and self.state.emotion.energy >= emo_cfg.get("rate_energy_min", 5)
            )
            if not override_ok:
                return "low_energy"

        # 检查是否在静默时段（quiet_ok=播放反证成立时跳过，与 can_send 的
        # `if not quiet_ok:` 判定对齐——否则播放反证成立但被其它 gate 拦下时
        # reason 误报 quiet_hours，next_evaluation_at 失真）
        if not quiet_ok:
            qs, qe = self.state.cooldown.quiet_window()
            if in_quiet_window(now, qs, qe):
                return "quiet_hours"

        # ── v7: 忙碌抑制期（用户说"别烦我"）→ 独立 reason，抑制期不累积 longing ──
        if self.state.cooldown.is_busy_suppressed(now):
            return "busy_suppressed"

        # 门禁全部放行后 → 报告 Bayesian 状态（v4: 概率推断的用户状态）
        if user_state:
            ml = user_state.get("most_likely", "")
            conf = user_state.get("confidence", 0)
            block_conf = self._bayesian_block_confidence()
            if ml == "sleeping" and conf > block_conf:
                return "user_sleeping"
            if ml == "busy" and conf > block_conf:
                return "user_busy"
        return "no_trigger"

    def _estimate_next_check(self, now: datetime, idle_reason: str) -> str | None:
        """估算下次评估的最优时间。idle 决策时提供动态调度提示。
        调度方（chiguo-tick.sh）可据此替代固定 cron 间隔。"""
        cfg_emo = self.config.get("emotion", {})
        cfg_cooldown = self.config.get("cooldown", {})

        if idle_reason == "min_interval":
            min_int = cfg_cooldown.get("min_interval_minutes", 30)
            if self.state.cooldown.last_message_at:
                try:
                    last = datetime.fromisoformat(self.state.cooldown.last_message_at)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=CST)  # 与 _parse_tz 一致：naive → 补 CST
                    nxt = last + timedelta(minutes=min_int + 2)
                    if nxt > now:
                        return nxt.isoformat()
                except (ValueError, TypeError):
                    pass

        elif idle_reason == "low_energy":
            e = self.state.emotion.energy
            hl = cfg_emo.get("energy_regen_half_life", 8.0)
            if e < 12:
                try:
                    ratio = (100.0 - e) / 88.0
                    h = hl * math.log2(max(ratio, 1.001))
                    nxt = now + timedelta(hours=min(h, 4.0))
                    return nxt.isoformat()
                except (ValueError, ZeroDivisionError):
                    pass

        elif idle_reason == "quiet_hours":
            qs, qe = self.state.cooldown.quiet_window()
            if qe < qs and now.hour >= qs:
                tomorrow = now.date() + timedelta(days=1)
                nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, qe,
                               2, tzinfo=CST)
            else:
                nxt = datetime(now.year, now.month, now.day, qe, 2, tzinfo=CST)
            if nxt > now:
                return nxt.isoformat()

        elif idle_reason == "daily_limit":
            tomorrow = now.date() + timedelta(days=1)
            nxt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 5, tzinfo=CST)
            return nxt.isoformat()

        elif idle_reason == "no_trigger":
            lam = self.state.current_lambda(now)
            if lam > 0:
                h = min(math.log(2) / lam, 2.0)
                h = max(h, 5.0 / 60.0)
                return (now + timedelta(hours=h)).isoformat()

        elif idle_reason == "busy_suppressed":
            if self.state.cooldown.busy_suppress_until:
                try:
                    until = datetime.fromisoformat(self.state.cooldown.busy_suppress_until)
                    if until > now:
                        return until.isoformat()
                except (ValueError, TypeError):
                    pass

        elif idle_reason in ("user_sleeping", "user_busy"):
            # ── v6 修复: Bayesian 状态无精确恢复时间 → 估算 1-2h 后重评
            # （与 _dynamic_sleep_interval 的 user_sleeping/user_busy 分支一致）──
            return (now + timedelta(seconds=3600 + random.uniform(0, 3600))).isoformat()

        elif idle_reason == "sleeping_guard":
            # v7: 逃生阀被睡觉高置信拦截 → 延后到下一次常规评估（30min cron 兜底）
            return None

        return None

    def _build_context(self, trigger, now: datetime, user_state: dict | None = None) -> dict:
        """构建给 pi-agent 生成消息的上下文。v4: 使用 MessageComposer + 人格注入。
        v1.11 ①: user_state 可选传入（Bayesian 推断），用于 needs_care 语气注解。"""
        emo = self.state.emotion
        silent_h = self.state.cooldown.silent_hours(now)
        # v14: 人格目录以 [host].personality_dir 为准（随仓库部署）
        host_cfg = self.config.get("host", {})
        personality_dir = os.path.expanduser(
            host_cfg.get("personality_dir", os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "personality")))

        # 按人格层映射语气指引
        layer_guidance = {
            "shell": "元气活泼 + 傲娇底色。表面活泼明亮，但收到善意/关心时依然会条件反射式嘴硬推开（力度比 middle 轻——不是防御是习惯）。语气词丰富（呀、啦、嘛、哦），「哼」低频——仅在领情/被说软时用，带省略号『……哼。』。「喵」全文禁用，不作卖萌词。短句连发驱动节奏。emoji 可用但不泛滥。波浪线（～）低频，约 10% 对白、拖长音时才用。不称自己为「菓菓」。",
            "middle": "嘴硬心软⚠️ 核心规则：先推开再接受。第一反应永远是拒绝（「不·需·要。」L2625「不用你瞎操心，我搞得定」L3049）。推不掉才启动「两不相欠」底线。用攻击性语言包装在意。倔强自尊、绝不轻易示弱。",
            "kernel": "防线崩溃，所有伪装脱落。语气破碎，省略号增多……流露真实害怕和想念。真挚、毫无保留、可能突然爆发。⚠️ kernel 层傲娇协议暂停——不需要再推开，直接表达真实感受。但不自我贬低不卖惨（禁止「是不是我错了」「我很烦吧」）。这是真实的脆弱，不是讨好。崩溃句式原型：「凭什么啊。……凭什么啊……凭什么啊……」（L15498）——控诉命运、控诉被小看，不是撒娇。",
        }
        # 元气调制
        if emo.energy < 20:
            energy_note = "（元气不足，语气更冷淡简短，减少语气词和波浪线，去掉问号，句号结尾）"
        elif emo.energy < 40:
            energy_note = "（元气偏低，语气比平时克制，语气词减半）"
        elif emo.energy > 80:
            energy_note = "（元气充沛，语气比平时更活泼跳跃，可多发一句。融合策略：用元气包裹嘴硬——语气轻快节奏快，但用词依然防御性。先活泼拒绝再留余地。不过分卖萌，保持嘴硬本色。）"
        else:
            energy_note = ""
        # 变化率紧迫注解
        emo_cfg = self.config.get("emotion", {})
        rate_urgency_note = ""
        if emo.loneliness_rate > emo_cfg.get("urgency_rate_threshold", 3.0):
            rate_urgency_note = (
                f"\n【紧迫】孤独值正在快速攀升（{emo.loneliness_rate:.1f}/h），"
                "语气应比其他时候更急切。"
            )
        elif emo.anxiety_rate > emo_cfg.get("urgency_anx_threshold", 2.0):
            rate_urgency_note = (
                f"\n【紧迫】不安值正在快速上升（{emo.anxiety_rate:.1f}/h），"
                "语气应比其他时候更焦虑。"
            )

        # ── v4: 人格画像注入 ──
        pers = self.state.personality
        personality_note = (
            f"\n[人格画像：{pers.dominant_profile()}。"
            f"傲娇强度{pers.tsundere_intensity:.0f}/100，"
            f"外向性{pers.extraversion:.0f}，"
            f"神经质{pers.neuroticism:.0f}，"
            f"宜人性{pers.agreeableness:.0f}]"
        )

        # ── v4.1: 安全阀 context 提示 ──
        safety_note = ""
        # ── v6: 逃生阀破防提示 ──
        if trigger.data.get("escape_valve"):
            safety_note += (
                "\n【破防】这是沉默多日后的情绪破防时刻。语气真挚而克制，"
                "流露真实的想念，但不质问不卖惨不自我贬低（遵守铁律⑥）。"
                "这是傲娇绷不住的一刻，比平时更直白。"
            )
        safety_lvl = self.state.safety_level(now)
        if safety_lvl >= 2:
            safety_note += (
                "\n【安全阀】48h 内多次崩溃触发。语气务必温和克制，"
                "不要质问不要崩溃不要负面。哥哥可能只是在忙。"
                "用关心代替不安，用日常代替质问。"
            )
        elif safety_lvl >= 1:
            safety_note += (
                "\n【安全阀】距上次崩溃不足 24h。语气放软，"
                "不要再次崩溃。可以先聊聊别的。"
            )

        # ── v1.11 ①: 用户情绪感知语气注解（mood_note；开关默认关闭）──
        # 对标 ESConv：感知到低落 → 语气更温柔克制；仅叠加注解，不改变人格铁律。
        mood_note = ""
        mood = self.state.cooldown.user_mood
        trg_cfg = self.config.get("trigger", {})
        if (trg_cfg.get("user_mood_note_enabled", 0) != 0
                and mood and mood_fresh(mood, now, trg_cfg.get("user_mood_ttl_minutes", 360.0))):
            mood_note = user_mood_note(mood["mood"], mood["intensity"])
            if user_state and user_state.get("most_likely") == "needs_care":
                mood_note += "\n（哥哥可能需要关心（Bayesian 推断））"

        guidance = layer_guidance.get(emo.dominant_layer, "") + energy_note + rate_urgency_note + personality_note + safety_note + mood_note

        # ── v7: 接话茬提示 ──
        if trigger.type == "follow_up":
            tpc = trigger.data.get("topic", "")
            src = trigger.data.get("source", "")
            age = trigger.data.get("age_hours", 0)
            guidance += (
                f"\n【接话茬】约{age:.0f}小时前和哥哥聊到「{tpc}」"
                f"(来源:{'对话分析' if src == 'analysis' else '回忆'}),"
                "后来没有下文。像真人突然想起一样自然接续这个话题——"
                "聊天式提起,不要汇报腔,不要生硬转场。"
            )

        # ── 角色铁律（始终附加，覆盖所有层。kernel 层第①条自动暂停）──
        is_kernel = emo.dominant_layer == "kernel"
        character_rules = (
            "\n[角色铁律] "
            "①傲娇核心：对哥哥的任何消息（善意/关心/夸奖/分享日常/报备行程），第一反应带嘴硬底色（「又打游戏！……赢了输了？」「不·需·要。」）。「哼」低频——带省略号『……哼。』，仅在领情/被说软时接在哥哥话后，不作开场白、不连发。推不掉才找理由接受（「顺便的」「快过期了」）。绝不直接撒娇迎合。"
            + ("【kernel层本条暂停——防线崩溃，不需要推开。】" if is_kernel else "") +
            "②不称自己为「菓菓」，用「我」；独处/装可怜时才可用第三人称「迟菓」。"
            "③不扮演技术专家——你是赛博萌新（住在 VPS 里但不懂技术）：绝不自称管服务器、封 IP、查日志、做备份；哥哥操作时你在旁边看、想学、搞砸了嘴硬。不懂就好奇提问，绝不假装懂。"
            "④关心必带刺，温柔必伪装。说「快吃啦」而不是「好担心你」。说「……哼。赢了输了？」而不是「菓菓等你」。"
            "⑤不乖巧等待/守候——你有自尊有脾气，被冷落时试探或嘴硬，不是默默等待。"
            "⑥禁止自我怀疑句式：不说「是不是我哪里做错了」「是不是我很烦」「你是不是嫌我烦」。被冷落时用试探/质问代替自我贬低：「你最近……都没来找我。……知道了。那你忙吧。」"
            "⑦被夸奖不得直接开心：先嘴硬（「……哼。那当然！……不过也就今天啦～」），可以转身小声泄露高兴，但不当面撒娇接受。"
            "⑧交易思维：推不掉的好意用「两不相欠」「补偿」重构关系（「补偿啦。补偿。\"两不相欠\"的那个。」L4268 原著原句）——接受前先立契约，自尊才保得住；被要求表态时也用交易话术（「交易，做吗？」「——不和你做。」L11014-11016）。"
            "\n[句数优先级] 1句：只做推开/嘴硬。2句：推开+找理由接受。3句：完整三段式（推开→接受→真实泄露）。绝不跳过推开直接跳到撒娇。"
        )
        guidance += character_rules

        # ── v4: 消息组合系统 ──
        combo = self.composer.select_combo(trigger.type, now)

        # ── 话题注入 ──
        topic_data = None
        if trigger.type in ("lonely_low", "lonely_mid"):
            cfg_topic = self.config.get("topic_picker", {})
            force_threshold = cfg_topic.get("force_topic_threshold", 3)
            topic_prob = cfg_topic.get("topic_probability", 0.7)

            history = self.state.cooldown.trigger_history
            recent_lonely = sum(
                1 for t in history[-force_threshold:] if t.startswith("lonely_")
            )

            if recent_lonely >= force_threshold or random.random() < topic_prob:
                topic_data = self.topic_picker.pick(now)
                if topic_data:
                    trigger.data["topic"] = topic_data
        elif trigger.type not in ("follow_up", "reflect", "lonely_high", "longing"):
            # v9 审计 F-4:仅 netease 源跨触发(其他 7 源仍限孤独破冰)。
            # 活跃时段(非睡眠/非上课)才可能产出(peek 内部门控)。
            # 排除列表:follow_up/reflect 已有专用素材注入路径;lonely_high(崩溃态)
            # 与 longing(逃生阀破防)不夹带音乐话题;lonely_low/mid 由上方分支处理。
            topic_data = self.topic_picker.pick_netease_only(now)
            if topic_data:
                trigger.data["topic"] = topic_data

        # ── 使用 composer 组合情境文本 ──
        situation = self.composer.compose_situation(combo, topic_data, silent_h)

        # 课表上下文
        sch = self.state.schedule_status(now)
        schedule_hint = ""
        if sch and sch.get("holiday"):
            schedule_hint = f"今天是{sch['holiday']}假期，哥哥放假。"
        elif sch and sch.get("weekend"):
            schedule_hint = "今天是周末，哥哥休息。"
        elif sch and sch.get("makeup_day"):
            schedule_hint = f"{sch.get('makeup_reason', '调休日')}，虽然是周末但要上课。"
        elif sch and sch.get("in_class"):
            c = sch["current_course"]
            schedule_hint = f"哥哥正在上{c['course']}（到{c['time'][1]}）。不要在上课时发消息。"
        elif sch and sch.get("class_load") == "free":
            schedule_hint = "哥哥今天没课。"
        elif sch and sch.get("remaining_classes", 0) == 0:
            schedule_hint = "哥哥今天的课上完了。"

        # ── 构造指令 ──
        instruction = (
            f"请以迟菓（{personality_dir}/迟菓人格-精简版.md 设定）的身份，用上述语气发一条微信消息给哥哥。"
            "1-3句话。自然。允许适当的动作/神态描写。不打破第四面墙。"
            "每句话最多一个感叹号。一句话里波浪线和感叹号不同时出现。问号最多一个。"
        )
        if topic_data:
            instruction += (
                f"\n用以下话题自然破冰，不要让话题显得刻意："
                f"{topic_data['hint']}。"
                "让哥哥感受到你是真的关心他的生活，而不是因为孤独才找他。"
                "不要话题一转就直接表达情感需求——先好好聊话题。"
            )

        # ── v7: 接话茬素材注入(供 pi-agent 生成)──
        if trigger.type == "follow_up":
            instruction += (
                f"\n用「{trigger.data.get('topic', '')}」这个之前没聊完的话题自然接话茬,"
                "不要直接说『你上次说的那个……后来怎么样了』这种汇报句,"
                "像想起一样顺嘴问。"
            )

        return {
            "character": "迟菓",
            "personality_source": f"{personality_dir}/迟菓人格-精简版.md",
            "situation": situation,
            "schedule_hint": schedule_hint,
            "layer": emo.dominant_layer,
            "layer_guidance": guidance,
            "character_rules": (
                "①傲娇核心：对哥哥的任何消息先嘴硬。②不称菓菓。③不扮专家。"
                "④关心带刺。⑤不乖巧等待。⑥禁止自我怀疑。⑦被夸先嘴硬。"
                "喵字禁用。嘻嘻极少使用。"
                "[句数优先级] 1句推开，2句推开+接受，3句完整三段式。kernel层①暂停。"
            ),
            "personality_profile": pers.dominant_profile(),
            "combo": combo.get("combo_string", "a"),
            "emotion": {
                "loneliness": round(emo.loneliness, 0),
                "affection": round(emo.affection, 0),
                "anxiety": round(emo.anxiety, 0),
                "energy": round(emo.energy, 0),
                "tsundere_index": round(emo.tsundere_index, 0),
            },
            "silent_hours": round(silent_h, 1),
            "poisson_lambda": round(self.state.current_lambda(now), 4),
            "accumulated_lambda": round(self.state.cooldown.accumulated_lambda or self.state.current_lambda(now), 4),
            "follow_up": {
                "topic": trigger.data.get("topic", ""),
                "source": trigger.data.get("source", ""),
                "age_hours": trigger.data.get("age_hours", 0),
            },
            "trigger_type": trigger.type,
            "intensity": trigger.intensity,
            "instruction": instruction,
        }

    RECV_DEDUP_WINDOW_S = 450  # v9+R1+R2: 补报升级判定窗口（须覆盖 bridge 两次 daemon 调用间的 agent 分析往返：
                               #  recall 两趟 agent 路径最坏 420s：getAttention≤30s + askAgent 第一趟≤180s
                               #  + --schedule-recall daemon≤30s + runAgentRun 第二趟 agent≤180s；余量 30s）

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
        analysis_dict = None
        if analysis_json:
            try:
                analysis_dict = json.loads(analysis_json)
                if not isinstance(analysis_dict, dict):
                    raise ValueError("analysis is not a dict")
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                print(f"[warn] 分析JSON解析失败: {e}，降级为纯长度模式", file=sys.stderr)
                analysis_dict = None

        # ── v11 (#75): RMW 临界区全程持跨进程锁，防并发丢更新。
        # v1.14 (#139): 锁内先重载磁盘最新状态再处理——构造加载（S0）与拿到锁
        # 之间，cron evaluate（锁内 _load+推进+save）可能已把 S1 落盘；若基于
        # S0 陈旧快照 RMW+save，S0→S1 的情绪推进/cooldown 更新被整体回滚
        # （tick_seq CAS 只防序列回退，不保护 emotion/cooldown 字段）。
        # 与 record_send_result v12-R2 同一修复。旧注释「单次 CLI 进程不重载、
        # 避免覆盖调用方在构造后的内存修改」理由不成立：唯一生产调用点
        # （main 1855 行 engine 构造后立即调用）进锁前无任何进程内修改，
        # 重载幂等且安全。──
        with self.state.state_lock():
            try:
                self.state._load()
            except Exception:  # noqa: BLE001 - 重载失败维持现有内存状态
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
            is_upgrade = (
                analysis_dict is not None
                and bool(dedup)
                and not dedup.get("analysis")
            )
            if is_upgrade:
                # U5 (#233, D1): recv_id 精确匹配（同 id → 补报升级，免窗口判断）
                if recv_id and dedup.get("recv_id") == recv_id:
                    is_upgrade = True
                # 无 recv_id（CLI 手动/测试/老调用）→ 回退 text_sha + 窗口逻辑（450s 不变）
                elif dedup.get("text_sha") == text_sha and dedup.get("at"):
                    try:
                        prev_at = datetime.fromisoformat(dedup["at"])
                        is_upgrade = (now - prev_at).total_seconds() < self.RECV_DEDUP_WINDOW_S
                    except (ValueError, TypeError):
                        is_upgrade = False
                else:
                    is_upgrade = False

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
                except Exception:
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
        设 CHIGUO_MEM0_AUTOWRITE=0 可跳过自动写入（部署验证/测试用途，防止
        验证消息混入生产记忆库）。"""
        if os.environ.get("CHIGUO_MEM0_AUTOWRITE", "1") != "1":
            return  # 部署验证/测试可设 0 防污染生产记忆库
        if len(text.strip()) < 8:
            return  # 短消息（寒暄/无信息量）不写，也避免无谓的可用性探测
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
            mem.add_messages(messages, metadata=metadata)
        except Exception:
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
            with open(self.messages_log_path, "a") as f:
                try:
                    os.chmod(self.messages_log_path, 0o600)  # 消息归档含明文对话 → 0600
                except OSError:
                    pass
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
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
                with self.state.state_lock():
                    self.state._load()
                    self.state.record_trigger_sent(trigger)
                    self.state.save()
            except Exception:
                pass  # 统计失败不影响发送记录主链路
        self._log_message(
            msg_id=msg_id,
            direction="send",
            text=text,
            trigger=trigger,
            intensity=intensity,
            fallback=fallback,
        )

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
        except Exception:  # noqa: BLE001 - 记账失败静默，不阻断发送
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
        except Exception:  # noqa: BLE001 - 无状态文件/未记账 → 放行
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
            except Exception:  # noqa: BLE001
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
            # B5: 回环 bridge 调用绕系统代理（同 chiguo_envcheck._urlopen：本机有
            # http_proxy 时 localhost 直连不被劫持，防回环请求走代理失败降级）
            host = urllib.request.urlsplit(req.full_url).hostname or ""
            if host in ("localhost", "127.0.0.1", "::1"):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                resp = opener.open(req, timeout=t)
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
                try:
                    p = subprocess.run(
                        [node_bin, runner, "--prompt",
                         json.dumps(decision, ensure_ascii=False), "--send-mode"],
                        capture_output=True, text=True, timeout=timeout)
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
                    except Exception:  # noqa: BLE001 - 告警失败不阻断主消息
                        pass

        if not text:
            out["error"] = gen_err
            # U2 (#227): 无 composer 兜底——记录 health fail（达 threshold → state down + transition 告警）
            _send_transition_alert(self._record_health("fail", gen_err, loop_cfg))
            return out
        out["generated"] = True
        # 生成成功 → record success；transition（down→up 恢复）经 /send 发恢复（对齐 tick.sh）
        _send_transition_alert(self._record_health("success", "", loop_cfg))
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
            resp = _post("/send", {"to": to, "text": text}, 35.0)
            if not resp.get("ok"):
                raise RuntimeError(str(resp.get("error") or "bridge /send ok=false"))
            out["sent"] = True
            self.record_send_text(msg_id, text, trigger, intensity)
        except Exception as e:  # noqa: BLE001
            out["send_error"] = str(e)
            if msg_id:
                self.record_send_result(msg_id, "failed", str(e))
        return out

    def recent_sent_texts(self, n: int = 5) -> list[str]:
        """A9 查重数据源：最近 n 条已发送消息文本（chiguo_messages.jsonl 倒序取）。
        记录由 --record-send --text 写入（含 direction=send + text 字段），
        不新增文件。文件缺失/损坏行 → 静默跳过（查重降级为不启用）。"""
        try:
            with open(self.messages_log_path, "r") as f:
                lines = f.readlines()
        except OSError:
            return []
        texts: list[str] = []
        # 只扫最近 500 行（倒序前截断）：日志随运行时间线性增长，全量扫描无必要
        for line in reversed(lines[-500:]):
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
        with self.state.state_lock():
            # v12-R2: 锁内重载磁盘最新状态再执行退款——CLI --send-result 与
            # cron evaluate 并发时，若基于构造时（T0）陈旧快照 refund 后 save，
            # 会覆盖 evaluate 已落盘的情绪推进（tick_seq CAS 只防序列回退，
            # 不保护 emotion/cooldown 字段）。安全前提：本方法调用路径进锁前
            # 均无未保存的进程内修改（evaluate 各出口无条件 save；--loop 的
            # _loop_send 在 evaluate 锁释放后才执行）。
            try:
                self.state._load()
            except Exception:  # noqa: BLE001 - 重载失败维持现有内存状态
                pass
            already_reported = self._has_send_result(msg_id)
            if status == "failed" and not already_reported:
                # ── v6 修复: 仅当 msg_id 能在在途 Hawkes 事件中定位时退款 ──
                # 未知 msg_id → 只审计跳过，不执行退款副作用（防止凭空刷新逃生阀冷却/
                # 误删最后一条事件——旧实现恒走 pop()，乱序回传会删错事件）。
                # 兼容: 在途事件全部无 msg_id（旧 daemon 产生）→ 沿用旧语义退款。
                events = self.state.cooldown.event_timestamps
                matched = any(ev.get("msg_id") == msg_id for ev in events)
                legacy_events = bool(events) and all("msg_id" not in ev for ev in events)
                if matched or legacy_events:
                    self.state.refund_send(now, msg_id=msg_id)
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
                        f"not found in {len(events)} in-flight events",
                        file=sys.stderr,
                    )
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

    def snapshot(self):
        return self.state.snapshot(datetime.now(CST))


def _load_light_config(config_path: str | None = None) -> dict:
    """轻量分支共用:读 toml + 注入 _base_dir(config 所在目录)。不构造任何引擎对象。"""
    if config_path is None:
        config_path = str(Path(__file__).resolve().parent / "chiguo_proactive.toml")
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(Path(config_path).resolve().parent)
    return cfg


def _cmd_attention(config_path: str | None = None):
    """--attention 轻量读(§5.4):T1/T2/T3 组装 + 情感快照。零写副作用,毫秒级。"""
    import json as _json
    from schedule.sources import load_sources
    from schedule.attention import build_attention
    cfg = _load_light_config(config_path)
    try:
        src = load_sources(cfg["_base_dir"], cfg)
        att = build_attention(src, datetime.now(CST).date())
        emotion = {}
        try:
            st = _json.loads((Path(cfg["_base_dir"]) / "chiguo_state.json").read_text())
            emotion = st.get("emotion", {})
        except Exception:
            pass
        print(_json.dumps({"action": "attention", "ok": True, "attention": att,
                           "emotion": emotion, "week_num": att["week_num"],
                           "today_exceptions": att["today_exceptions"]}, ensure_ascii=False))
    except Exception as e:
        print(_json.dumps({"action": "attention", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --attention 失败: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_schedule_recall(query: str, config_path: str | None = None):
    """--schedule-recall <query>:recall 检索(A4 形状;失败 ok:false + exit 1,bridge 降级普通回复)。"""
    import json as _json
    from schedule.sources import load_sources
    from schedule.recall import recall
    cfg = _load_light_config(config_path)
    try:
        r = recall(query, load_sources(cfg["_base_dir"], cfg), datetime.now(CST).date())
    except Exception as e:
        print(_json.dumps({"action": "schedule_recall", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-recall 失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(_json.dumps({"action": "schedule_recall", "ok": True, "query": r["query"],
                       "matches": r["matches"]}, ensure_ascii=False))


def _cmd_schedule_change(json_arg: str, config_path: str | None = None):
    """--schedule-change <json>:写安排(二十轮 A4 形状;畸形 JSON → bad_json 不写入;ApiRejection → H5 文案)。"""
    import json as _json
    from schedule.api import ScheduleApi, ApiRejection
    from schedule.confirm import build_question
    cfg = _load_light_config(config_path)
    try:
        item = _json.loads(json_arg)
    except (_json.JSONDecodeError, TypeError):
        print(_json.dumps({"action": "schedule_change", "ok": False,
                           "reason": "bad_json", "question": "处理失败,再试一次?"}, ensure_ascii=False))
        print("[chiguo_daemon] --schedule-change 畸形 JSON,未写入", file=sys.stderr)
        sys.exit(1)
    try:
        api = ScheduleApi(cfg["_base_dir"], cfg)
        if isinstance(item, dict) and item.get("kind") == "remove":
            result = api.remove_override(item.get("match", {}))
        else:
            result = api.apply_override(item)
    except ApiRejection as e:
        question, missing = build_question(e.category)
        out = {"action": "schedule_change", "ok": False, "reason": e.category, "question": question}
        if missing:
            out["missing"] = missing
        print(_json.dumps(out, ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-change 拒绝({e.category}): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(_json.dumps({"action": "schedule_change", "ok": False,
                           "reason": "internal_error", "question": "处理失败,再试一次?"},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --schedule-change 异常: {e}", file=sys.stderr)
        sys.exit(1)
    print(_json.dumps({"action": "schedule_change", "ok": True, "text": result["text"]},
                      ensure_ascii=False))


def _cmd_memory_search(query: str, config_path: str | None = None):
    """--memory-search <query>: 回复侧记忆检索(mem0,软降级)。JSON→stdout,诊断→stderr,失败 exit 1。"""
    import json as _json
    from memory import create_backend
    cfg = _load_light_config(config_path)
    try:
        bridge = create_backend(cfg.get("memory", {}), base_dir=cfg["_base_dir"])
        rows = bridge.search_with_forgetting(query, limit=5)
    except Exception as e:
        print(_json.dumps({"action": "memory_search", "ok": False, "reason": str(e)[:200]},
                          ensure_ascii=False))
        print(f"[chiguo_daemon] --memory-search 失败: {e}", file=sys.stderr)
        sys.exit(1)
    # 行契约(id/text/category/scope/importance/timestamp/datetime…)已为 JSON 可序列化；
    # default=str 兜底 datetime 等非标准类型，防单条脏形状拖垮整个检索输出
    print(_json.dumps({"action": "memory_search", "ok": True, "query": query,
                       "count": len(rows), "memories": rows},
                      ensure_ascii=False, default=str))


# ── 入口 ──────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="迟菓主动消息 决策引擎")
    # L2 (#234): --version 帮助不写死具体版本链，只写规则（防过期）；实际次版本见 chiguo_version.py
    parser.add_argument("--version", action="version", version=f"chiguo v{VERSION} (规则: 每次迭代次版本 MINOR+1，见 chiguo_version.py)")
    parser.add_argument("--loop", type=int, nargs="?", const=300, metavar="SECONDS",
                        help="循环评估间隔秒数（最小60）")
    parser.add_argument("--user-msg", type=str, default=None,
                        help="记录哥哥消息")
    parser.add_argument("--analysis", type=str, default=None,
                        help="LLM情感分析JSON（配合 --user-msg 使用）")
    parser.add_argument("--recv-id", type=str, default=None,
                        help="bridge 每条主人消息本地生成的 uuid，用于 recv_dedup 精确去重（同 id 补报升级，不进 agent prompt；无则回退 text_sha+窗口逻辑）")
    # ── v6: 文件传参（避免 shell 转义问题，SKILL.md 已采用此路径）──
    parser.add_argument("--user-msg-file", type=str, default=None,
                        help="消息文本文件（配合 --analysis-file 使用）")
    parser.add_argument("--analysis-file", type=str, default=None,
                        help="LLM分析JSON文件（配合 --user-msg-file 使用）")
    parser.add_argument("--status", action="store_true",
                        help="显示状态")
    parser.add_argument("--compact", action="store_true",
                        help="紧凑输出（cron用，idle时不输出）")
    parser.add_argument("--anniversary", type=str, default=None,
                        help="纪念日管理: add anniversary <DATE> <NAME> / remove <ID> / list / update <ID> key=val...")
    parser.add_argument("--break", type=str, default=None, dest="break_cmd",
                        metavar="CMD",
                        help="寒暑假: on|off|status|add <起> <止> <备注>|remove <序号>|list|clear")
    parser.add_argument("--health", action="store_true",
                        help="健康检查：检测 daemon 最近是否正常运行")
    parser.add_argument("--attention", action="store_true",
                        help="注意力快照（T1/T2/T3 + 情感快照，轻量读，零写）")
    parser.add_argument("--schedule-recall", type=str, default=None,
                        metavar="QUERY",
                        help="安排回忆检索（日期或关键词）")
    parser.add_argument("--schedule-change", type=str, default=None,
                        metavar="JSON",
                        help="写安排（JSON: reminder/add/cancel/move/exam_week/remove）")
    parser.add_argument("--memory-search", type=str, default=None,
                        metavar="QUERY",
                        help="记忆检索（mem0 语义检索，回复侧记忆注入用；mem0 不可用软降级返回空）")
    parser.add_argument("--tune", action="store_true",
                        help="参数校准：基于回复延迟推荐 base_lambda 调整")
    parser.add_argument("--stats", type=int, nargs="?", const=7, metavar="DAYS",
                        help="统计摘要（默认7天，0=全部历史）")
    parser.add_argument("--alerts", action="store_true",
                        help="异常检测告警")
    parser.add_argument("--monitor", action="store_true",
                        help="完整监控报告（stats + alerts + health）")
    # ── C1: 确定性记忆巩固 ──
    parser.add_argument("--consolidate", action="store_true",
                        help="确定性记忆巩固（去重降权+过期；零 LLM；停机维护专用）")
    # ── v5: 对话日志 & 归档 ──
    parser.add_argument("--conversation", type=str, default=None,
                        metavar="DATE",
                        help="显示某天对话记录 (YYYY-MM-DD)")
    parser.add_argument("--conversation-days", type=int, default=None,
                        metavar="N",
                        help="显示最近N天对话记录")
    parser.add_argument("--export", type=str, nargs="?", const="json",
                        metavar="FORMAT",
                        help="导出对话历史 (默认json)")
    parser.add_argument("--record-send", type=str, default=None,
                        metavar="MSG_ID",
                        help="记录已发送消息文本 (配合 --text)")
    parser.add_argument("--fallback", action="store_true",
                        help="A8: 标记该消息为 composer 确定性兜底生成 (配合 --record-send)")
    parser.add_argument("--text", type=str, default=None,
                        help="消息文本 (配合 --record-send)")
    parser.add_argument("--trigger", type=str, default=None,
                        help="触发类型 (配合 --record-send)")
    parser.add_argument("--intensity", type=str, default=None,
                        help="消息强度 (配合 --record-send)")
    # ── v6: 反馈闭环 ──
    parser.add_argument("--send-result", type=str, default=None, metavar="MSG_ID",
                        help="回传发送结果 (配合 --send-status success|failed, 可选 --error)")
    parser.add_argument("--send-status", type=str, default=None,
                        choices=["success", "failed"],
                        help="发送状态 (配合 --send-result)")
    parser.add_argument("--error", type=str, default=None, help="失败原因 (配合 --send-result)")
    # ── v5: 告警持久化 ──
    parser.add_argument("--alerts-all", action="store_true",
                        help="显示所有告警（含已解决）")
    parser.add_argument("--ack", type=str, default=None,
                        metavar="ALERT_ID",
                        help="确认告警 (配合 --alerts)")
    # ── v5: 日志轮转 ──
    parser.add_argument("--rotate", action="store_true",
                        help="强制日志轮转")
    args = parser.parse_args()

    # ── 参数校验 ──
    if args.loop is not None and args.loop < 60:
        print("[chiguo_daemon] interval < 60, using 60", file=sys.stderr)
    # v8: --ack 是告警确认参数，自动联动开启 alerts 处理（不再静默忽略）
    if args.ack and not args.alerts:
        print("[chiguo_daemon] --ack 需要 --alerts，已自动联动开启", file=sys.stderr)
        args.alerts = True

    # ── 纪念日 CRUD（独立分支，不影响决策引擎；批 5 改调 ScheduleApi） ──
    if args.anniversary:
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
        if not args.send_status:
            print(json.dumps({"error": "--send-status required with --send-result"}, ensure_ascii=False))
            sys.exit(1)
        engine = DecisionEngine()
        r = engine.record_send_result(args.send_result, args.send_status, args.error)
        print(json.dumps(r, ensure_ascii=False))
        return

    # ── v5: 对话查询 & 导出（v10: 锚定 base_dir，从任意 cwd 运行都读写项目文件）──
    if args.conversation or args.conversation_days or args.export:
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
        sys.exit(DecisionEngine().cli_consolidate())

    # ── 监控系统（stats / alerts / monitor）（v10: 锚定 base_dir，从任意 cwd 运行都读写项目文件）──
    if args.stats is not None or args.alerts or args.monitor:
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
        max_interval = args.loop  # 用户设定的最大间隔（上限）

        # ── v5: PID 锁文件，防止双开（v6: 锚定 base_dir）──
        # v1.15: O_CREAT|O_EXCL 原子创建，消除 exists→write 的 TOCTOU（两进程
        # 同时过 exists 检查都会写锁，双开防不住）。持锁进程退出由 finally 清理。
        pid_path = engine._base_dir / "chiguo_loop.pid"
        try:
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # 锁已存在 → 检查持有者是否存活
            try:
                old_pid = int(pid_path.read_text().strip())
                try:
                    os.kill(old_pid, 0)  # 信号 0 只检查进程是否存在
                    print(f"❌ 已有实例运行 (PID {old_pid})，拒绝启动", file=sys.stderr)
                    sys.exit(1)
                except OSError:
                    pass  # 进程不存在 → 清理过期锁
            except (ValueError, OSError):
                pass
            # 过期锁 → 删除后原子重试创建
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

        def run():
            decision = engine.evaluate()
            if decision["action"] == "send":
                # v1.11 C: --loop 发送侧内聚（生成→发送→记账），不再只打印 JSON 等外部消费
                loop_cfg = engine.config.get("loop", {}) or {}
                try:
                    # U2 (#227): down 暂停 / 降频探测门控——不满足则跳过尝试（suppressed）
                    if engine._health_should_probe(loop_cfg):
                        decision["_loop"] = engine._loop_send(decision, loop_cfg)
                    else:
                        decision["_loop"] = {"generated": False, "sent": False,
                                             "suppressed": True}
                except Exception as e:  # noqa: BLE001 - 兜底：打印决策不中断循环
                    decision["_loop_error"] = str(e)
                print(json.dumps(decision, ensure_ascii=False))
            elif not args.compact:
                print(json.dumps(decision, ensure_ascii=False))
            else:
                print(json.dumps({"action": "idle", "version": VERSION, "time": datetime.now(CST).isoformat()}, ensure_ascii=False))
            sys.stdout.flush()
            return decision

        decision = run()
        try:
            while True:
                # ── v4: 动态休眠 ──
                now = datetime.now(CST)
                dynamic_sec = engine._dynamic_sleep_interval(now, decision)
                sleep_sec = min(dynamic_sec, max_interval)
                sleep_sec = max(60, sleep_sec)  # 下限 1 分钟
                time.sleep(sleep_sec)
                decision = run()
        except KeyboardInterrupt:
            print("\n💤 已停止", file=sys.stderr)
            if not engine.state.save():
                print("[chiguo_daemon] state_save_failed: 状态写盘失败", file=sys.stderr)
        finally:
            # ── v5: 清理 PID 锁（v6: 复用创建时的锚定路径，勿按 cwd 重建）──
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
        return

    # 默认：单次评估
    decision = engine.evaluate()
    if args.compact and decision["action"] == "idle":
        # 紧凑模式 idle 时输出最小 heartbeat（用于 cron 健康检查）
        print(json.dumps({"action": "idle", "version": VERSION, "time": datetime.now(CST).isoformat()}, ensure_ascii=False))
        return
    print(json.dumps(decision, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
