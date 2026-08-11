#!/usr/bin/env python3
# ============================================================
# chiguo_envcheck.py — 环境就绪检查(v10.3)
# 检查:Python/uv 版本、pi-agent(pi --version)、
#       ollama embedding(qwen3-embedding)、auth.json [host].provider 条目（缺省 opencode-go）、
#       mem0 记忆层(qdrant 目录 + key + mem0ai)、网易云 API+登录、数据文件完整。
# 输出:JSON → stdout,汇总退出码 0=就绪 1=warn 2=critical(与 watchdog 一致)。
# 只读:不建目录、不写缓存、不启动服务;网易云/ollama 检查仅发轻量健康请求
#       (localhost 目标绕过系统代理,等价 curl --noproxy '*')。
# 参数:--skip-agent → 用户显式跳过 agent 安装(deploy.sh --skip-agent 传入)时,
#       agent 缺失降为 warn,不阻塞部署(如实报告降级)。
# 路径单一事实来源:chiguo_proactive.toml(与 daemon 相同读取点);
#       agent 侧路径(settings.json/auth.json/扩展)为 ~/.pi 约定(与 install_agent.sh 一致)。
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


def _sanitize_path(p) -> str:
    """detail 输出脱敏：绝对路径 → basename（防泄漏部署路径）；相对路径原样保留。"""
    s = str(p)
    try:
        if Path(s).is_absolute():
            return Path(s).name or s
    except (OSError, ValueError):
        pass
    return s


def _sanitize_url(url: str) -> str:
    """URL 脱敏：剥离 userinfo（user:pass@）与 query/fragment（token/key 等敏感参数），
    防凭据泄漏到输出（R24，如 OLLAMA_BASE?token=）。端口非法/IPv6 字面量同样安全
    （G8 自审：非数字端口访问 .port 抛 ValueError 不崩，退化为纯 hostname；
     IPv6 hostname 是去括号形式，重建需包回 [] 否则 urlunsplit 得畸形 URL）。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return str(url)
    if not (parts.username or parts.password or parts.query or parts.fragment):
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        pass  # 非数字端口：退化为纯 hostname 展示，不重建畸形 URL
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _truncate(text, limit: int = 120) -> str:
    """异常/命令输出截断：防路径、凭据或超长文本泄漏到 detail。"""
    s = str(text).strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"…(截断 {len(s) - limit} 字符)"


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
    """本地回环目标禁用系统代理(等价 install_agent.sh 的 curl --noproxy '*'),
    远程目标保留默认代理。本机有 http_proxy 时 localhost 直连不被劫持。"""
    host = urllib.parse.urlsplit(req.full_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def check_agent(agent_bin: str = "pi", skip_agent: bool = False,
                runner: str = "agent", agent_command: list[str] = None) -> dict:
    """消息生成后端可执行且可报告版本。缺失/不可运行 → critical(消息生成端缺失);
    --skip-agent 下缺失降为 warn(用户显式跳过,不阻塞部署但如实报告降级)。
    v1.8: runner=command 时检查自定义 agent 命令(任意 CLI 后端,
    经 scripts/agent-run.mjs 契约调用,见 doc/AGENT_INTEGRATION.md)。"""
    if runner != "agent":
        # 自定义 agent 后端:检查 agent_command[0] 可执行(绝对路径或 PATH)
        cmd = (agent_command or ["agent"])[0]
        name = "agent"
        resolved = None
        if cmd.startswith("/") or cmd.startswith("./"):
            resolved = cmd if os.path.exists(cmd) else None
        else:
            resolved = shutil.which(cmd)
        if not resolved:
            if skip_agent:
                return {"name": name, "ok": False, "severity": "warn",
                        "detail": f"agent 命令 {cmd} 不可用(--skip-agent) → 消息生成端缺失"
                                  f"(需先安装/配置 [host].agent_command)"}
            return {"name": name, "ok": False, "severity": "critical",
                    "detail": f"agent 命令 {cmd} 不可用 → 消息生成端缺失"
                              f"(配置 [host].runner/agent_command 后重跑 deploy.sh)"}
        try:
            out = subprocess.run([resolved, "--version"], capture_output=True,
                                 text=True, timeout=15)
            if out.returncode != 0:
                return {"name": name, "ok": False, "severity": "critical",
                        "detail": f"{cmd} --version 退出码 {out.returncode}: "
                                  f"{_truncate(out.stderr.strip() or out.stdout.strip() or '无输出')}"}
            ver = out.stdout.strip().splitlines()[0] if out.stdout.strip() else "?"
        except Exception as e:
            return {"name": name, "ok": False, "severity": "critical",
                    "detail": f"{cmd} --version 失败: {_truncate(e)}"}
        return {"name": name, "ok": True, "severity": "ok",
                "detail": f"agent OK ({cmd} {ver})"}
    resolved = shutil.which(agent_bin)
    if not resolved:
        if skip_agent:
            return {"name": "agent", "ok": False, "severity": "warn",
                    "detail": "agent 未安装(--skip-agent) → 消息生成端缺失,消息将无法生成"
                              "(需要时安装 agent 后端后重跑 deploy.sh)"}
        return {"name": "agent", "ok": False, "severity": "critical",
                "detail": "agent 未安装 → 消息生成端缺失(Phase 4 寄主;请安装 agent 后端后重跑 deploy.sh)"}
    try:
        out = subprocess.run([resolved, "--version"], capture_output=True,
                             text=True, timeout=15)
        if out.returncode != 0:
            return {"name": "agent", "ok": False, "severity": "critical",
                    "detail": f"pi --version 退出码 {out.returncode}: "
                              f"{_truncate(out.stderr.strip() or out.stdout.strip() or '无输出')}"
                              f" → 消息生成端异常(重装 agent 后端后重跑 deploy.sh)"}
        ver = out.stdout.strip().splitlines()[0] if out.stdout.strip() else "?"
    except Exception as e:
        return {"name": "agent", "ok": False, "severity": "critical",
                "detail": f"pi --version 失败: {_truncate(e)}"}
    return {"name": "agent", "ok": True, "severity": "ok", "detail": f"agent OK ({ver})"}


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
                    "detail": f"ollama embedding OK ({_sanitize_url(base_url)} 有 qwen3-embedding)"}
        return {"name": "ollama", "ok": False, "severity": "warn",
                "detail": f"ollama({_sanitize_url(base_url)}) 无 qwen3-embedding 模型 → 记忆 embedding 未启用"
                          f"(ollama pull qwen3-embedding:0.6b)"}
    except Exception as e:
        return {"name": "ollama", "ok": False, "severity": "warn",
                "detail": f"ollama 不可达({_sanitize_url(base_url)}): {_truncate(e)} → 记忆 embedding 未启用"
                          f"(启动 ollama 后 bash scripts/install_agent.sh --yes)"}


def check_agent_auth(auth_path: Path, provider: str = "opencode-go") -> dict:
    """auth.json 含 provider 条目(key 存在)。缺失 → warn(消息生成将失败)。
    provider = toml [host].provider（auth.json 键名与 pi --provider 名一致）。"""
    if not auth_path.is_file():
        return {"name": "agent_auth", "ok": False, "severity": "warn",
                "detail": f"{_sanitize_path(auth_path)} 不存在 → {provider} key 缺失"
                          f"(export {_key_env_hint(provider)}=... 后 bash scripts/install_agent.sh --yes)"}
    try:
        cfg = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"name": "agent_auth", "ok": False, "severity": "warn",
                "detail": f"{_sanitize_path(auth_path)} 解析失败: {_truncate(e)}"}
    entry = cfg.get(provider)
    if isinstance(entry, dict) and entry.get("key"):
        return {"name": "agent_auth", "ok": True, "severity": "ok",
                "detail": f"auth.json 含 {provider} key(已配置)"}
    return {"name": "agent_auth", "ok": False, "severity": "warn",
            "detail": f"auth.json 无 {provider} 条目 → 消息生成将失败"
                      f"(export {_key_env_hint(provider)}=... 后 bash scripts/install_agent.sh --yes"
                      f"，或配置其他 provider 见 doc/AGENT_INTEGRATION.md)"}


def _key_env_hint(provider: str) -> str:
    """key 环境变量名（chiguo 工具链统一入口）。install_agent.sh 阶段 5 只读
    AGENT_API_KEY（通用名）/OPENCODE_API_KEY（兼容回退）写 auth.json——
    提示必须与之一致，否则按提示操作写不进去。"""
    return "AGENT_API_KEY"


def _pi_api_key(provider: str = "opencode-go") -> str | None:
    """~/.pi/agent/auth.json 读 pi provider key；失败返回 None。"""
    try:
        import json as _json
        with open(os.path.expanduser("~/.pi/agent/auth.json"), encoding="utf-8") as f:
            return (_json.load(f).get(provider) or {}).get("key")
    except Exception:
        return None


def check_mem0(qdrant_dir: Path, history_db: Path) -> dict:
    """mem0 记忆层检查：mem0ai 可导入 + LLM key 存在 + 本地向量库目录就绪。
    mem0 为唯一记忆后端（必需依赖）：mem0ai 缺失 → critical（阻塞部署）；key 缺失 /
    qdrant 目录未初始化 → warn（部署时序：envcheck 先于 install_agent.sh 写 key，
    目录首写自动创建；具体可用性由后端自身降级）。"""
    try:
        import mem0  # noqa: F401
    except ImportError:
        return {"name": "mem0", "ok": False, "severity": "critical",
                "detail": "mem0ai 未安装 → 记忆层缺失(唯一记忆后端,必需依赖);运行 uv sync --all-extras"}
    if not _pi_api_key():
        return {"name": "mem0", "ok": False, "severity": "warn",
                "detail": "~/.pi/agent/auth.json 无 opencode-go key → 记忆写入不可用(mem0 唯一后端)"}
    if not qdrant_dir.is_dir():
        return {"name": "mem0", "ok": False, "severity": "warn",
                "detail": "记忆库未初始化(mem0 唯一后端;首次对话自动创建)"}
    if not history_db.is_file():
        return {"name": "mem0", "ok": True, "severity": "ok",
                "detail": f"mem0 OK ({_sanitize_path(qdrant_dir)}，历史库未创建)"}
    return {"name": "mem0", "ok": True, "severity": "ok",
            "detail": f"mem0 OK ({_sanitize_path(qdrant_dir)})"}


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
        issues.append(f"API 不可达: {_truncate(e)}")
    if cookie_path.is_file():
        issues.append("已登录(netease_cookie.txt)")
    else:
        issues.append("未登录(可选来源,未启用不介入;启用需 uv run python -m netease.bridge --login 扫码)")
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


def run_checks(base_dir: Path = None, skip_agent: bool = False, home: Path = None) -> dict:
    """按序执行 7 组检查。返回完整报告 dict。单项失败不中断。
    skip_agent: deploy.sh --skip-agent 传入 → agent 缺失降为 warn,不阻塞部署。
    home: 测试注入用（默认 Path.home()）。"""
    base = base_dir or _BASE_DIR
    cfg = _load_config(base)
    mem0_qdrant = _cfg_path(cfg, "memory", "mem0_qdrant_path", "data/mem0/qdrant", base)
    mem0_history = _cfg_path(cfg, "memory", "mem0_history_db", "data/mem0/history.db", base)
    xlsx = _cfg_path(cfg, "schedule", "xlsx_path", "data/xskb.xlsx", base)
    mem = _cfg_path(cfg, "memory", "manual_path", "data/chiguo_memories.json", base)
    api_base = os.environ.get("NETEASE_API_BASE", "http://localhost:3000")
    home = home or Path.home()
    agent_auth = home / ".pi" / "agent" / "auth.json"
    ollama_url = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
    provider = cfg.get("host", {}).get("provider") or "opencode-go"
    # v1.8: agent 后端抽象（runner=agent 默认；command=自定义 CLI agent）
    runner = cfg.get("host", {}).get("runner") or "agent"
    agent_command = cfg.get("host", {}).get("agent_command") or None
    # 记忆后端检查（mem0 为唯一后端，恒直检；具体可用性由后端自身降级）
    checks = [
        check_env(),
        check_agent(skip_agent=skip_agent, runner=runner, agent_command=agent_command),
    ]
    checks.append(check_mem0(mem0_qdrant, mem0_history))
    if runner == "agent":
        checks.append(check_ollama(ollama_url))
        checks.append(check_agent_auth(agent_auth, provider=provider))
    checks.append(
        check_netease(api_base, base / "netease" / "netease_cookie.txt", base / "netease" / "netease_health.json"),
    )
    checks.append(check_data(xlsx, mem))
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
    skip_agent = "--skip-agent" in sys.argv[1:]
    report = run_checks(skip_agent=skip_agent)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
