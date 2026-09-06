#!/usr/bin/env python3
"""monitor.health — health 视图（#378 纯搬运 + #408 R23 锚定修复）。

HealthMixin 由 monitor.base.ChiguoMonitor 组装；self.* helper
（_read_state/_now/_mem0_qdrant_dir/_monitor_config + 路径属性）
运行时经宿主类解析。

R23（test_health_disk_check_anchored_to_project 验证）：磁盘检查必须锚定
chiguo_paths.PROJECT_ROOT，随 health() 搬家含义不再随模块文件位置漂移。
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from chiguo_paths import PROJECT_ROOT
from chiguo_time import CST

from .helpers import _HAS_MEM0


class HealthMixin:
    """健康检查视图：health()（自 chiguo_monitor.py 搬运）。

    R23: 磁盘检查锚定 chiguo_paths.PROJECT_ROOT（原 Path(__file__).parent
    在 health 搬入 monitor/health.py 后会指向 monitor/ 子目录，含义漂移，
    故必须改用项目根锚点——本视图唯一允许的行为修正）。
    """

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
                PROJECT_ROOT / "netease" / "netease_health.json")  # #378: 原模块目录即项目根
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
            config_exists = (PROJECT_ROOT / self.config_path).exists()  # #378: 原模块目录即项目根
        if not config_exists:
            healthy = False
            issues.append("config file missing")

        # 7. 磁盘空间
        disk_info = {"free_mb": None, "total_mb": None, "used_mb": None}
        try:
            usage = shutil.disk_usage(PROJECT_ROOT)  # R23: 锚定项目根,防 cwd/搬家漂移假阴性(#378)
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
            pid_candidates.append(PROJECT_ROOT / pid_file)  # #378: 原模块目录即项目根
        for cand in pid_candidates:
            if not cand.is_file():
                continue
            try:
                daemon_pid = int(cand.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue  # 解析失败 → 尝试下一候选（此前 break 会截断回退）
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

        # 9. 记忆后端连通性直检（mem0 唯一后端，恒检本地向量库目录）
        qdir = self._mem0_qdrant_dir()
        if not qdir.is_dir():
            mem0_direct = False
            issues.append(f"mem0 qdrant dir missing: {qdir}")
        elif _HAS_MEM0:
            mem0_direct = True
        else:
            mem0_direct = False
            issues.append("mem0ai 未安装 → 记忆层缺失(唯一记忆后端)")

        # 10. mem0 写链故障（F-RT-017）：state 文件的 mem0_write 快照由 daemon
        # 每次 save 透传（见 state/persistence.py）。add_fail_count>0 即告警提示，
        # 不置 healthy=False（写失败不影响主链路，daemon 侧静默即可）。
        mem0_write = state.get("mem0_write") or {}
        if not isinstance(mem0_write, dict):  # state 被手改/损坏成非 dict 真值 → 回退空
            mem0_write = {}
        add_fail_count = mem0_write.get("add_fail_count", 0)
        if isinstance(add_fail_count, int) and add_fail_count > 0:
            last_err = mem0_write.get("last_error")
            err_str = f" last_error={last_err}" if last_err else ""
            issues.append(
                f"mem0 写链累计失败 {add_fail_count} 次（LLM 事实提取端点可能故障）{err_str}")

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
            "mem0_write": mem0_write,
            "issues": issues,
            "checked_at": now.isoformat(),
        }
