"""test_docs_sync.py — 文档-脚本一致性测试（独立 runner，防漂移）
用法: uv run python tests/test_docs_sync.py（非零退出=有漂移）
覆盖: deploy.sh 测试数组↔文档计数 / --skip-* flags / 落点锚点 / 硬编码路径 / toml 键 / README 中英对齐"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAIL = []

def check(name, cond, detail=""):
    if cond:
        print(f"  ok - {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL - {name} {detail}")

# a) 测试链单一入口 + 磁盘↔链双向集合校验（U3/#228：去魔数，双向比对）
#    deploy.sh 复用 ci-test.sh（不维护内联列表）；ci-test.sh 实际引用集合 == tests/ 磁盘 test_* 集合
deploy = (ROOT / "deploy.sh").read_text()
citesh = (ROOT / "scripts" / "ci-test.sh").read_text()
check("deploy.sh 调用 ci-test.sh（单一测试入口）", "ci-test.sh" in deploy)
check("deploy.sh 无内联 TESTS 数组（防重复维护漂移）", "TESTS=(" not in deploy)
check("ci-test.sh 含 test_docs_sync", "test_docs_sync.py" in citesh)

# 磁盘测试集合（仅 test_ 开头；fixtures 如 _loop_worker.py / fake-agent-rpc.mjs / __pycache__ 天然排除）
disk = {p.name for p in ROOT.glob("tests/test_*.py")} \
     | {p.name for p in ROOT.glob("tests/test_*.mjs")} \
     | {p.name for p in ROOT.glob("tests/test_*.sh")}
# ci-test.sh 引用集合（双向往返：磁盘有链无 → 未入链静默不跑；链有磁盘无 → 悬空引用）
chain = {m for pat in (r"test_[a-z_0-9]+\.py", r"test_[a-z_0-9]+\.mjs", r"test_[a-z_0-9]+\.sh") for m in re.findall(pat, citesh)}
disk_only = sorted(disk - chain)
chain_only = sorted(chain - disk)
check("U3 磁盘测试集合 == ci-test.sh 引用集合（双向一致）", disk == chain,
      f"未入链(磁盘有链无): {disk_only} / 悬空(链有磁盘无): {chain_only}")
check("U3 无新用例遗漏入链（磁盘有链无）", not disk_only,
      f"以下测试文件未入链，需在 ci-test.sh 追加: {disk_only}")
check("U3 ci-test.sh 无悬空引用（链有磁盘无）", not chain_only,
      f"以下链引用无对应磁盘文件，需删除或恢复: {chain_only}")

# 文档计数引用化（U3/#228）：各文档不得硬编码「N py + M script」式计数，必须引用 scripts/ci-test.sh 为准
# 覆盖 59 py + 14 script / 59 个 Python + 13 个脚本(漂移旧值) / 59 个 py + 8 个 mjs(旧值) 等漏检句式
MAGIC = re.compile(
    r"\d+ *py *\+ *\d+ *script"                   # 59 py + 14 script
    r"|\d+ *个 *Python *\+ *\d+ *个? *脚本"         # 59 个 Python + 13 个脚本测试（漂移）
    r"|\d+ *个 *py *\+ *\d+ *个? *mjs"             # 59 个 py + 8 个 mjs（旧值）
    r"|\d+ *py *\+ *\d+ *个? *mjs")
for f in ["AGENTS.md", "CLAUDE.md", "README.md", "README_EN.md",
          "doc/DEPLOYMENT.md", "doc/SYSTEM.md"]:
    txt = (ROOT / f).read_text()
    check(f"{f} 测试计数引用化（以 ci-test.sh 为准，无硬编码计数）",
          "ci-test.sh" in txt and not MAGIC.search(txt),
          f"引用/魔数: 缺引用={"ci-test.sh" not in txt} 命中={MAGIC.findall(txt)}")

# U8d: 文档行数引用化（防再漂移）——代码文件行数不再硬编码，以 wc -l 为准
# 覆盖句式: “(1984 行)” / “；621 行” / “470 行 / 22 段” / “| 文件 | 行数 | 职责 |” 及英文 “(1984 lines)” / “470 lines / 22 sections”
LINES_DOCS = ["README.md", "README_EN.md", "AGENTS.md", "doc/SYSTEM.md"]
LINES_MAGIC = re.compile(
    r"[（(]\d{3,4} 行[)）]"
    r"|；\s*\d{3,4} 行"
    r"|\d{3,4} 行 /\s*\d+ 段"
    r"|[(（]\d{3,4} lines[)]"
    r"|;\s*\d{3,4} lines"
    r"|\d{3,4} lines /\s*\d+ sections"
    r"|\| 文件 \| 行数 \|")
for f in LINES_DOCS:
    txt = (ROOT / f).read_text()
    check(f"{f} 代码文件行数引用化（以 wc -l 为准，无硬编码行数）",
          not LINES_MAGIC.search(txt),
          f"命中={LINES_MAGIC.findall(txt)}")

# b) --skip-* flags：deploy.sh 与 DEPLOYMENT.md 一致
flags = ["--skip-agent", "--skip-bridge", "--skip-netease"]
check("deploy.sh 支持全部 --skip-*", all(f in deploy for f in flags))
dep_file = ROOT / "doc" / "DEPLOYMENT.md"
dep = dep_file.read_text() if dep_file.exists() else ""
check("DEPLOYMENT.md 存在", bool(dep))
check("DEPLOYMENT.md 引用全部 --skip-*", all(f in dep for f in flags))

# c) 落点锚点：DEPLOYMENT.md 与脚本常量一致（$HOME 归一化为 ~）
anchors = [
    ("wechatbot", "scripts/wechat-bridge.sh"),
    ("/opt/netease-api", "scripts/netease-api.sh"),
    ("data/mem0", "chiguo_proactive.toml"),
    (".chiguo/auth", "scripts/wechat-bridge.sh"),
]
for anchor, script in anchors:
    script_txt = (ROOT / script).read_text().replace("$HOME", "~")
    check(f"DEPLOYMENT.md 含落点 {anchor}", anchor in dep)
    check(f"{script} 与锚点 {anchor} 一致", anchor in script_txt)

# d) 无 /root/chiguo 硬编码残留（全仓库脚本链 + 测试；注释剔除后扫描）
hits = []
for pattern in ["scripts/*.mjs", "scripts/*.sh", "wechat-bridge/*.mjs", "tests/*.mjs", "tests/*.sh"]:
    for p in ROOT.glob(pattern):
        txt = p.read_text()
        txt = re.sub(r"//.*$", "", txt, flags=re.M)
        txt = re.sub(r"#.*$", "", txt, flags=re.M)
        if "/root/chiguo" in txt:
            hits.append(str(p.relative_to(ROOT)))
check("脚本链无 /root/chiguo 硬编码", not hits, f"命中: {hits}")

# e) toml 无 personality_dir
check("toml 无 personality_dir", "personality_dir" not in (ROOT / "chiguo_proactive.toml").read_text())

# f) README 中英 section 标题集合一致（经 EN_TITLES 翻译映射；映射即配对文档，改名时同步更新）
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
def headings(p):
    return set(re.findall(r"^## (.+)$", p.read_text(), re.M))
zh = {EN_TITLES.get(h, h) for h in headings(ROOT / "README.md")}
en = headings(ROOT / "README_EN.md")
check("README 中英 section 标题集合一致（EN_TITLES 映射）", zh == en, f"仅中文: {zh - en} 仅英文: {en - zh}")

# g) AGENTS.md 引用 DEPLOYMENT.md
check("AGENTS.md 引用 DEPLOYMENT.md", "DEPLOYMENT.md" in (ROOT / "AGENTS.md").read_text())

if FAIL:
    print(f"\n{len(FAIL)} 项失败: {FAIL}")
    sys.exit(1)
print("\n全部通过")
