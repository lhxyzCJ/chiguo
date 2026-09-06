#!/usr/bin/env python3
"""monitor.base — ChiguoMonitor 壳（#378 纯搬运，零行为变化）。

ChiguoMonitor 经 StatsMixin/AlertsMixin/HealthMixin 组装；本模块含全部
I/O 与解析 helper（_read_state/_iter_decisions/_parse_time_str 等）+
report/conversation/export + main CLI。

R23 类修正（与 health 磁盘锚定同理，搬家后 Path(__file__).parent 含义漂移，
故原“模块所在目录即项目根”的回退一律改用 chiguo_paths.PROJECT_ROOT，
行为与搬家前一致）：
- _load_monitor_config 相对 config 回退
- _mem0_qdrant_dir 相对 qdrant 目录锚定
"""

import json
import os
import sys
import tomllib
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from chiguo_paths import PROJECT_ROOT
from chiguo_time import CST
from decision_schema import validate as validate_decision  # Q16: 消费同一 schema

from .alerts import AlertManager, AlertsMixin
from .health import HealthMixin
from .stats import StatsMixin


class ChiguoMonitor(StatsMixin, AlertsMixin, HealthMixin):
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
                 messages_log_path: str = "chiguo_messages.jsonl",
                 alerts_path: str = "chiguo_alerts.json",
                 events_path: str = "chiguo_events.jsonl"):
        self.log_path = Path(log_path)
        self.state_path = Path(state_path)
        self.break_state_path = Path(break_state_path)
        self.messages_log_path = Path(messages_log_path)
        self.config_path = Path(config_path)
        # Q24 (#275): 时序指标数据源——告警持久化(chiguo_alerts.json)与
        # 事件审计(chiguo_events.jsonl，轮转等)一并纳入 stats() 事件时间序列。
        self.alerts_path = Path(alerts_path)
        self.events_path = Path(events_path)
        self._monitor_config = self._load_monitor_config(self.config_path)
        self._invalid_decision_count = 0  # B10: _iter_decisions 校验失败计数（stats 窗口粒度）

    def _load_monitor_config(self, config_path: Path) -> dict:
        """读取 [monitor] 段配置，缺省硬编码阈值。
        相对路径在当前 cwd 找不到时回退到项目根（与 health() 的 config 检测一致），
        避免从其他 cwd 运行时阈值静默回落默认值。"""
        defaults = {
            "disk_warn_mb": 500,
            "disk_critical_mb": 100,
            "memory_warn_mb": 500,
            "memory_critical_mb": 1000,
            "mem0_qdrant_path": "data/mem0/qdrant",
            "mem0_history_db": "data/mem0/history.db",
            "backend": "mem0",         # mem0 唯一记忆后端（[memory].backend 镜像）
        }
        candidates = [config_path]
        if not config_path.is_absolute():
            candidates.append(PROJECT_ROOT / config_path)
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
                # [memory].backend 镜像（mem0 唯一后端）
                defaults["backend"] = cfg.get("memory", {}).get("backend") or defaults["backend"]
                break
            except (ValueError, TypeError, OSError):
                continue
        return defaults

    def _mem0_qdrant_dir(self) -> Path:
        """mem0 本地向量库目录。优先级：[monitor] > [memory] > 默认 data/mem0/qdrant。
        相对路径锚定项目根；~ 展开为 $HOME。"""
        raw = self._monitor_config.get("mem0_qdrant_path", "data/mem0/qdrant")
        p = Path(os.path.expanduser(raw))
        if p.is_absolute():
            return p
        return PROJECT_ROOT / p

    # ═══════════════════════════════════════════════════════════
    # 内部：流式解析
    # ═══════════════════════════════════════════════════════════

    def _now(self) -> datetime:
        return datetime.now(CST)

    # 尾读行数上限：最近 N 行满足 since 窗口即可（避免全扫）。
    # 经验：14天窗口 ≈ 96*14 ≈ 1344 行；5000 行留足余量覆盖未来增长。
    _MAX_TAIL_LINES = 5000

    def _iter_decisions(self, since: datetime | None = None):
        """流式迭代 decisions.jsonl，支持逆向尾读优化。

        策略：
        - 无 since（days=0 全历史）→ 正向全扫（保持原语义）。
        - 有 since（窗口查询）→ 逆向读取最近 _MAX_TAIL_LINES 行，正向 yield。
          一旦收集到的最早行 < since，即可终止（窗口已满足），避免全扫。
        - 损坏行静默跳过；schema 校验在 since 过滤后计数（窗口粒度）。

        Q16：每条 dict 记录都经决策 schema（decision_schema.validate）消费——
        旧 jsonl 无 contract 键按缺省 1 处理（兼容不跳）；仍 yield 原记录，
        不据此跳过（不破坏历史读取）。真正的形状防御由下游 _normalize_entry。
        B10：消费 validate_decision 返回值——非法记录计入 _invalid_decision_count
        （由 stats() 暴露为 period.invalid_decision_count），非法 JSONL 行既不再
        静默吞没也不影响 stats 循环（统计仍基于合法可解析行）。计数在 since
        过滤之后（仅窗口内将 yield 的行），与 unparsed_time_count 窗口语义一致。
        """
        if not self.log_path.exists():
            return

        # 无时间窗口 → 正向全扫（保持原语义、对齐现有测试期望）
        if since is None:
            try:
                with open(self.log_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(d, dict):
                            continue
                        errs = validate_decision(d)
                        if errs:
                            self._invalid_decision_count += 1
                        yield d
            except OSError:
                return
            return

        # 有 since 窗口 → 逆向尾读，正向 yield
        try:
            # 读取最后 _MAX_TAIL_LINES 行（逆向）
            tail_lines = self._read_tail_lines(self._MAX_TAIL_LINES)
            # 逆序收集：从最新向最旧，直到遇到 < since 的行
            collected: list[dict] = []
            for line in reversed(tail_lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                ts = self._extract_time(d)
                if ts and ts < since:
                    # 已读到窗口外的行 → 停止（更旧的行必然也 < since，因文件追加单调）
                    break
                collected.append(d)

            # 正向 yield（按时间序，兼容下游期望的顺序）
            for d in reversed(collected):
                errs = validate_decision(d)
                if errs:  # 窗口内非法记录计数
                    self._invalid_decision_count += 1
                yield d
        except OSError:
            return

    def _read_tail_lines(self, max_lines: int) -> list[str]:
        """读取文件最后 N 行（正向全文件读 + deque 有界缓冲）。

        实现：正向逐行读全文件，仅保留最后 max_lines 行（deque maxlen）。
        内存 O(max_lines)，但 I/O 为 O(全文件)——非真正的逆向 seek 尾读，
        大文件窗口查询仍有全文件读代价（后续可优化为块级逆向 seek）。
        """
        if not self.log_path.exists():
            return []
        buf: deque[str] = deque(maxlen=max_lines)
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    buf.append(line)
        except OSError:
            return []
        return list(buf)

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
    # 综合监控报告（唯一同时依赖三视图的代码，留 base 防循环 import）
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
            with open(self.messages_log_path, encoding="utf-8", errors="replace") as f:
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
