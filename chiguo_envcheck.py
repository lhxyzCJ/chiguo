#!/usr/bin/env python3
# ============================================================
# chiguo_envcheck.py — 环境就绪检查(v10.3)
# 检查:Python/uv 版本、pi-agent(pi --version)、pi 扩展路径(settings.json)、
#       ollama embedding(qwen3-embedding)、auth.json [host].provider 条目（缺省 opencode-go）、
#       LanceDB lancedb-pro、网易云 API+登录、数据文件完整。
# 输出:JSON → stdout,汇总退出码 0=就绪 1=warn 2=critical(与 watchdog 一致)。
# 只读:不建目录、不写缓存、不启动服务;网易云/ollama 检查仅发轻量健康请求
#       (localhost 目标绕过系统代理,等价 curl --noproxy '*')。
# 参数:--skip-pi → 用户显式跳过 pi 安装(deploy.sh --skip-pi 传入)时,
#       pi 缺失降为 warn,不阻塞部署(如实报告降级)。
# 路径单一事实来源:chiguo_proactive.toml(与 daemon 相同读取点);
#       pi 侧路径(settings.json/auth.json/扩展)为 ~/.pi 约定(与 install_pi.sh 一致)。
# ============================================================

import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path


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


def _urlopen(req, timeout: float = 5):
    """本地回环目标禁用系统代理(等价 install_pi.sh 的 curl --noproxy '*'),
    远程目标保留默认代理。本机有 http_proxy 时 localhost 直连不被劫持。"""
    host = urllib.parse.urlsplit(req.full_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def check_pi(pi_bin: str = "pi", skip_pi: bool = False) -> dict:
    """pi-agent 可执行且可报告版本。缺失/不可运行 → critical(消息生成端缺失);
    --skip-pi 下缺失降为 warn(用户显式跳过 pi,不阻塞部署但如实报告降级)。"""
    resolved = shutil.which(pi_bin)
    if not resolved:
        if skip_pi:
            return {"name": "pi", "ok": False, "severity": "warn",
                    "detail": "pi 未安装(--skip-pi) → 消息生成端缺失,消息将无法生成"
                              "(需要时安装 pi-agent 后重跑 deploy.sh)"}
        return {"name": "pi", "ok": False, "severity": "critical",
                "detail": "pi 未安装 → 消息生成端缺失(Phase 4 寄主;请安装 pi-agent 后重跑 deploy.sh)"}
    try:
        out = subprocess.run([resolved, "--version"], capture_output=True,
                             text=True, timeout=15)
        ver = out.stdout.strip().splitlines()[0] if out.stdout.strip() else "?"
    except Exception as e:
        return {"name": "pi", "ok": False, "severity": "critical",
                "detail": f"pi --version 失败: {e}"}
    return {"name": "pi", "ok": True, "severity": "ok", "detail": f"pi OK ({ver})"}


def _is_windows_ext(e) -> bool:
    return (isinstance(e, str)
            and ("/mnt/" in e or "\\" in e
                 or (len(e) > 1 and e[1] == ":" and e[0].isalpha())))


def check_pi_ext(settings_path: Path, expected_path: Path) -> dict:
    """pi settings.json extensions 指向 Linux 扩展路径。缺失/指向 Windows 残留 → warn。"""
    if not settings_path.is_file():
        return {"name": "pi_ext", "ok": False, "severity": "warn",
                "detail": f"{settings_path} 不存在 → pi 记忆扩展未注册(bash scripts/install_pi.sh --yes)"}
    try:
        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        exts = cfg.get("extensions") or []
        if not isinstance(exts, list):
            exts = []
    except Exception as e:
        return {"name": "pi_ext", "ok": False, "severity": "warn",
                "detail": f"{settings_path} 解析失败: {e}(bash scripts/install_pi.sh --yes 修复)"}
    want = str(expected_path)
    bad = [e for e in exts if _is_windows_ext(e)]
    if want in exts and not bad:
        return {"name": "pi_ext", "ok": True, "severity": "ok",
                "detail": f"pi 扩展 OK ({want})"}
    if bad:
        return {"name": "pi_ext", "ok": False, "severity": "warn",
                "detail": f"settings.json 扩展指向 Windows 残留 {bad} → 记忆扩展不会加载"
                          f"(bash scripts/install_pi.sh --yes 修正)"}
    return {"name": "pi_ext", "ok": False, "severity": "warn",
            "detail": f"settings.json 缺扩展路径 {want} → 记忆扩展不会加载"
                      f"(bash scripts/install_pi.sh --yes)"}


def check_ollama(base_url: str = "http://localhost:11434") -> dict:
    """ollama 服务可达 + qwen3-embedding 模型在册。任一缺失 → warn(记忆 embedding 降级)。"""
    try:
        req = urllib.request.Request(f"{base_url}/api/tags",
                                     headers={"User-Agent": "chiguo-envcheck"})
        with _urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in data.get("models", [])]
        if any(n.startswith("qwen3-embedding") for n in names):
            return {"name": "ollama", "ok": True, "severity": "ok",
                    "detail": f"ollama embedding OK ({base_url} 有 qwen3-embedding)"}
        return {"name": "ollama", "ok": False, "severity": "info",
                "detail": f"ollama({base_url}) 无 qwen3-embedding 模型 → 记忆 embedding 未启用(可选)"
                          f"(ollama pull qwen3-embedding:0.6b)"}
    except Exception as e:
        return {"name": "ollama", "ok": False, "severity": "info",
                "detail": f"ollama 不可达({base_url}): {e} → 记忆 embedding 未启用(可选)"
                          f"(启动 ollama 后 bash scripts/install_pi.sh --yes)"}


def check_pi_auth(auth_path: Path, provider: str = "opencode-go") -> dict:
    """auth.json 含 provider 条目(key 存在)。缺失 → warn(消息生成将失败)。
    provider = toml [host].provider（auth.json 键名与 pi --provider 名一致）。"""
    if not auth_path.is_file():
        return {"name": "pi_auth", "ok": False, "severity": "warn",
                "detail": f"{auth_path} 不存在 → {provider} key 缺失"
                          f"(export {_key_env_hint(provider)}=... 后 bash scripts/install_pi.sh --yes)"}
    try:
        cfg = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"name": "pi_auth", "ok": False, "severity": "warn",
                "detail": f"{auth_path} 解析失败: {e}"}
    entry = cfg.get(provider)
    if isinstance(entry, dict) and entry.get("key"):
        return {"name": "pi_auth", "ok": True, "severity": "ok",
                "detail": f"auth.json 含 {provider} key(已配置)"}
    return {"name": "pi_auth", "ok": False, "severity": "warn",
            "detail": f"auth.json 无 {provider} 条目 → 消息生成将失败"
                      f"(export {_key_env_hint(provider)}=... 后 bash scripts/install_pi.sh --yes"
                      f"，或配置其他 provider 见 doc/PI_INTEGRATION.md)"}


def _key_env_hint(provider: str) -> str:
    """key 环境变量名（chiguo 工具链统一入口）。install_pi.sh 阶段 5 只读
    PI_API_KEY（通用名）/OPENCODE_API_KEY（兼容回退）写 auth.json——
    提示必须与之一致，否则按提示操作写不进去。"""
    return "PI_API_KEY"


def check_lancedb(db_path: Path) -> dict:
    """lancedb 库可导入 + 数据库可只读连接。任一缺失 → warn(JSON 降级可用)。"""
    try:
        import lancedb
    except ImportError:
        return {"name": "lancedb", "ok": False, "severity": "info",
                "detail": "lancedb 未安装 → 记忆未启用(可选,JSON 兜底)"}
    if not db_path.is_dir():
        return {"name": "lancedb", "ok": False, "severity": "info",
                "detail": f"{db_path} 不存在 → 记忆未启用(可选,JSON 兜底)"}
    try:
        db = lancedb.connect(str(db_path))
        table = db.open_table("memories")
        _ = table.schema
        return {"name": "lancedb", "ok": True, "severity": "ok",
                "detail": f"LanceDB OK ({db_path}/memories)"}
    except Exception as e:
        return {"name": "lancedb", "ok": False, "severity": "info",
                "detail": f"LanceDB 不可用: {e}(可选,JSON 兜底)"}


def check_netease(api_base: str, cookie_path: Path, health_path: Path) -> dict:
    """网易云 API 可达 + 登录态。任一缺失 → warn(话题源降级,不影响主流程)。"""
    issues = []
    api_ok = False
    try:
        req = urllib.request.Request(f"{api_base}/login/status",
                                     headers={"User-Agent": "chiguo-envcheck"})
        with _urlopen(req, timeout=5) as resp:
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
        issues.append("未登录(可选来源,未启用不介入;启用需 uv run python netease_bridge.py --login 扫码)")
    if health_path.is_file():
        issues.append("netease_health.json 存在")
    else:
        issues.append("netease_health.json 缺失(daemon 运行后自动生成)")
    ok = api_ok and cookie_path.is_file()
    return {"name": "netease", "ok": ok, "severity": "ok" if ok else "info",
            "detail": "; ".join(issues)}


def check_data(xlsx_path: Path, memories_path: Path) -> dict:
    """课表 + 手动记忆存在且可读。缺失 → info(可选来源,未启用不介入)。"""
    missing = []
    for label, p in (("课表", xlsx_path), ("手动记忆", memories_path)):
        if p.is_file():
            pass
        else:
            missing.append(f"{label} 未启用(可选来源,缺了按无课表/空记忆处理)")
    if missing:
        return {"name": "data", "ok": False, "severity": "info", "detail": "; ".join(missing)}
    return {"name": "data", "ok": True, "severity": "ok",
            "detail": f"课表/手动记忆 OK ({xlsx_path.name}, {memories_path.name})"}


def run_checks(base_dir: Path = None, skip_pi: bool = False, home: Path = None) -> dict:
    """按序执行 8 组检查。返回完整报告 dict。单项失败不中断。
    skip_pi: deploy.sh --skip-pi 传入 → pi 缺失降为 warn,不阻塞部署。
    home: 测试注入用（默认 Path.home()）。"""
    base = base_dir or _BASE_DIR
    cfg = _load_config(base)
    lancedb_path = _cfg_path(cfg, "memory", "lancedb_path",
                             "~/.pi-agent/memory/lancedb-pro", base)
    xlsx = _cfg_path(cfg, "schedule", "xlsx_path", "data/xskb.xlsx", base)
    mem = _cfg_path(cfg, "memory", "manual_path", "data/chiguo_memories.json", base)
    api_base = os.environ.get("NETEASE_API_BASE", "http://localhost:3000")
    home = home or Path.home()
    pi_settings = home / ".pi" / "agent" / "settings.json"
    pi_ext = home / ".pi-agent" / "TestForPi-memory-lancedb-pro" / "dist" / "pi-adapter" / "index.js"
    pi_auth = home / ".pi" / "agent" / "auth.json"
    ollama_url = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
    provider = cfg.get("host", {}).get("provider") or "opencode-go"
    checks = [
        check_env(),
        check_pi(skip_pi=skip_pi),
        check_pi_ext(pi_settings, pi_ext),
        check_lancedb(lancedb_path),
        check_ollama(ollama_url),
        check_pi_auth(pi_auth, provider=provider),
        check_netease(api_base, base / "netease_cookie.txt", base / "netease_health.json"),
        check_data(xlsx, mem),
    ]
    summary = {"ok": 0, "info": 0, "warn": 0, "critical": 0}
    for c in checks:
        summary[c["severity"]] = summary.get(c["severity"], 0) + 1
    ready = summary["critical"] == 0
    from chiguo_version import VERSION
    return {"app_version": VERSION, "checks": checks, "summary": summary, "ready": ready}


def exit_code(report: dict) -> int:
    """汇总退出码:0=就绪,1=有 warn,2=有 critical。"""
    s = report["summary"]
    if s.get("critical", 0) > 0:
        return 2
    if s.get("warn", 0) > 0:
        return 1
    return 0


def main() -> int:
    skip_pi = "--skip-pi" in sys.argv[1:]
    report = run_checks(skip_pi=skip_pi)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
