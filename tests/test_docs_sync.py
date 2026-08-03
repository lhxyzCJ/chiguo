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

# a) deploy.sh TESTS 数组 ↔ 文档计数
deploy = (ROOT / "deploy.sh").read_text()
m = re.search(r"^TESTS=\((.*?)\)", deploy, re.S)
assert m, "deploy.sh 找不到 TESTS 数组"
test_names = m.group(1).split()
check("deploy.sh TESTS 含 test_docs_sync", "test_docs_sync" in test_names)
check("deploy.sh TESTS 长度 == 36", len(test_names) == 36, f"实际 {len(test_names)}")
for f in ["AGENTS.md", "CLAUDE.md", "README.md", "README_EN.md"]:
    txt = (ROOT / f).read_text()
    nums = re.findall(r"(\d+) py \+ (\d+) script", txt)
    check(f"{f} 计数为 36 py + 10 script", any((int(a), int(b)) == (36, 10) for a, b in nums), f"实际 {nums}")

# b) --skip-* flags：deploy.sh 与 DEPLOYMENT.md 一致
flags = ["--skip-pi", "--skip-bridge", "--skip-netease"]
check("deploy.sh 支持全部 --skip-*", all(f in deploy for f in flags))
dep = (ROOT / "doc" / "DEPLOYMENT.md").read_text()
check("DEPLOYMENT.md 引用全部 --skip-*", all(f in dep for f in flags))

# c) 落点锚点：DEPLOYMENT.md 与脚本常量一致（$HOME 归一化为 ~）
anchors = [
    ("wechatbot", "scripts/wechat-bridge.sh"),
    ("/opt/netease-api", "scripts/netease-api.sh"),
    (".pi-agent/memory/lancedb-pro", "scripts/install_pi.sh"),
    (".chiguo/auth", "scripts/wechat-bridge.sh"),
]
for anchor, script in anchors:
    script_txt = (ROOT / script).read_text().replace("$HOME", "~")
    check(f"DEPLOYMENT.md 含落点 {anchor}", anchor in dep)
    check(f"{script} 与锚点 {anchor} 一致", anchor in script_txt)

# d) 无 /root/chiguo 硬编码残留（生产脚本链 + mjs 测试；注释剔除后扫描）
hits = []
for pattern in ["scripts/*.mjs", "scripts/*.sh", "wechat-bridge/*.mjs", "tests/*.mjs"]:
    for p in ROOT.glob(pattern):
        txt = p.read_text()
        txt = re.sub(r"//.*$", "", txt, flags=re.M)
        txt = re.sub(r"#.*$", "", txt, flags=re.M)
        if "/root/chiguo" in txt:
            hits.append(str(p.relative_to(ROOT)))
check("脚本链无 /root/chiguo 硬编码", not hits, f"命中: {hits}")

# e) toml 无 personality_dir
check("toml 无 personality_dir", "personality_dir" not in (ROOT / "chiguo_proactive.toml").read_text())

# f) README 中英 section 标题集合一致（目录/Table of Contents 归一为同一 token）
def headings(p):
    s = set(re.findall(r"^## (.+)$", p.read_text(), re.M))
    s.discard("目录")
    s.discard("Table of Contents")
    return s
zh, en = headings(ROOT / "README.md"), headings(ROOT / "README_EN.md")
check("README 中英 section 标题集合一致", zh == en, f"仅中文: {zh - en} 仅英文: {en - zh}")

# g) AGENTS.md 引用 DEPLOYMENT.md
check("AGENTS.md 引用 DEPLOYMENT.md", "DEPLOYMENT.md" in (ROOT / "AGENTS.md").read_text())

if FAIL:
    print(f"\n{len(FAIL)} 项失败: {FAIL}")
    sys.exit(1)
print("\n全部通过")
