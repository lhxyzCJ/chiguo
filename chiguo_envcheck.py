#!/usr/bin/env python3
# ============================================================
# chiguo_envcheck.py — 环境就绪检查(v10.1)
# 检查:Python/uv 版本、OpenClaw+skill 目录、LanceDB lancedb-pro、
#       网易云 API+登录、数据文件完整。
# 输出:JSON → stdout,汇总退出码 0=就绪 1=warn 2=critical(与 watchdog 一致)。
# 只读:不建目录、不写缓存、不启动服务;网易云检查仅发轻量健康请求。
# 路径单一事实来源:chiguo_proactive.toml(与 daemon 相同读取点)。
# ============================================================

import json
import os
import shutil
import sys
import tomllib
import urllib.request
import urllib.error
from pathlib import Path

CST_NOTE = None  # 无时区逻辑,占位保持模块风格一致

_BASE_DIR = Path(__file__).resolve().parent


def _load_config(base_dir: Path = None) -> dict:
    """读取 chiguo_proactive.toml(base_dir 默认模块目录)。失败返回空 dict(全用默认)。"""
    base = base_dir or _BASE_DIR
    try:
        with open(base / "chiguo_proactive.toml", "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        return {"_error": str(e)}


def _cfg_path(config: dict, section: str, key: str, default: str, base_dir: Path) -> Path:
    """从 toml 读路径:绝对路径原样保留;相对路径锚定 base_dir;~ 展开为 $HOME。"""
    raw = config.get(section, {}).get(key, default)
    p = Path(os.path.expanduser(raw))
    if p.is_absolute():
        return p
    return base_dir / p


def check_env() -> dict:
    """Python >= 3.14 + uv 可用。版本不足/缺 uv → critical(3.14-only 语法必挂)。"""
    ok = True
    detail = f"Python {sys.version.split()[0]}"
    if sys.version_info < (3, 14):
        ok = False
        detail += " < 3.14(3.14-only 语法会解析失败)"
    if shutil.which("uv"):
        detail += ", uv OK"
    else:
        ok = False
        detail += ", uv 未安装(请运行 bash deploy.sh 或安装 uv)"
    return {"name": "env", "ok": ok,
            "severity": "ok" if ok else "critical", "detail": detail}


def check_openclaw(personality_dir: Path) -> dict:
    """personality_source 目录(来自 toml)。目录缺失 → critical;skill 文件缺 → warn。"""
    if not personality_dir.is_dir():
        return {"name": "openclaw", "ok": False, "severity": "critical",
                "detail": f"{personality_dir} 不存在 → OpenClaw 未安装或 personality_source 配置错误(消息生成端缺失)"}
    missing = [n for n in ("SUN2.md", "SKILL.md") if not (personality_dir / n).is_file()]
    if missing:
        return {"name": "openclaw", "ok": False, "severity": "warn",
                "detail": f"{personality_dir} 缺少 {', '.join(missing)} → 人格设定缺失,OpenClaw 仍可发消息"}
    return {"name": "openclaw", "ok": True, "severity": "ok",
            "detail": f"OpenClaw skill OK ({personality_dir} 含 SUN2.md/SKILL.md)"}


def check_lancedb(db_path: Path) -> dict:
    """lancedb 库可导入 + 数据库可只读连接。任一缺失 → warn(JSON 降级可用)。"""
    try:
        import lancedb
    except ImportError:
        return {"name": "lancedb", "ok": False, "severity": "warn",
                "detail": "lancedb 未安装 → 记忆降级 JSON 模式(可运行: uv pip install lancedb)"}
    if not db_path.is_dir():
        return {"name": "lancedb", "ok": False, "severity": "warn",
                "detail": f"{db_path} 不存在 → 记忆降级 JSON 模式(OpenClaw 侧 memory 插件未初始化?)"}
    try:
        db = lancedb.connect(str(db_path))
        table = db.open_table("memories")
        _ = table.schema
        return {"name": "lancedb", "ok": True, "severity": "ok",
                "detail": f"LanceDB OK ({db_path}/memories)"}
    except Exception as e:
        return {"name": "lancedb", "ok": False, "severity": "warn",
                "detail": f"LanceDB 不可用: {e}"}


def check_netease(api_base: str, cookie_path: Path, health_path: Path) -> dict:
    """网易云 API 可达 + 登录态。任一缺失 → warn(话题源降级,不影响主流程)。"""
    issues = []
    api_ok = False
    try:
        req = urllib.request.Request(f"{api_base}/login/status",
                                     headers={"User-Agent": "chiguo-envcheck"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
        if status == 200:
            api_ok = True
            issues.append(f"API OK({status})")
        else:
            issues.append(f"API HTTP {status}")
    except Exception as e:
        issues.append(f"API 不可达: {e}")
    if cookie_path.is_file():
        issues.append("已登录(netease_cookie.txt)")
    else:
        issues.append("未登录 → 需 uv run python netease_bridge.py --login 扫码")
    if health_path.is_file():
        issues.append("netease_health.json 存在")
    else:
        issues.append("netease_health.json 缺失(daemon 运行后自动生成)")
    ok = api_ok and cookie_path.is_file()
    return {"name": "netease", "ok": ok, "severity": "ok" if ok else "warn",
            "detail": "; ".join(issues)}


def check_data(xlsx_path: Path, memories_path: Path) -> dict:
    """课表 + 手动记忆存在且可读。缺失 → warn(均有降级路径)。"""
    missing = []
    for label, p in (("课表", xlsx_path), ("手动记忆", memories_path)):
        if p.is_file():
            pass
        else:
            missing.append(f"{label} {p} 缺失(有降级:课表→availability=0.85,记忆→空)")
    if missing:
        return {"name": "data", "ok": False, "severity": "warn", "detail": "; ".join(missing)}
    return {"name": "data", "ok": True, "severity": "ok",
            "detail": f"课表/手动记忆 OK ({xlsx_path.name}, {memories_path.name})"}


def run_checks(home: Path = None, base_dir: Path = None) -> dict:
    """按序执行 5 组检查。返回完整报告 dict。单项失败不中断。"""
    base = base_dir or _BASE_DIR
    cfg = _load_config(base)
    lancedb_path = _cfg_path(cfg, "memory", "lancedb_path",
                             "~/.openclaw/memory/lancedb-pro", base)
    personality = _cfg_path(cfg, "openclaw", "personality_source",
                            "~/.openclaw/workspace/skills/chiguo", base)
    xlsx = _cfg_path(cfg, "schedule", "xlsx_path", "data/xskb.xlsx", base)
    mem = _cfg_path(cfg, "memory", "manual_path", "data/chiguo_memories.json", base)
    api_base = os.environ.get("NETEASE_API_BASE", "http://localhost:3000")
    checks = [
        check_env(),
        check_openclaw(personality),
        check_lancedb(lancedb_path),
        check_netease(api_base, base / "netease_cookie.txt", base / "netease_health.json"),
        check_data(xlsx, mem),
    ]
    summary = {"ok": 0, "warn": 0, "critical": 0}
    for c in checks:
        summary[c["severity"]] = summary.get(c["severity"], 0) + 1
    ready = summary["critical"] == 0
    return {"checks": checks, "summary": summary, "ready": ready}


def exit_code(report: dict) -> int:
    """汇总退出码:0=就绪,1=有 warn,2=有 critical。"""
    s = report["summary"]
    if s.get("critical", 0) > 0:
        return 2
    if s.get("warn", 0) > 0:
        return 1
    return 0


def main() -> int:
    report = run_checks()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
