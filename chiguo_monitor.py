#!/usr/bin/env python3
# ============================================================
# chiguo_monitor.py — 迟菓主动消息 结构化监控
#
# 零新依赖，纯 stdlib。流式解析 decisions.jsonl。
# 独立可用：python3 chiguo_monitor.py [--days 7] [--alerts]
# daemon 集成：python3 chiguo_daemon.py --stats|--alerts|--monitor
# ============================================================

import json
import os
import shutil
import statistics
import sys
import tomllib
from collections import Counter
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

try:
    import mem0 as _mem0
    _HAS_MEM0 = True
except ImportError:
    _mem0 = None
    _HAS_MEM0 = False

CST = timezone(timedelta(hours=8))


class ChiguoMonitor:
    """只读监控：解析决策日志 + 状态文件 → 统计 + 异常检测。
    一次遍历完成所有聚合。流式逐行解析（行级 O(1) 内存），但聚合序列
    （emotion_series/reply_events/daily_counts）随窗口内条目数线性增长。
    文件缺失 → 返回空统计，不抛异常。
    """

    def __init__(self,
                 log_path: str = "chiguo_decisions.jsonl",
                 state_path: str = "chiguo_state.json",
                 break_state_path: str = "break_state.json",
                 config_path: str = "chiguo_proactive.toml",
                 messages_log_path: str = "chiguo_messages.jsonl"):
        self.log_path = Path(log_path)
        self.state_path = Path(state_path)
        self.break_state_path = Path(break_state_path)
        self.messages_log_path = Path(messages_log_path)
        self.config_path = Path(config_path)
        self._monitor_config = self._load_monitor_config(self.config_path)

    def _load_monitor_config(self, config_path: Path) -> dict:
        """读取 [monitor] 段配置，缺省硬编码阈值。
        相对路径在当前 cwd 找不到时回退到模块目录（与 health() 的 config 检测一致），
        避免从其他 cwd 运行时阈值静默回落默认值。"""
        defaults = {
            "disk_warn_mb": 500,
            "disk_critical_mb": 100,
            "memory_warn_mb": 500,
            "memory_critical_mb": 1000,
            "mem0_qdrant_path": "data/mem0/qdrant",
            "mem0_history_db": "data/mem0/history.db",
            "backend": "mem0",         # v1.9: 记忆后端抽象（mem0/自定义类路径）
        }
        candidates = [config_path]
        if not config_path.is_absolute():
            candidates.append(Path(__file__).resolve().parent / config_path)
        for cand in candidates:
            try:
                with open(cand, "rb") as f:
                    cfg = tomllib.load(f)
                monitor = cfg.get("monitor", {})
                defaults.update(monitor)
                # [monitor] 未定义 mem0_qdrant_path 时回退 [memory] 段（与 toml 注释约定一致）
                if "mem0_qdrant_path" not in monitor:
                    defaults["mem0_qdrant_path"] = (cfg.get("memory", {}).get("mem0_qdrant_path")
                                                    or defaults["mem0_qdrant_path"])
                if "mem0_history_db" not in monitor:
                    defaults["mem0_history_db"] = (cfg.get("memory", {}).get("mem0_history_db")
                                                   or defaults["mem0_history_db"])
                # v1.9: 记忆后端单一事实来源 = [memory] 段
                defaults["backend"] = cfg.get("memory", {}).get("backend") or defaults["backend"]
                break
            except Exception:
                continue
        return defaults

    def _mem0_qdrant_dir(self) -> Path:
        """mem0 本地向量库目录。优先级：[monitor] > [memory] > 默认 data/mem0/qdrant。
        相对路径锚定项目根；~ 展开为 $HOME。"""
        raw = self._monitor_config.get("mem0_qdrant_path", "data/mem0/qdrant")
        p = Path(os.path.expanduser(raw))
        if p.is_absolute():
            return p
        return Path(__file__).resolve().parent / p

    def _memory_backend(self) -> str:
        """v1.9: 记忆后端类型（[memory].backend，缺省 mem0）。"""
        return self._monitor_config.get("backend", "mem0")

    # ═══════════════════════════════════════════════════════════
    # 内部：流式解析
    # ═══════════════════════════════════════════════════════════

    def _now(self) -> datetime:
        return datetime.now(CST)

    def _iter_decisions(self, since: datetime | None = None):
        """流式迭代 decisions.jsonl，一次一行。损坏行静默跳过。"""
        if not self.log_path.exists():
            return
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except UnicodeDecodeError, json.JSONDecodeError:
                        continue
                    if not isinstance(d, dict):
                        continue  # 合法 JSON 但非 dict（形状漂移）→ 跳过，防 AttributeError
                    if since:
                        ts = self._extract_time(d)
                        if ts and ts < since:
                            continue
                    yield d
        except OSError:
            return  # 权限/删除竞态 → 静默跳过

    @staticmethod
    def _extract_time(entry: dict) -> datetime | None:
        """从决策条目提取时间戳。"""
        # 优先 state.time
        state = entry.get("state")
        if isinstance(state, dict):
            raw = state.get("time")
            if raw:
                ts = ChiguoMonitor._parse_time_str(raw)
                if ts is not None:
                    return ts
        # 回退：顶层 time 字段
        raw = entry.get("time")
        if raw and isinstance(raw, str):
            return ChiguoMonitor._parse_time_str(raw)
        return None

    @staticmethod
    def _parse_time_str(raw: str) -> datetime | None:
        """解析单条时间字符串。

        优先 "%Y-%m-%d %H:%M"（naive → 视为 CST）；解析失败回退
        datetime.fromisoformat（daemon compact 输出
        datetime.now(CST).isoformat()，含 T/微秒/+08:00 —— 混入时不再
        被静默丢弃）。ISO 结果口径与 _parse_msg_ts 一致：naive → 视为 CST，
        aware → 统一换算到 CST。全部失败返回 None。"""
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        except ValueError, TypeError:
            pass
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)  # naive → 视为 CST
            return dt.astimezone(CST)
        except ValueError, TypeError:
            pass
        return None

    @staticmethod
    def _normalize_entry(entry: dict) -> None:
        """防御 None/invalid 嵌套字段：state/context 非 dict → 空 dict，
        state.emotion/cooldown 非 dict → 空 dict。避免 .get() 链崩溃。
        stats() 与 alerts() 共用，保证两者对脏数据处理口径一致。"""
        if not isinstance(entry.get("state"), dict):
            entry["state"] = {}
        if not isinstance(entry.get("context"), dict):
            entry["context"] = {}
        if not isinstance(entry["state"].get("emotion"), dict):
            entry["state"]["emotion"] = {}
        if not isinstance(entry["state"].get("cooldown"), dict):
            entry["state"]["cooldown"] = {}

    def _read_state(self) -> dict:
        """读取运行时状态，缺失/损坏返回 {}"""
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError, OSError:
            return {}
        if not isinstance(data, dict):
            return {}  # 合法 JSON 但形状错误（[]/123/"x"）→ 回退空，避免下游 .get() 崩溃
        return data

    def _read_break_state(self) -> dict:
        """读取假期状态"""
        if not self.break_state_path.exists():
            return {}
        try:
            data = json.loads(self.break_state_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError, OSError:
            return {}
        if not isinstance(data, dict):
            return {}  # 合法 JSON 但形状错误（[]/123/"x"）→ 回退空，避免下游 .get() 崩溃
        return data

    # ═══════════════════════════════════════════════════════════
    # 统计引擎
    # ═══════════════════════════════════════════════════════════

    def stats(self, days: int = 7) -> dict:
        """核心统计。一次遍历完成所有聚合。

        Args:
            days: 统计最近 N 天，0 = 全部

        Returns:
            结构化统计字典
        """
        since = None
        if days > 0:
            since = self._now() - timedelta(days=days)

        # ── D1: 主动消息效果评估（对标 ProactiveEval；[monitor].proactive_eval 默认 False 恒等）──
        proactive_eval = bool(self._monitor_config.get("proactive_eval", False))
        try:
            replied_within_h = float(self._monitor_config.get("replied_within_hours", 24.0))
        except (TypeError, ValueError):
            replied_within_h = 24.0
        send_events: list[tuple] = []  # [(t, trigger)] 按时间序（决策日志顺序）
        recv_times: list = []  # 按时间序的 user-msg 到达时刻

        # ── 累加器 ──
        total_sends = 0
        total_idles = 0
        trigger_counts: Counter = Counter()
        intensity_counts: Counter = Counter()
        hour_counts: Counter = Counter()
        weekday_counts: Counter = Counter()
        daily: dict[str, dict] = {}  # date_str → {sends, idles, idle_reasons}
        idle_reasons: Counter = Counter()

        # 情绪序列（按时间顺序，用于趋势分析）
        emotion_series: dict[str, list[float]] = {
            "loneliness": [], "affection": [], "anxiety": [],
            "energy": [], "tsundere_index": [],
        }

        # 回复追踪
        reply_events: list[dict] = []  # [{time, messages_without_reply}]
        max_unreplied = 0
        layer_counts: Counter = Counter()

        # 时间跨度
        first_time: datetime | None = None
        last_time: datetime | None = None

        # 无法解析时间的条目数（daemon compact ISO 时间混入等，供统计可观测性）
        unparsed_time_count = 0

        # mem0 降级检测
        mem0_ok_count = 0
        mem0_check_count = 0

        # v6: 发送结果统计
        send_success = 0
        send_failed = 0

        for entry in self._iter_decisions(since):
            self._normalize_entry(entry)

            t = self._extract_time(entry)
            if t is None:
                unparsed_time_count += 1
                continue

            # 时间窗口
            if first_time is None or t < first_time:
                first_time = t
            if last_time is None or t > last_time:
                last_time = t

            action = entry.get("action", "?")
            state = entry.get("state", {})

            # 日聚合
            date_key = t.strftime("%Y-%m-%d")
            if date_key not in daily:
                daily[date_key] = {"sends": 0, "idles": 0, "idle_reasons": Counter()}

            if action == "send":
                total_sends += 1
                daily[date_key]["sends"] += 1

                trigger = entry.get("trigger", "?")
                trigger_counts[trigger] += 1
                # D1: 收集发送事件（时间 + trigger）供 proactive_stats 分组统计
                send_events.append((t, trigger))

                intensity = entry.get("intensity", "?")
                intensity_counts[intensity] += 1

                hour_counts[t.hour] += 1
                weekday_counts[t.weekday()] += 1

                # 人格层
                layer = state.get("dominant_layer", "?")
                layer_counts[layer] += 1

                # 回复追踪：messages_without_reply
                cooldown = state.get("cooldown", {})
                mwr = cooldown.get("messages_without_reply", 0)
                if isinstance(mwr, (int, float)):
                    max_unreplied = max(max_unreplied, int(mwr))

                # 从 reply_latencies 收集已知延迟（state 快照不直接包含，但从 cooldown 推断）
                silent_h = cooldown.get("silent_hours", 0)
                reply_events.append({
                    "time": t.isoformat(),
                    "messages_without_reply": mwr if isinstance(mwr, (int, float)) else None,
                    "silent_hours": silent_h if isinstance(silent_h, (int, float)) else 0,
                })

                # mem0 可用性：有 memory 触发 → mem0 正常
                if trigger == "memory":
                    # 检查是否有 mem0_memory（situation 含「突然想起」或 context 含 mem0）
                    ctx = entry.get("context")
                    if not isinstance(ctx, dict):
                        ctx = {}
                    sit = ctx.get("situation") or ""
                    if "突然想起" in sit or "mem0" in str(ctx).lower():
                        mem0_ok_count += 1
                    mem0_check_count += 1

            elif action == "idle":
                total_idles += 1
                daily[date_key]["idles"] += 1
                reason = entry.get("reason", "?")
                idle_reasons[reason] += 1
                daily[date_key]["idle_reasons"][reason] += 1

            elif action == "send_result":
                if entry.get("status") == "success":
                    send_success += 1
                # R2: --user-msg 幻影发送退款（phantom_send_reply_path）不计入 send_failed——
                # 该记录是回复链内部回滚未送达的决策 booking，非真实发送失败。
                elif entry.get("status") == "failed" and entry.get("error") != "phantom_send_reply_path":
                    send_failed += 1

            elif action == "recv":
                # D1: 用户消息到达时刻（主动消息效果评估的"已回复"数据源）
                recv_times.append(t)

            # 情绪时间序列
            emo = state.get("emotion", {})
            for key in emotion_series:
                val = emo.get(key)
                if isinstance(val, (int, float)):
                    emotion_series[key].append(float(val))

        # ── 计算派生指标 ──

        # 时间跨度
        span_days = 0.0
        if first_time and last_time:
            # 下限 1 天：不足 1 天的窗口按 1 天计，避免 0.01 放大日均发送量失真
            span_days = max(1.0, (last_time - first_time).total_seconds() / 86400)
        sends_per_day = total_sends / span_days if span_days > 0 else 0.0

        # 情绪趋势
        emotion_trends = {}
        emotion_stats = {}
        for key in emotion_series:
            vals = emotion_series[key]
            if len(vals) >= 4:
                emotion_trends[key] = self._trend(vals)
                emotion_stats[key] = {
                    "min": round(min(vals), 1),
                    "max": round(max(vals), 1),
                    "avg": round(statistics.mean(vals), 1),
                    "current": round(vals[-1], 1),
                    "count": len(vals),
                }
            elif len(vals) > 0:
                emotion_trends[key] = "stable"
                emotion_stats[key] = {
                    "min": round(min(vals), 1),
                    "max": round(max(vals), 1),
                    "avg": round(statistics.mean(vals), 1),
                    "current": round(vals[-1], 1),
                    "count": len(vals),
                }
            else:
                emotion_trends[key] = "no_data"
                emotion_stats[key] = None

        # 回复率估算（用 silent_hours 变化推断：silent_hours 骤降 = 有回复）
        # 口径定义（与 alerts() B5 一致）：
        #   分子 = 相邻 send 中 messages_without_reply 下降的对数（检测到回复）
        #   分母 = 窗口内等待过回复的 send 数（mwr > 0，mwr=0 的 send 无回复可收，不参与）
        reply_rate = None
        avg_latency = None
        median_latency = None
        if reply_events and len(reply_events) >= 2:
            replied = 0
            latencies = []
            for i in range(1, len(reply_events)):
                prev = reply_events[i - 1]
                curr = reply_events[i]
                # messages_without_reply 下降 → 哥哥回复了
                # 口径（与 alerts() B5 一致）：双方均为数值才比较；
                # None/非数值视为未知，不计为回复变化（防止 prev=3, curr=None→0 误算成回复）
                if (isinstance(curr["messages_without_reply"], (int, float))
                        and isinstance(prev["messages_without_reply"], (int, float))
                        and curr["messages_without_reply"] < prev["messages_without_reply"]):
                    replied += 1
                    latencies.append(curr.get("silent_hours", 0))
            total_send_events = sum(1 for e in reply_events
                                    if isinstance(e["messages_without_reply"], (int, float))
                                    and e["messages_without_reply"] > 0)
            if total_send_events > 0:
                reply_rate = round(replied / total_send_events, 3)
            if latencies:
                avg_latency = statistics.mean(latencies)
                median_latency = statistics.median(latencies)

        # 当前状态
        state_data = self._read_state()
        current_emotion = None
        last_tick = None
        if state_data:
            current_emotion = state_data.get("emotion")
            last_tick = state_data.get("last_tick")

        # 日计数序列（按日期排序）
        daily_list = [
            {"date": d, "sends": v["sends"], "idles": v["idles"],
             "top_idle_reason": v["idle_reasons"].most_common(1)[0][0] if v["idle_reasons"] else None}
            for d, v in sorted(daily.items())
        ]

        mem0_likely = mem0_ok_count > 0

        # ── D1: 主动消息效果评估（按 trigger 分组：发送后 replied_within_h 内
        # 收到首条 user-msg 视为已回复；双指针流式，一次遍历 O(n)）──
        # 语义：一条 user-msg 至多算作一条主动消息的回复——命中窗口即消费
        # （recv_ptr 前进），后续 send 不再重复计同一条回复（防串计）。
        proactive_stats = None
        if proactive_eval:
            per_trigger: dict[str, dict] = {}
            recv_ptr = 0
            for s_time, trig in send_events:  # 决策日志时间序
                bucket = per_trigger.setdefault(trig, {"sent": 0, "replied": 0})
                bucket["sent"] += 1
                while recv_ptr < len(recv_times) and recv_times[recv_ptr] <= s_time:
                    recv_ptr += 1  # 跳过发送时刻之前的 user-msg（非本消息的回复）
                if recv_ptr < len(recv_times):
                    delta_h = (recv_times[recv_ptr] - s_time).total_seconds() / 3600.0
                    if delta_h <= replied_within_h:
                        bucket["replied"] += 1
                        recv_ptr += 1  # 消费该回复：不重复计给更晚的 send
            for trig, b in per_trigger.items():
                b["reply_rate"] = round(b["replied"] / b["sent"], 3) if b["sent"] else 0.0
            overall_sent = sum(b["sent"] for b in per_trigger.values())
            overall_replied = sum(b["replied"] for b in per_trigger.values())
            proactive_stats = dict(per_trigger)
            proactive_stats["overall"] = {
                "sent": overall_sent,
                "replied": overall_replied,
                "reply_rate": (round(overall_replied / overall_sent, 3)
                               if overall_sent else 0.0),
            }

        out = {
            "period": {
                "days": days if days > 0 else "all",
                "from": first_time.isoformat() if first_time else None,
                "to": last_time.isoformat() if last_time else None,
                "span_days": round(span_days, 1),
                "total_entries": total_sends + total_idles,
                "unparsed_time_count": unparsed_time_count,
            },
            "activity": {
                "total_sends": total_sends,
                "total_idles": total_idles,
                "sends_per_day": round(sends_per_day, 2),
                "by_trigger": dict(trigger_counts.most_common()),
                "by_intensity": dict(intensity_counts),
                "by_hour": {str(h): hour_counts[h] for h in sorted(hour_counts)},
                "by_weekday": {
                    self._weekday_name(d): weekday_counts.get(d, 0)
                    for d in range(7)
                },
                "by_layer": dict(layer_counts),
                "idle_reasons": dict(idle_reasons.most_common()),
                "daily_counts": daily_list,
            },
            "replies": {
                "max_unreplied_streak": max_unreplied,
                "reply_rate": reply_rate,
                "avg_reply_latency_h": round(avg_latency, 1) if avg_latency else None,
                "median_reply_latency_h": round(median_latency, 1) if median_latency else None,
                "total_send_events_tracked": len(reply_events),
            },
            "emotions": {
                "current": current_emotion,
                "trends": emotion_trends,
                "stats": emotion_stats,
            },
            "mem0": {
                "memory_trigger_count": mem0_check_count,
                "mem0_ok_estimate": mem0_ok_count,
                "likely_available": mem0_likely,
            } if mem0_check_count > 0 else None,
            "send_result": {
                "success": send_success,
                "failed": send_failed,
            },
        }
        # D1: 默认关闭恒等——proactive_eval=False 时不新增输出键
        if proactive_stats is not None:
            out["proactive_stats"] = proactive_stats
        return out

    @staticmethod
    def _weekday_name(d: int) -> str:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]

    @staticmethod
    def _trend(values: list[float], threshold_ratio: float = 0.03) -> str:
        """首半 vs 后半均值比较 → rising/stable/falling"""
        n = len(values)
        if n < 4:
            return "stable"
        half = n // 2
        first_avg = statistics.mean(values[:half])
        second_avg = statistics.mean(values[half:])
        if first_avg == 0:
            return "stable"
        change = (second_avg - first_avg) / max(abs(first_avg), 1)
        if change > threshold_ratio:
            return "rising"
        elif change < -threshold_ratio:
            return "falling"
        return "stable"

    # ═══════════════════════════════════════════════════════════
    # 异常检测
    # ═══════════════════════════════════════════════════════════

    def alerts(self) -> list[dict]:
        """扫描日志 + 状态，返回所有异常告警。按严重度排序。"""
        results: list[dict] = []
        now = self._now()

        # ── A. 崩溃间隙（daemon 停跑 > 6h） ──
        state = self._read_state()
        last_tick_str = state.get("last_tick")
        if last_tick_str:
            try:
                last_tick = datetime.fromisoformat(last_tick_str)
                if last_tick.tzinfo is None:
                    last_tick = last_tick.replace(tzinfo=CST)  # naive → 视为 CST
                gap_h = (now - last_tick).total_seconds() / 3600
                if gap_h > 6:
                    results.append({
                        "severity": "critical",
                        "type": "crash_gap",
                        "message": f"daemon 上次 tick 在 {gap_h:.1f} 小时前（>6h），可能已停止运行",
                        "last_tick": last_tick_str,
                        "gap_hours": round(gap_h, 1),
                    })
                elif gap_h > 3:
                    results.append({
                        "severity": "info",
                        "type": "slow_tick",
                        "message": f"daemon 上次 tick 在 {gap_h:.1f} 小时前（>3h），可能 cron 频率过低",
                        "last_tick": last_tick_str,
                        "gap_hours": round(gap_h, 1),
                    })
            except ValueError, TypeError:
                pass
        else:
            results.append({
                "severity": "critical",
                "type": "no_state",
                "message": "chiguo_state.json 不存在或无 last_tick，daemon 可能从未运行",
            })

        # ── B. 日志扫描异常 ──
        # 只看最近 14 天
        since = now - timedelta(days=14)
        recent_entries = list(self._iter_decisions(since))
        for e in recent_entries:
            self._normalize_entry(e)  # 与 stats() 相同的脏数据归一化（state:null 等）


        sends = [e for e in recent_entries if e.get("action") == "send"]
        idles = [e for e in recent_entries if e.get("action") == "idle"]

        # B1. 连续无回复
        max_mwr = 0
        mwr_times: list[tuple[int, str]] = []
        for e in sends:
            mwr = e.get("state", {}).get("cooldown", {}).get("messages_without_reply", 0)
            if isinstance(mwr, (int, float)) and mwr > max_mwr:
                max_mwr = int(mwr)
            if isinstance(mwr, (int, float)):
                ts = self._extract_time(e)
                mwr_times.append((int(mwr), ts.isoformat() if ts else "?"))

        if max_mwr >= 5:
            # 找到最近一次达到5的条目
            last_high = None
            for m, t in reversed(mwr_times):
                if m >= 5:
                    last_high = t
                    break
            results.append({
                "severity": "warn",
                "type": "consecutive_no_reply",
                "message": f"哥哥连续 {max_mwr} 条消息未回复",
                "max_unreplied": max_mwr,
                "last_at": last_high,
            })

        # B2. 情绪极端卡住
        if sends:
            recent_sends_24h = []
            for e in sends:
                t = self._extract_time(e)
                if t and (now - t).total_seconds() < 86400:
                    recent_sends_24h.append(e)

            if recent_sends_24h:
                high_lo = sum(1 for e in recent_sends_24h
                              if e.get("state", {}).get("emotion", {}).get("loneliness", 0) > 90)
                high_anx = sum(1 for e in recent_sends_24h
                               if e.get("state", {}).get("emotion", {}).get("anxiety", 0) > 90)
                total_24h = len(recent_sends_24h)
                if total_24h >= 3 and high_lo >= total_24h * 0.8:
                    results.append({
                        "severity": "warn",
                        "type": "emotion_stuck_high",
                        "message": f"过去24h内 {high_lo}/{total_24h} 次发送时孤独值 > 90，情绪持续极端",
                        "emotion": "loneliness",
                    })
                if total_24h >= 3 and high_anx >= total_24h * 0.8:
                    results.append({
                        "severity": "warn",
                        "type": "emotion_stuck_high",
                        "message": f"过去24h内 {high_anx}/{total_24h} 次发送时不安值 > 90，情绪持续极端",
                        "emotion": "anxiety",
                    })

            # B3. 低元气卡住（复用 B2 的 recent_sends_24h，避免重复 _extract_time 调用）
            if recent_sends_24h:
                low_energy = sum(1 for e in recent_sends_24h
                                 if e.get("state", {}).get("emotion", {}).get("energy", 0) < 15)
                if len(recent_sends_24h) >= 6 and low_energy >= len(recent_sends_24h) * 0.7:
                    results.append({
                        "severity": "info",
                        "type": "emotion_stuck_low",
                        "message": f"过去24h内 {low_energy}/{len(recent_sends_24h)} 次发送时元气 < 15，可能过度发送",
                        "emotion": "energy",
                    })

        # B4. 频繁崩溃触发（kernel 层占比 > 40%）
        if len(sends) >= 10:
            kernel_count = sum(1 for e in sends
                               if e.get("state", {}).get("dominant_layer") == "kernel")
            kernel_ratio = kernel_count / len(sends)
            if kernel_ratio > 0.4:
                results.append({
                    "severity": "warn",
                    "type": "frequent_crash",
                    "message": f"过去14天 {kernel_count}/{len(sends)}（{kernel_ratio:.0%}）次发送处于 kernel 崩溃层",
                    "kernel_ratio": round(kernel_ratio, 2),
                })

        # B5. 低回复率
        # 口径与 stats() 一致（见 stats() 回复率估算注释）：
        #   分子 = 相邻 send 中 messages_without_reply 下降的对数（检测到回复）
        #   分母 = 等待过回复的 send 数（mwr > 0），而非全部 send 数
        if len(sends) >= 5:
            replied = 0
            for i in range(1, len(sends)):
                prev_mwr = sends[i - 1].get("state", {}).get("cooldown", {}).get("messages_without_reply", 0)
                curr_mwr = sends[i].get("state", {}).get("cooldown", {}).get("messages_without_reply", 0)
                if isinstance(curr_mwr, (int, float)) and isinstance(prev_mwr, (int, float)):
                    if curr_mwr < prev_mwr:
                        replied += 1
            tracked = sum(1 for e in sends
                          if isinstance(e["state"]["cooldown"].get("messages_without_reply"), (int, float))
                          and e["state"]["cooldown"].get("messages_without_reply", 0) > 0)
            if tracked > 0:
                rate = replied / tracked
                if rate < 0.3:
                    results.append({
                        "severity": "warn",
                        "type": "low_reply_rate",
                        "message": f"过去14天回复率仅 {rate:.0%}（{replied}/{tracked}），主人可能逐渐疏远",
                        "reply_rate": round(rate, 2),
                    })

        # B6. 情绪快速攀升（24h 内 loneliness 涨 > 40）
        emotion_vals_24h = []
        for e in recent_entries:
            ts = self._extract_time(e)
            if ts and (now - ts).total_seconds() < 86400:
                lo = e.get("state", {}).get("emotion", {}).get("loneliness", 0)
                if isinstance(lo, (int, float)):
                    emotion_vals_24h.append((ts, float(lo)))
        if len(emotion_vals_24h) >= 4:
            emotion_vals_24h.sort(key=lambda x: x[0])
            first_lo = emotion_vals_24h[0][1]
            last_lo = emotion_vals_24h[-1][1]
            delta = last_lo - first_lo
            if delta > 40:
                results.append({
                    "severity": "info",
                    "type": "rapid_escalation",
                    "message": f"过去24h孤独值暴涨 {delta:.0f}（{first_lo:.0f} → {last_lo:.0f}），主人可能异常沉默",
                    "delta": round(delta, 1),
                })

        # ── C. mem0 降级检测 ──
        memory_triggers = sum(1 for e in sends if e.get("trigger") == "memory")
        if len(sends) >= 10 and memory_triggers == 0:
            results.append({
                "severity": "info",
                "type": "mem0_possible_degradation",
                "message": f"过去14天 {len(sends)} 次发送中无 memory 触发，mem0 可能不可用",
            })

        # ── D. 假期状态异常 ──
        break_data = self._read_break_state()
        if break_data.get("manual_override"):
            since_str = break_data.get("since", "?")
            results.append({
                "severity": "info",
                "type": "manual_break_active",
                "message": f"假期模式手动无限期开启中（since {since_str}）。如已开学请执行 --break off",
            })

        # 按严重度排序：critical > warn > info
        severity_order = {"critical": 0, "warn": 1, "info": 2}
        results.sort(key=lambda a: severity_order.get(a["severity"], 3))
        return results

    # ═══════════════════════════════════════════════════════════
    # 健康检查（增强版 --health）
    # ═══════════════════════════════════════════════════════════

    def health(self) -> dict:
        """增强版健康检查：检测 daemon + mem0 记忆 + 数据新鲜度"""
        now = self._now()
        state = self._read_state()
        issues = []
        healthy = True

        # 1. daemon 活跃
        last_tick = state.get("last_tick")
        hours_ago = None
        if last_tick:
            try:
                lt = datetime.fromisoformat(last_tick)
                if lt.tzinfo is None:
                    lt = lt.replace(tzinfo=CST)  # naive → 视为 CST
                hours_ago = (now - lt).total_seconds() / 3600
                if hours_ago > 6:
                    healthy = False
                    issues.append(f"daemon last tick {hours_ago:.1f}h ago (>6h)")
            except ValueError, TypeError:
                healthy = False
                issues.append("last_tick parse error")
        else:
            healthy = False
            issues.append("no last_tick in state file (daemon never run?)")

        # 2. 日志存在
        if not self.log_path.exists():
            healthy = False
            issues.append("decisions log file missing")

        # 3. 日志最近有写入
        log_recent = False
        if self.log_path.exists():
            try:
                log_mtime = datetime.fromtimestamp(self.log_path.stat().st_mtime, tz=CST)
                log_age_h = (now - log_mtime).total_seconds() / 3600
                if log_age_h > 12:
                    issues.append(f"decisions log last modified {log_age_h:.1f}h ago")
                else:
                    log_recent = True
            except OSError:
                issues.append("cannot stat decisions log")

        # 4. 网易云音乐桥健康(只读 netease_health.json;缺失/损坏 → 跳过不告警)
        nh_candidates = [self.config_path.parent / "netease" / "netease_health.json"]
        if not self.config_path.is_absolute():
            nh_candidates.append(
                Path(__file__).resolve().parent / "netease" / "netease_health.json")
        for nh_path in nh_candidates:
            try:
                with open(nh_path, encoding="utf-8") as f:
                    nh = json.load(f)
                if isinstance(nh, dict) and nh.get("faulty"):
                    reason = nh.get("failure_reason") or "?"
                    healthy = False
                    issues.append(
                        f"netease music API faulty (reason={reason}, "
                        f"api_alive={nh.get('api_alive')}, logged_in={nh.get('logged_in')})")
                break  # 找到文件即停(即使非 faulty 也 break)
            except OSError, json.JSONDecodeError:
                continue  # 该候选不存在/损坏 → 试下一个

        # 5. 状态文件版本
        version = state.get("_version", "?")

        # 6. 配置文件存在（锚定到构造时传入的路径，不依赖 cwd；相对路径回退到模块所在目录）
        config_exists = self.config_path.exists()
        if not config_exists and not self.config_path.is_absolute():
            config_exists = (Path(__file__).resolve().parent / self.config_path).exists()
        if not config_exists:
            healthy = False
            issues.append("config file missing")

        # 7. 磁盘空间
        disk_info = {"free_mb": None, "total_mb": None, "used_mb": None}
        try:
            usage = shutil.disk_usage(Path(__file__).resolve().parent)  # 锚定项目目录,防 cwd 漂移假阴性(R23)
            free_mb = usage.free / (1024 * 1024)
            total_mb = usage.total / (1024 * 1024)
            used_mb = usage.used / (1024 * 1024)
            disk_info = {"free_mb": round(free_mb, 1), "total_mb": round(total_mb, 1),
                         "used_mb": round(used_mb, 1)}
            critical = self._monitor_config.get("disk_critical_mb", 100)
            warn = self._monitor_config.get("disk_warn_mb", 500)
            if free_mb < critical:
                healthy = False
                issues.append(f"disk free {free_mb:.0f}MB < {critical}MB (critical)")
            elif free_mb < warn:
                issues.append(f"disk free {free_mb:.0f}MB < {warn}MB (warn)")
        except OSError:
            pass

        # 8. 进程内存：优先 daemon（chiguo_loop.pid 记录的 PID）查 /proc/<pid>/status 的 VmRSS；
        #    pid 文件存在但进程不可读 → null 不报错；pid 文件不存在 → 回退当前进程（独立 CLI 场景）
        rss_mb = None
        daemon_pid = None
        pid_file = Path("chiguo_loop.pid")
        pid_candidates = [pid_file]
        if not pid_file.is_absolute():
            pid_candidates.append(Path(__file__).resolve().parent / pid_file)
        for cand in pid_candidates:
            if cand.is_file():
                try:
                    daemon_pid = int(cand.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    daemon_pid = None
                break
        status_paths = []
        if daemon_pid:
            status_paths.append(f"/proc/{daemon_pid}/status")
        status_paths.append("/proc/self/status")  # daemon PID 无效/不可读时回退当前进程
        for status_path in status_paths:
            try:
                with open(status_path, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_kb = int(line.split()[1])
                            rss_mb = round(rss_kb / 1024, 1)
                            break
                if rss_mb is not None:
                    break
            except OSError:
                continue  # /proc/<pid> 不存在/不可读 → 尝试下一候选（最后回退 self）
        if rss_mb is not None:
            critical = self._monitor_config.get("memory_critical_mb", 1000)
            warn = self._monitor_config.get("memory_warn_mb", 500)
            if rss_mb > critical:
                healthy = False
                issues.append(f"process memory {rss_mb:.0f}MB > {critical}MB (critical)")
            elif rss_mb > warn:
                issues.append(f"process memory {rss_mb:.0f}MB > {warn}MB (warn)")

        # 9. 记忆后端连通性直检（v1.9: 按 [memory].backend 分流；mem0 检查本地向量库目录，
        # 自定义类路径不直检（由后端自身降级），避免误报 mem0 状态）
        mem0_direct = None  # None=未检测(自定义后端), True=正常, False=不可达
        mem_backend = self._memory_backend()
        if mem_backend in ("auto", "mem0", ""):
            qdir = self._mem0_qdrant_dir()
            if not qdir.is_dir():
                mem0_direct = False
                issues.append(f"mem0 qdrant dir missing: {qdir}")
            elif _HAS_MEM0:
                mem0_direct = True
            else:
                mem0_direct = False
                issues.append("mem0ai 未安装 → 记忆未启用(可选)")
        # 自定义类路径（含 .）→ mem0_direct stays None (skipped)

        return {
            "healthy": healthy,
            "last_tick": last_tick,
            "hours_since_tick": round(hours_ago, 1) if hours_ago else None,
            "state_version": version,
            "log_recent": log_recent,
            "config_ok": config_exists,
            "disk": disk_info,
            "memory": {"rss_mb": rss_mb},
            "mem0_direct": mem0_direct,
            "issues": issues,
            "checked_at": now.isoformat(),
        }

    # ═══════════════════════════════════════════════════════════
    # 综合监控报告
    # ═══════════════════════════════════════════════════════════

    def report(self, days: int = 7) -> dict:
        """完整监控报告：stats + alerts + health"""
        from chiguo_version import VERSION
        return {
            "app_version": VERSION,
            "stats": self.stats(days=days),
            "alerts": self.alerts(),
            "health": self.health(),
        }


    def conversation(self, date_str: str = None, days: int = None) -> list[dict]:
        """读取对话记录，按日期/天数过滤。

        Args:
            date_str: 单日查询 "YYYY-MM-DD"
            days: 最近N天查询

        Returns:
            按时间排序的消息列表
        """
        if not self.messages_log_path.exists():
            return []

        since = None
        if days is not None and days > 0:
            since = self._now() - timedelta(days=days)

        results = []
        for msg in self._iter_messages(since):
            if date_str:
                ts = self._parse_msg_ts(msg.get("ts"))
                # 无法解析时间戳 → 跳过（不泄漏到所有日期查询）
                if ts is None or ts.strftime("%Y-%m-%d") != date_str:
                    continue
            results.append(msg)

        return results

    def export(self, format: str = "json") -> str:
        """导出完整对话历史。format='json' 返回 JSON 字符串。"""
        msgs = self.conversation()
        if format == "json":
            return json.dumps(msgs, ensure_ascii=False, indent=2)
        return json.dumps(msgs, ensure_ascii=False)

    def _iter_messages(self, since: datetime | None = None):
        """流式读取 chiguo_messages.jsonl。"""
        if not self.messages_log_path.exists():
            return
        try:
            with open(self.messages_log_path, encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        msg = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if since:
                        ts = self._parse_msg_ts(msg.get("ts"))
                        if ts and ts < since:
                            continue
                    yield msg
        except OSError:
            return

    @staticmethod
    def _parse_msg_ts(ts_str: str | None) -> datetime | None:
        """解析消息 ts 字段（ISO格式，支持任意时区偏移）。"""
        if not ts_str:
            return None
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)  # 无时区 → 视为 CST
            return dt.astimezone(CST)
        except ValueError, TypeError:
            pass
        return None


# ═══════════════════════════════════════════════════════════
# v5: AlertManager — 告警生命周期管理
# ═══════════════════════════════════════════════════════════

class AlertManager:
    """告警持久化：active → acknowledged → resolved 生命周期。
    状态存储在 chiguo_alerts.json，原子写入。
    按 alert type 去重 —— 同类型重复触发只更新 last_seen。
    """

    def __init__(self, state_path: str = "chiguo_alerts.json"):
        self.state_path = Path(state_path)
        self._alerts: dict[str, dict] = {}
        self._load()

    def _load(self):
        """加载告警状态文件。缺失/损坏/形状错误 → 空字典。"""
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    self._alerts = {}  # 顶层非 dict（如列表/字符串）→ 回退空
                    return
                alerts = data.get("alerts", {})
                self._alerts = alerts if isinstance(alerts, dict) else {}
        except json.JSONDecodeError, OSError, UnicodeDecodeError:
            self._alerts = {}

    def _save(self):
        """原子写入：tmp → os.replace。"""
        try:
            data = json.dumps({
                "_version": 1,
                "alerts": self._alerts,
            }, ensure_ascii=False, indent=2)
            tmp = Path(str(self.state_path) + ".tmp")
            tmp.write_text(data)
            os.replace(str(tmp), str(self.state_path))
        except OSError:
            pass

    @staticmethod
    def _make_alert_id(alert_type: str) -> str:
        """从类型生成稳定ID（同一类型始终同ID，方便去重）。"""
        return f"alert_{alert_type}"

    def ingest(self, fresh_alerts: list[dict]) -> list[dict]:
        """摄入 monitor.alerts() 结果，与持久化状态合并。

        规则：
        - 新类型 → 创建 alert, status='active', count=1
        - 已有类型仍触发 → 更新 last_seen, count++, acknowledged 保持不变
        - 已有类型未触发 → 标记 status='resolved'
        """
        now = datetime.now(CST)
        fresh_types = set()

        for fa in fresh_alerts:
            alert_type = fa.get("type", "unknown")
            fresh_types.add(alert_type)
            alert_id = self._make_alert_id(alert_type)

            if alert_id in self._alerts:
                existing = self._alerts[alert_id]
                existing["last_seen"] = now.isoformat()
                prev_count = existing.get("count")
                # count 非数值（损坏文件）→ 重置为 1 再自增，避免 TypeError
                if not isinstance(prev_count, (int, float)) or isinstance(prev_count, bool):
                    prev_count = 1
                existing["count"] = prev_count + 1
                # 如果之前已 resolved，重新触发
                if existing.get("status") == "resolved":
                    existing["status"] = "active"
                    existing["resolved_at"] = None
                    existing["acknowledged_at"] = None  # 重置确认状态
                    # first_seen 保持不变（保留首次触发时间）
                existing["message"] = fa.get("message", existing.get("message", ""))
                existing["severity"] = fa.get("severity", existing.get("severity", "info"))
            else:
                self._alerts[alert_id] = {
                    "alert_id": alert_id,
                    "type": alert_type,
                    "severity": fa.get("severity", "info"),
                    "message": fa.get("message", ""),
                    "first_seen": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "count": 1,
                    "status": "active",
                    "acknowledged_at": None,
                    "resolved_at": None,
                }

        # 标记已消失的告警为 resolved
        for alert_id, alert in self._alerts.items():
            alert_type = alert.get("type", "")
            if alert_type not in fresh_types and alert.get("status") != "resolved":
                alert["status"] = "resolved"
                alert["resolved_at"] = now.isoformat()

        self._save()
        return self.list_active()

    def acknowledge(self, alert_id: str) -> bool:
        """确认告警。"""
        if alert_id in self._alerts:
            self._alerts[alert_id]["status"] = "acknowledged"
            self._alerts[alert_id]["acknowledged_at"] = datetime.now(CST).isoformat()
            self._save()
            return True
        return False

    def list_active(self) -> list[dict]:
        """返回 active + acknowledged 告警（不含 resolved）。"""
        return [a for a in self._alerts.values()
                if a.get("status") in ("active", "acknowledged")]

    def list_all(self) -> list[dict]:
        """返回所有告警（含 resolved）。"""
        return list(self._alerts.values())


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main() -> int:
    """独立 CLI 入口：JSON → stdout，诊断 → stderr。

    用法（与文件头注释及 doc/SYSTEM.md 一致）：
        python3 chiguo_monitor.py [--days 7] [--alerts] [--alerts-all] [--ack ALERT_ID]
        python3 chiguo_monitor.py --health
        python3 chiguo_monitor.py --report
    默认（无动作参数）输出 stats JSON；退出码 0=成功，1=--ack 未找到。
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="chiguo_monitor.py",
        description="迟菓主动消息 结构化监控（统计/告警/健康，JSON → stdout）")
    parser.add_argument("--days", type=int, default=7, metavar="N",
                        help="统计窗口天数（默认 7，0=全部历史）")
    parser.add_argument("--alerts", action="store_true",
                        help="异常告警（含持久化 ingest；配合 --alerts-all/--ack）")
    parser.add_argument("--alerts-all", action="store_true",
                        help="显示所有告警（含已解决，配合 --alerts）")
    parser.add_argument("--health", action="store_true",
                        help="增强版健康检查（JSON）")
    parser.add_argument("--report", action="store_true",
                        help="完整报告（stats + alerts + health，JSON）")
    parser.add_argument("--ack", type=str, default=None, metavar="ALERT_ID",
                        help="确认告警 (配合 --alerts)")
    args = parser.parse_args()

    # --ack 是告警确认参数，自动联动开启 alerts 处理（与 daemon 一致）
    if args.ack and not args.alerts:
        print("[chiguo_monitor] --ack 需要 --alerts，已自动联动开启", file=sys.stderr)
        args.alerts = True

    mon = ChiguoMonitor()

    if args.health:
        print(json.dumps(mon.health(), ensure_ascii=False, indent=2))
        return 0

    if args.alerts:
        am = AlertManager()
        if args.ack:
            ok = am.acknowledge(args.ack)
            print(json.dumps({"action": "ack", "alert_id": args.ack, "ok": ok},
                             ensure_ascii=False))
            return 0 if ok else 1
        fresh = mon.alerts()
        am.ingest(fresh)
        result = am.list_all() if args.alerts_all else am.list_active()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.report:
        print(json.dumps(mon.report(days=args.days), ensure_ascii=False, indent=2))
        return 0

    # 默认：结构化统计
    print(json.dumps(mon.stats(days=args.days), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

