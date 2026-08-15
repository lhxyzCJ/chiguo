"""test_docs_sync.py — 文档-脚本一致性 pytest 测试（Q26 迁移）

覆盖: ci-test.sh pytest 入口与磁盘集合 / 文档计数引用化（防硬编码计数）/
--skip-* flags / 落点锚点 / 硬编码路径 / toml 键 / README 中英对齐"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def check(name, cond, detail=""):
    """断言式 check：失败即抛 AssertionError（pytest 感知为失败）。"""
    if not cond:
        raise AssertionError(f"{name} {detail}")


# ── a) 测试链单一入口 + pytest 驱动 + 磁盘↔收集集合校验（Q26 迁移）────────
def test_ci_test_single_entry_and_pytest_driver():
    """deploy.sh 单一测试入口；ci-test.sh 以 pytest 驱动 py 测试，不再逐文件切片。"""
    deploy = (ROOT / "deploy.sh").read_text()
    citesh = (ROOT / "scripts" / "ci-test.sh").read_text()
    check("deploy.sh 调用 ci-test.sh（单一测试入口）", "ci-test.sh" in deploy)
    check("deploy.sh 无内联 TESTS 数组（防重复维护漂移）", "TESTS=(" not in deploy)
    check("ci-test.sh 用 pytest 驱动 py 测试", "pytest tests/" in citesh)
    # 去逐文件 runner：不再出现 uv run python tests/test_X.py 式单文件切片
    legacy = re.findall(r"uv run python tests/test_[a-z_0-9]+\.py", citesh)
    check("ci-test.sh 无逐文件 py runner（Q26 已全量 pytest 化）", not legacy,
          f"残留逐文件调用: {legacy}")


def test_pytest_collects_all_disk_py():
    """磁盘 tests/test_*.py 集合 == pytest 收集集合（无遗落、无收集错误，Q26 验收）。"""
    disk = {p.name for p in ROOT.glob("tests/test_*.py")}
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        capture_output=True, text=True, cwd=ROOT)
    out = res.stdout + res.stderr
    collected = {m.split("/")[-1] for m in re.findall(r"tests/test_[a-z_0-9]+\.py", out)}
    check("pytest --collect-only 无收集错误（退出非零即失败）", res.returncode == 0,
          f"exit={res.returncode} stderr_tail={res.stderr[-400:]!r}")
    disk_only = sorted(disk - collected)
    chain_only = sorted(collected - disk)
    check("磁盘 test_*.py 全部被 pytest 收集（无遗落）", not disk_only,
          f"未收集: {disk_only}")
    check("pytest 收集无磁盘外悬空模块", not chain_only,
          f"链路外: {chain_only}")


def test_script_chain_references_all_scripts():
    """mjs/sh 脚本测试仍依 ci-test.sh 引用链逐一保留（Q26 保留非 py 链）。"""
    citesh = (ROOT / "scripts" / "ci-test.sh").read_text()
    disk = {p.name for p in ROOT.glob("tests/test_*.mjs")} | \
           {p.name for p in ROOT.glob("tests/test_*.sh")}
    chain = {m for pat in (r"test_[a-z_0-9]+\.mjs", r"test_[a-z_0-9]+\.sh")
             for m in re.findall(pat, citesh)}
    check("磁盘 mjs/sh 脚本测试集合 == ci-test.sh 引用链（双向一致）", disk == chain,
          f"未入链: {sorted(disk - chain)} / 悬空: {sorted(chain - disk)}")


# ── 文档计数引用化（U3/#228）：不得硬编码「N py + M script」式计数 ─────────
MAGIC = re.compile(
    r"\d+ *py *\+ *\d+ *script"                   # e.g. 59 py + 14 script
    r"|\d+ *个 *Python *\+ *\d+ *个? *脚本"         # e.g. 59 个 Python + 13 个脚本测试（漂移）
    r"|\d+ *个 *py *\+ *\d+ *个? *mjs"             # e.g. 59 个 py + 8 个 mjs（旧值）
    r"|\d+ *py *\+ *\d+ *个? *mjs")

def test_docs_no_hardcoded_test_count():
    """各文档测试计数引用化（以 scripts/ci-test.sh 为准，无硬编码 N py + M script）。"""
    for f in ["AGENTS.md", "CLAUDE.md", "README.md", "README_EN.md",
              "doc/DEPLOYMENT.md", "doc/SYSTEM.md"]:
        txt = (ROOT / f).read_text()
        check(f"{f} 测试计数引用化（以 ci-test.sh 为准，无硬编码计数）",
              "ci-test.sh" in txt and not MAGIC.search(txt),
              f"缺引用={"ci-test.sh" not in txt} 命中={MAGIC.findall(txt)}")


def test_runner_scripts_no_hardcoded_count():
    """Q12/#262：runner（ci-test.sh 自身 / deploy.sh）不得硬编码「N py + N script」魔数。

    测试计数魔数改磁盘动态计算——否则文档引用它时又把魔数固化回来，与磁盘实际数目脱钩。
    """
    for runner in ["scripts/ci-test.sh", "deploy.sh"]:
        runner_txt = (ROOT / runner).read_text()
        check(f"{runner} 测试计数不硬编码（动态扫描磁盘，无「N py + N script」魔数）",
              not MAGIC.search(runner_txt),
              f"命中={MAGIC.findall(runner_txt)}")


# ── U8d：代码文件行数引用化（防再漂移）───────────────────────────────
LINES_DOCS = ["README.md", "README_EN.md", "AGENTS.md", "doc/SYSTEM.md"]
LINES_MAGIC = re.compile(
    r"[（(]\d{3,4} 行[)）]"
    r"|；\s*\d{3,4} 行"
    r"|\d{3,4} 行 /\s*\d+ 段"
    r"|[(（]\d{3,4} lines[)]"
    r"|;\s*\d{3,4} lines"
    r"|\d{3,4} lines /\s*\d+ sections"
    r"|\| 文件 \| 行数 \|")

def test_daemon_cli_manifest_guard():
    """Q12/#262：daemon CLI 参数清单守卫——从 argparse 动态提取，与文档全量清单双向比对。

    守护「新增 CLI 参数未文档化」：daemon 每新增 add_argument，若未同步写入全量清单文档即可失败；
    同理文档清单里若出现 daemon 本不存在的参数（悬空/过期）也失败。
    T10 daemon 拆分：argparse 定义从 chiguo_daemon.py 迁至 cli/parser.py，守卫随之指向该文件。
    """
    def _doc_cli_manifest(txt, anchor_pat):
        """anchor_pat 须捕获 \1=声明参数数、\2=反引号清单段；返回 (declared, set|None)。"""
        m = re.search(anchor_pat, txt, re.S)
        if not m:
            return None, None
        return int(m.group(1)), set(re.findall(r"--[a-z][a-z0-9-]*", m.group(2)))

    daemon_src = (ROOT / "cli" / "parser.py").read_text()
    daemon_args = set(re.findall(r'parser\.add_argument\("(--[a-z][a-z0-9-]*)"', daemon_src))
    _CLI_MANIFEST_DOCS = {
        "CLAUDE_CODE_RULES.md": r'### daemon CLI（(\d+) 个参数）\n(`[^`]*`)',
        "doc/AGENT_INTEGRATION.md": r'daemon CLI 共 (\d+) 个参数（(`[^`]*`)',
    }
    for cli_doc, cli_pat in _CLI_MANIFEST_DOCS.items():
        cli_txt = (ROOT / cli_doc).read_text()
        declared, doc_args = _doc_cli_manifest(cli_txt, cli_pat)
        check(f"{cli_doc} daemon CLI 清单锚点存在", declared is not None)
        if declared is None:
            continue
        check(f"{cli_doc} daemon CLI 计数 == argparse 参数数",
              declared == len(daemon_args), f"声明 {declared} vs 实际 {len(daemon_args)}")
        check(f"{cli_doc} daemon CLI 清单 == argparse（双向一致，新增参数须补文档）",
              doc_args == daemon_args,
              f"文档缺(新增未文档化): {sorted(daemon_args - doc_args)} / 文档多(悬空): {sorted(doc_args - daemon_args)}")

def test_docs_no_hardcoded_line_count():
    """代码文件行数引用化（以 wc -l 为准，无硬编码行数）。"""
    for f in LINES_DOCS:
        txt = (ROOT / f).read_text()
        check(f"{f} 代码文件行数引用化（以 wc -l 为准，无硬编码行数）",
              not LINES_MAGIC.search(txt),
              f"命中={LINES_MAGIC.findall(txt)}")


# ── b) --skip-* flags：deploy.sh 与 DEPLOYMENT.md 一致 ────────────────
FLAGS = ["--skip-agent", "--skip-bridge", "--skip-netease"]


def test_deploy_skip_flags():
    """deploy.sh 支持全部 --skip-*；DEPLOYMENT.md 引用一致。"""
    deploy = (ROOT / "deploy.sh").read_text()
    dep_file = ROOT / "doc" / "DEPLOYMENT.md"
    dep = dep_file.read_text() if dep_file.exists() else ""
    check("deploy.sh 支持全部 --skip-*", all(f in deploy for f in FLAGS))
    check("DEPLOYMENT.md 存在", bool(dep))
    check("DEPLOYMENT.md 引用全部 --skip-*", all(f in dep for f in FLAGS))


# ── c) 落点锚点：DEPLOYMENT.md 与脚本常量一致（$HOME 归一化为 ~）─────────
ANCHORS = [
    ("wechatbot", "scripts/wechat-bridge.sh"),
    ("/opt/netease-api", "scripts/netease-api.sh"),
    ("data/mem0", "chiguo_proactive.toml"),
    (".chiguo/auth", "scripts/wechat-bridge.sh"),
]


def test_deployment_anchors():
    """落点锚点：DEPLOYMENT.md 与脚本常量一致。"""
    dep = (ROOT / "doc" / "DEPLOYMENT.md").read_text()
    for anchor, script in ANCHORS:
        script_txt = (ROOT / script).read_text().replace("$HOME", "~")
        check(f"DEPLOYMENT.md 含落点 {anchor}", anchor in dep)
        check(f"{script} 与锚点 {anchor} 一致", anchor in script_txt)


# ── d) 无 /root/chiguo 硬编码残留（注释剔除后扫描）────────────────────
def test_no_root_chiguo_hardcode():
    """脚本链无 /root/chiguo 硬编码。"""
    hits = []
    for pattern in ["scripts/*.mjs", "scripts/*.sh", "wechat-bridge/*.mjs",
                    "tests/*.mjs", "tests/*.sh"]:
        for p in ROOT.glob(pattern):
            txt = p.read_text()
            txt = re.sub(r"//.*$", "", txt, flags=re.M)
            txt = re.sub(r"#.*$", "", txt, flags=re.M)
            if "/root/chiguo" in txt:
                hits.append(str(p.relative_to(ROOT)))
    check("脚本链无 /root/chiguo 硬编码", not hits, f"命中: {hits}")


# ── e) toml 无 personality_dir ─────────────────────────────────────
def test_toml_no_personality_dir():
    """主 toml 无 personality_dir 残留键。"""
    check("toml 无 personality_dir",
          "personality_dir" not in (ROOT / "chiguo_proactive.toml").read_text())


# ── f) README 中英 section 标题集合一致 ──────────────────────────────
EN_TITLES = {
    "目录": "Table of Contents",
    "🎀 她是谁": "🎀 Who Is She",
    "🧭 这是什么": "🧭 What This Is",
    "✨ 特性一览": "✨ Features",
    "🏗 架构": "🏗 Architecture",
    "💬 效果示例": "💬 Example Outputs",
    "🚀 快速开始": "🚀 Quick Start",
    "🧩 组件": "🧩 Components",
    "🧠 接入模型后端": "🧠 Bring Your Own Model",
    "🎭 人格设定": "🎭 Persona (Fixed)",
    "🛠 部署与运维": "🛠 Deploy & Ops",
    "📖 文档与贡献": "📖 Docs & Contributing",
    "❓ FAQ": "❓ FAQ",
    "📄 License": "📄 License",
    "📁 文件结构": "📁 Project Layout",
}


def _headings(p: pathlib.Path):
    return set(re.findall(r"^## (.+)$", p.read_text(), re.M))


def test_readme_en_zh_section_parity():
    """README 中英 section 标题集合一致（EN_TITLES 映射；改名时同步更新）。"""
    zh = {EN_TITLES.get(h, h) for h in _headings(ROOT / "README.md")}
    en = _headings(ROOT / "README_EN.md")
    check("README 中英 section 标题集合一致（EN_TITLES 映射）", zh == en,
          f"仅中文: {zh - en} 仅英文: {en - zh}")


# ── g) AGENTS.md 引用 DEPLOYMENT.md ────────────────────────────────
def test_agents_documents_reference_deployment():
    """AGENTS.md 引用 DEPLOYMENT.md。"""
    check("AGENTS.md 引用 DEPLOYMENT.md",
          "DEPLOYMENT.md" in (ROOT / "AGENTS.md").read_text())


def test_docs_sync_self_is_collected():
    """本测试自身具备 test_* 函数（Q26 迁移后不再有模块级 sys.exit runner）。"""
    assert True
