# 目录整理 + 环境检查脚本 实施计划 (v10.1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 数据/资源文件移入 `data/` 子目录,新增 `chiguo_envcheck.py` 环境就绪检查 CLI,deploy.sh 去重集成。

**Architecture:** 目录整理只动路径值/默认值(全部走既有 `_base_dir` 相对锚定);envcheck 为纯标准库独立模块,5 组检查函数接受可注入路径参数(便于测试隔离),CLI 输出 JSON + 退出码 0/1/2(与 watchdog 约定一致)。

**Tech Stack:** Python 3.14(uv),tomllib,urllib,零外部依赖;测试为独立 runner(非 pytest),`uv run python test_xxx.py`,失败退出码非 0。

**Spec:** `docs/superpowers/specs/2026-08-01-directory-cleanup-and-envcheck-design.md`

---
## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `data/xskb.xlsx` | git mv | 课表 |
| `data/chiguo_memories.json` | git mv | 手动记忆 |
| `data/netease_qr.png` | git mv | 登录二维码输出 |
| `chiguo_proactive.toml:19,171` | 修改 | 路径值 → `data/` |
| `chiguo_state.py:235,322` | 修改 | 默认值 → `data/` |
| `schedule_parser.py:36,438` | 修改 | 默认值 → `data/` |
| `netease_bridge.py:172` | 修改 | QR 输出锚定 `data/` |
| `chiguo_envcheck.py` | 新建 | 环境检查 CLI(5 组检查 + JSON + 退出码) |
| `test_envcheck.py` | 新建 | envcheck 单元测试(第 19 个 runner) |
| `deploy.sh` | 修改 | 第 4/5 节改调 envcheck |
| `README.md`/`doc/README.md`/`doc/SYSTEM.md`/`MEMORY.md`/`doc/IMPROVE.md` | 修改 | 文档同步 |

---

### Task 1: 数据文件移入 data/ + 路径同步

**Files:**
- Modify: `data/`(git mv 3 个文件)
- Modify: `chiguo_proactive.toml:19,171`
- Modify: `chiguo_state.py:235,322`
- Modify: `schedule_parser.py:36,438`
- Modify: `netease_bridge.py:172`

- [ ] **Step 1: git mv 三个文件**

```bash
cd /root/chiguo
mkdir -p data
git mv xskb.xlsx data/xskb.xlsx
git mv chiguo_memories.json data/chiguo_memories.json
git mv netease_qr.png data/netease_qr.png
```

- [ ] **Step 2: 改 toml 路径值**

`chiguo_proactive.toml:19`:
```toml
manual_path = "data/chiguo_memories.json"
```
`chiguo_proactive.toml:171`:
```toml
xlsx_path = "data/xskb.xlsx"              # 课表文件，直接替换即可更新
```

- [ ] **Step 3: 改代码默认值(保持默认与 toml 一致)**

`chiguo_state.py:235`:
```python
        xlsx_path = sched.get("xlsx_path", "data/xskb.xlsx")
```
`chiguo_state.py:322`:
```python
        mp = self.config.get("memory", {}).get("manual_path", "data/chiguo_memories.json")
```
`schedule_parser.py:36`:
```python
    def __init__(self, xlsx_path: str = "data/xskb.xlsx",
```
`schedule_parser.py:438`:
```python
    parser = ScheduleParser("data/xskb.xlsx")
```

- [ ] **Step 4: 改 QR 输出路径**

`netease_bridge.py:172`:
```python
    qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "netease_qr.png")
```

- [ ] **Step 5: 全量回归(18 个既有测试)**

```bash
cd /root/chiguo && uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && \
uv run python test_integration.py && uv run python test_monitor.py && \
uv run python test_eventbus.py && uv run python test_personality.py && \
uv run python test_bayesian.py && uv run python test_composer.py && \
uv run python test_ebbinghaus.py && uv run python test_longing.py && \
uv run python test_escape_valve.py && uv run python test_feedback.py && \
uv run python test_trigger.py && uv run python test_topics.py && \
uv run python test_circadian.py && uv run python test_followup.py && \
uv run python test_netease_proof.py && uv run python test_netease_service.py && \
echo "=== PASS ==="
```
Expected: 全部通过,`=== PASS ===`。再跑单次决策确认课表/记忆路径解析:
```bash
uv run python chiguo_daemon.py 2>/dev/null | head -c 120
```
Expected: 输出 JSON 决策(无异常)。

- [ ] **Step 6: Commit**

```bash
git add data/ chiguo_proactive.toml chiguo_state.py schedule_parser.py netease_bridge.py
git commit -m "v10.1: 数据/资源文件移入 data/ 子目录,路径与默认值同步"
```

---

### Task 2: chiguo_envcheck.py 主体

**Files:**
- Create: `chiguo_envcheck.py`

- [ ] **Step 1: 写模块主体**

新建 `chiguo_envcheck.py`,内容:

```python
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


def check_openclaw(home: Path = None) -> dict:
    """OpenClaw 本体 + skill 目录。本体缺失 → critical;skill 文件缺 → warn。"""
    home = home or Path.home()
    oc = home / ".openclaw"
    if not oc.is_dir():
        return {"name": "openclaw", "ok": False, "severity": "critical",
                "detail": f"{oc} 不存在 → OpenClaw 未安装(消息生成/发送端缺失)"}
    skill = oc / "workspace" / "skills" / "chiguo"
    missing = [n for n in ("SUN2.md", "SKILL.md") if not (skill / n).is_file()]
    if missing:
        return {"name": "openclaw", "ok": False, "severity": "warn",
                "detail": f"{skill} 缺少 {', '.join(missing)} → 人格设定缺失,OpenClaw 仍可发消息"}
    return {"name": "openclaw", "ok": True, "severity": "ok",
            "detail": f"OpenClaw OK ({skill} 含 SUN2.md/SKILL.md)"}


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
    try:
        req = urllib.request.Request(f"{api_base}/login/status",
                                     headers={"User-Agent": "chiguo-envcheck"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
        issues.append(f"API OK({status})") if status == 200 else issues.append(f"API HTTP {status}")
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
    ok = cookie_path.is_file()
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
    oc_cfg = cfg.get("openclaw", {})
    mem_cfg = cfg.get("memory", {})
    sched_cfg = cfg.get("schedule", {})
    lancedb_path = _cfg_path(cfg, "memory", "lancedb_path",
                             "~/.openclaw/memory/lancedb-pro", base)
    personality = _cfg_path(cfg, "openclaw", "personality_source",
                            "~/.openclaw/workspace/skills/chiguo", base)
    xlsx = _cfg_path(cfg, "schedule", "xlsx_path", "data/xskb.xlsx", base)
    mem = _cfg_path(cfg, "memory", "manual_path", "data/chiguo_memories.json", base)
    api_base = os.environ.get("NETEASE_API_BASE", "http://localhost:3000")
    checks = [
        check_env(),
        check_openclaw(home),
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
```

- [ ] **Step 2: 手动冒烟**

```bash
cd /root/chiguo && uv run python chiguo_envcheck.py; echo "exit=$?"
```
Expected: JSON 输出 5 组检查,`exit=2`(本机无 ~/.openclaw → openclaw critical)。
再验证退出码映射:
```bash
uv run python -c "import sys; sys.path.insert(0,'.'); import chiguo_envcheck; print(chiguo_envcheck.exit_code({'summary':{'ok':5,'warn':0,'critical':0}}), chiguo_envcheck.exit_code({'summary':{'ok':4,'warn':1,'critical':0}}), chiguo_envcheck.exit_code({'summary':{'ok':3,'warn':1,'critical':1}}))"
```
Expected: `0 1 2`

- [ ] **Step 3: Commit**

```bash
git add chiguo_envcheck.py
git commit -m "v10.1: 新增 chiguo_envcheck.py 环境就绪检查(5 组 + JSON + 退出码 0/1/2)"
```

---

### Task 3: test_envcheck.py

**Files:**
- Create: `test_envcheck.py`

- [ ] **Step 1: 写测试 runner**

新建 `test_envcheck.py`(独立 runner 风格,与其他 test_*.py 一致:`sys.path.insert` + 模块级测试函数 + 末尾依次调用):

```python
#!/usr/bin/env python3
"""test_envcheck.py — chiguo_envcheck 环境检查单元测试(v10.1)"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chiguo_envcheck as ec


def _mk(tmp: Path, **files) -> Path:
    """在临时目录下创建文件树,返回根目录。"""
    for rel, content in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp


def test_check_env():
    r = ec.check_env()
    assert r["name"] == "env"
    assert r["severity"] in ("ok", "critical")
    print("  OK test_check_env")


def test_check_openclaw_missing_dir_critical():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_openclaw(home=Path(td))
        assert r["severity"] == "critical" and not r["ok"]
        assert "不存在" in r["detail"]
    print("  OK test_check_openclaw_missing_dir_critical")


def test_check_openclaw_skill_missing_warn():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), ".openclaw/workspace/skills/chiguo/empty.txt": "x")
        r = ec.check_openclaw(home=Path(td))
        assert r["severity"] == "warn" and not r["ok"]
        assert "SUN2.md" in r["detail"] and "SKILL.md" in r["detail"]
    print("  OK test_check_openclaw_skill_missing_warn")


def test_check_openclaw_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), ".openclaw/workspace/skills/chiguo/SUN2.md": "s",
                       ".openclaw/workspace/skills/chiguo/SKILL.md": "k")
        r = ec.check_openclaw(home=Path(td))
        assert r["severity"] == "ok" and r["ok"]
    print("  OK test_check_openclaw_ok")


def test_check_lancedb_missing_db_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_lancedb(db_path=Path(td) / "no_such_lancedb")
        # lancedb 未装或路径不存在 → 均为 warn,不崩
        assert r["severity"] == "warn" and not r["ok"]
    print("  OK test_check_lancedb_missing_db_warn")


def test_check_netease_no_cookie_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_netease("http://127.0.0.1:1/", Path(td) / "netease_cookie.txt",
                             Path(td) / "netease_health.json")
        assert r["severity"] == "warn" and not r["ok"]
        assert "login" in r["detail"]
    print("  OK test_check_netease_no_cookie_warn")


def test_check_data_missing_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_data(Path(td) / "xskb.xlsx", Path(td) / "mem.json")
        assert r["severity"] == "warn" and not r["ok"]
    print("  OK test_check_data_missing_warn")


def test_check_data_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), "xskb.xlsx": "x", "mem.json": "{}")
        r = ec.check_data(Path(td) / "xskb.xlsx", Path(td) / "mem.json")
        assert r["severity"] == "ok" and r["ok"]
    print("  OK test_check_data_ok")


def test_exit_code_mapping():
    assert ec.exit_code({"summary": {"ok": 5, "warn": 0, "critical": 0}}) == 0
    assert ec.exit_code({"summary": {"ok": 4, "warn": 1, "critical": 0}}) == 1
    assert ec.exit_code({"summary": {"ok": 3, "warn": 1, "critical": 1}}) == 2
    print("  OK test_exit_code_mapping")


def test_run_checks_never_crashes():
    """run_checks 在任何环境下都不抛异常(注入临时 home,真实 toml 副本)。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = td / "chiguo_proactive.toml"
        cfg.write_text(Path("chiguo_proactive.toml").read_text())
        import re
        cfg.write_text(re.sub(r"(?m)^lancedb_path\s*=.*$",
                              f'lancedb_path = "{td / "no_lancedb"}"',
                              cfg.read_text()))
        report = ec.run_checks(home=td, base_dir=td)
        assert len(report["checks"]) == 5
        assert report["summary"]["ok"] + report["summary"]["warn"] + report["summary"]["critical"] == 5
        # netease 检查会尝试连 localhost:3000 —— 只要求不崩(超时 5s 内失败 → warn)
        json.dumps(report)
    print("  OK test_run_checks_never_crashes")


if __name__ == "__main__":
    test_check_env()
    test_check_openclaw_missing_dir_critical()
    test_check_openclaw_skill_missing_warn()
    test_check_openclaw_ok()
    test_check_lancedb_missing_db_warn()
    test_check_netease_no_cookie_warn()
    test_check_data_missing_warn()
    test_check_data_ok()
    test_exit_code_mapping()
    test_run_checks_never_crashes()
    print(f"test_envcheck.py: ALL {10} TESTS PASSED")
```

注意:`test_check_openclaw_skill_missing_warn` 里 `_mk(Path(td), ".openclaw/workspace/skills/chiguo/empty.txt": "x")` 语法 → 应写为:
```python
_mk(Path(td), {".openclaw/workspace/skills/chiguo/empty.txt": "x"})
```
`_mk` 签名是 `_mk(tmp: Path, **files)`——改用 dict 传参更清晰。实现时统一写成 `_mk(Path(td), {".openclaw/...": "x"})` 形式,`_mk` 改为接受 `dict`:
```python
def _mk(tmp: Path, files: dict) -> Path:
    for rel, content in files.items():
        ...
```
(修改 Step 1 中 `_mk` 定义为 `def _mk(tmp: Path, files: dict) -> Path`,所有调用点用 `_mk(Path(td), {"rel": "content"})`。)

- [ ] **Step 2: 跑测试,确认全过**

```bash
cd /root/chiguo && uv run python test_envcheck.py
```
Expected: `test_envcheck.py: ALL 10 TESTS PASSED`,退出码 0。

- [ ] **Step 3: Commit**

```bash
git add test_envcheck.py
git commit -m "v10.1: test_envcheck.py 环境检查测试(10 用例)"
```

---

### Task 4: deploy.sh 集成

**Files:**
- Modify: `deploy.sh`(第 4 节 OpenClaw 检查 + 第 5 节网易云检查 → envcheck 调用)

- [ ] **Step 1: 替换散装检查**

`deploy.sh` 中删除第 4 节「OpenClaw 集成检查」与第 5 节「网易云检查」的整块 `if [ ... ]` 逻辑(保留 say/warn 函数),替换为:

```bash
# ── 4. 环境就绪检查(OpenClaw/依赖/数据文件,chiguo_envcheck.py) ──
say "运行环境检查 ..."
set +e
uv run python chiguo_envcheck.py
EC=$?
set -e
case $EC in
    0) say "环境就绪 ✓" ;;
    1) warn "环境存在警告(见上方 JSON,系统可运行但部分降级)" ;;
    2) fail "环境存在严重问题(见上方 JSON),请先修复再继续" ;;
esac
```

注意:`fail` 已定义为退出;`--stats` 等说明保留在第 4 节后。

- [ ] **Step 2: 验证 deploy.sh 仍能跑通**

```bash
cd /root/chiguo && bash -n deploy.sh && bash deploy.sh 2>&1 | grep -E "环境就绪|环境存在|全部测试" | head -3
```
Expected: 出现「环境存在严重问题」(本机无 ~/.openclaw,envcheck critical → fail)或「环境就绪」(若已配置)。无论哪种,脚本行为符合预期(有 critical 就停,与 set -e 语义一致)。

- [ ] **Step 3: Commit**

```bash
git add deploy.sh
git commit -m "v10.1: deploy.sh 集成 chiguo_envcheck(替代散装 OpenClaw/网易云检查)"
```

---

### Task 5: 文档同步 + 全量回归

**Files:**
- Modify: `MEMORY.md`(新条目)
- Modify: `doc/IMPROVE.md`(新条目)
- Modify: `doc/SYSTEM.md`(路径说明 + envcheck 模块表)
- Modify: `README.md` + `doc/README.md`(数据目录说明 + envcheck CLI)
- Modify: `AGENTS.md`(测试清单 18→19;test_envcheck 加入链)

- [ ] **Step 1: 更新 AGENTS.md 测试清单**

`AGENTS.md` 中 18 文件测试链追加:
```bash
uv run python test_circadian.py && uv run python test_followup.py && \
uv run python test_netease_proof.py && uv run python test_netease_service.py && \
uv run python test_envcheck.py   # full suite (19 files)
```
并将「18 文件」字样统一改 19。

- [ ] **Step 2: 更新文档**

- `README.md`:文件结构章节加 `data/`(课表/手动记忆/二维码);CLI 参考加 `uv run python chiguo_envcheck.py`(环境就绪检查,退出码 0/1/2)
- `doc/README.md`:同上简述
- `doc/SYSTEM.md`:模块表加 `chiguo_envcheck.py`;路径说明章节补 `data/` 前缀说明(课表/手动记忆/二维码均位于 data/,相对路径经 `_anchored` 解析)
- `MEMORY.md` 新条目(v10.1:日期、文件、改动描述、验证)——格式参考既有条目(中文,`## YYYY-MM-DD — 标题` 置顶)
- `doc/IMPROVE.md` 新条目(同格式,含验证:19 测试全过)

- [ ] **Step 3: 全量回归(19 个测试)**

```bash
cd /root/chiguo && uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && \
uv run python test_integration.py && uv run python test_monitor.py && \
uv run python test_eventbus.py && uv run python test_personality.py && \
uv run python test_bayesian.py && uv run python test_composer.py && \
uv run python test_ebbinghaus.py && uv run python test_longing.py && \
uv run python test_escape_valve.py && uv run python test_feedback.py && \
uv run python test_trigger.py && uv run python test_topics.py && \
uv run python test_circadian.py && uv run python test_followup.py && \
uv run python test_netease_proof.py && uv run python test_netease_service.py && \
uv run python test_envcheck.py && echo "=== FULL SUITE PASS ==="
```
Expected: `=== FULL SUITE PASS ===`。

- [ ] **Step 4: 收尾检查**

```bash
cd /root/chiguo && git status --short
uv run python chiguo_envcheck.py >/dev/null; echo "envcheck exit=$?"   # 2(无 ~/.openclaw)或 0/1
uv run python chiguo_daemon.py 2>/dev/null | head -c 100               # 决策 JSON 正常
```
Expected: 无未跟踪杂项(除 .venv 等 gitignore 项);daemon 输出正常 JSON。

- [ ] **Step 5: Commit + 自审计**

```bash
git add -A
git commit -m "v10.1: 文档同步(19 测试链/MEMORY/IMPROVE/SYSTEM/README)"
```
然后按 AGENTS.md 要求派子代理自审计全部 v10.1 改动,修复发现的问题,最后 `git push`(需用户确认)。

---

## Self-Review 结果

- **Spec coverage**: 设计 A(§3.1-3.2)→ Task 1;设计 B(§4 CLI/5 组检查/输出 schema/退出码/只读)→ Task 2+3;deploy.sh 集成(§5)→ Task 4;文档(§6 职责分工提示 + 验证 §7)→ Task 5。全部覆盖。
- **Placeholder scan**: 无 TBD/TODO;每步含完整代码/命令。Task 3 中 `_mk` 签名问题已在步骤内显式纠正(⚠️ 以纠正后版本为准)。
- **Type consistency**: `check_env/check_openclaw/check_lancedb/check_netease/check_data` 均返回 `{"name","ok","severity","detail"}`;`run_checks` 返回 `{"checks","summary","ready"}`;`exit_code(report)->int`;测试与实现签名一致(注入参数 `home=`/`db_path=`/`base_dir=`)。
