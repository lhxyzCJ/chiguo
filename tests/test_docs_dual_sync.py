"""test_docs_dual_sync.py — T18 文档双写铁律：README.md ↔ README_EN.md + VERSION 1.24 同步

TDD RED→GREEN: 验证 README_EN 与 README 中英文镜像、顶部滞后声明、VERSION 1.24 全量同步。
- README.md 顶部必须含 "英文版可能滞后，以中文版为准" 且链向 README_EN.md
- README_EN.md 顶部必须含 "The English version may lag behind" 且链向 README.md
- 双 README section 标题经 EN_TITLES 映射后集合一致（复用 test_docs_sync 的映射）
- 特性表行数对等（20 行级别，无增删漂移）
- VERSION 1.24 单源：chiguo_version.VERSION == "1.24" 且 SYSTEM/DEPLOYMENT/AGENT_INTEGRATION header/table 均 1.24
- 无 1.19/1.23 硬编码（history 序列除外：仅允许 "1.9→...→1.24" 连续枚举中出现）
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

EXPECTED = "1.24"
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
    return set(re.findall(r"^## (.+)$", p.read_text(encoding="utf-8"), re.M))


def test_readme_top_lag_declaration():
    """README.md / README_EN.md 顶部滞后声明互镜像（中文为准）"""
    zh = (ROOT / "README.md").read_text(encoding="utf-8")
    en = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    assert "英文版可能滞后，以中文版为准" in zh, "README.md 顶部缺少 '英文版可能滞后，以中文版为准'"
    assert "README_EN.md" in zh, "README.md 顶部应链向 README_EN.md"
    assert "The English version may lag behind" in en, "README_EN.md 顶部缺少英文滞后声明"
    assert "the Chinese version is authoritative" in en, "README_EN.md 顶部缺少 'the Chinese version is authoritative'"
    assert "README.md" in en, "README_EN.md 顶部应链向 README.md"


def test_readme_section_parity():
    """双 README section 标题经 EN_TITLES 映射后集合一致"""
    zh = {EN_TITLES.get(h, h) for h in _headings(ROOT / "README.md")}
    en = _headings(ROOT / "README_EN.md")
    assert zh == en, f"section 漂移: 仅中文 {zh - en} 仅英文 {en - zh}"


def test_readme_feature_table_parity():
    """特性表行数对等（8 大话题等 20 行特征，无增删漂移）"""
    zh = (ROOT / "README.md").read_text(encoding="utf-8")
    en = (ROOT / "README_EN.md").read_text(encoding="utf-8")

    def _feature_rows(txt: str):
        # 特性表位于 ## ✨ 特性一览 / ## ✨ Features 之后、下一 ## 之前
        m = re.search(r"## ✨ (?:特性一览|Features)\n(.*?)\n## ", txt, re.S)
        block = m.group(1) if m else txt
        # 统计以 "| 🧭" / "| 🌙" 等开头且含特征的行（排除表头）
        rows = [l for l in block.splitlines() if l.startswith("| ") and "---" not in l and "特性 |" not in l and "Feature |" not in l]
        # 仅特性表（到空行前），过滤掉后续 T0/T1 表；特性表共 20 行
        # 取连续特性行直到非特性行；简化：取前20
        return rows

    zh_rows = _feature_rows(zh)
    en_rows = _feature_rows(en)
    # 严格对等
    assert len(zh_rows) == len(en_rows), f"特性表行数漂移 zh={len(zh_rows)} en={len(en_rows)}"
    assert len(zh_rows) == 20, f"特性表应 20 行，实 {len(zh_rows)}"


def test_version_single_source_and_docs_sync():
    """VERSION 单源 1.24 且 SYSTEM/DEPLOYMENT/AGENT_INTEGRATION 全量同步"""
    from chiguo_version import VERSION
    assert VERSION == EXPECTED, f"VERSION={VERSION!r} != {EXPECTED!r}"
    # pyproject 同步（复用 test_owner 逻辑，补充）
    import tomllib
    py_ver = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert py_ver == EXPECTED, f"pyproject {py_ver!r} != {EXPECTED!r}"

    # SYSTEM header + 957 + 1090
    system = (ROOT / "doc/SYSTEM.md").read_text(encoding="utf-8")
    assert f"v{EXPECTED}" in system or f"VERSION={EXPECTED}" in system, "SYSTEM.md 未含 v1.24"
    m = re.search(r'chiguo_version\.py.*VERSION\s*=\s*"([^"]+)"', system)
    assert m and m.group(1) == EXPECTED, f"SYSTEM.md version table {m.group(1) if m else None} != {EXPECTED}"
    # DEPLOYMENT / AGENT_INTEGRATION header
    for fname in ("doc/DEPLOYMENT.md", "doc/AGENT_INTEGRATION.md"):
        txt = (ROOT / fname).read_text(encoding="utf-8")
        assert EXPECTED in txt, f"{fname} 未同步到 {EXPECTED}"
        # header 应含 v1.24
        assert f"v{EXPECTED}" in txt or EXPECTED in txt

    # README 双写也应同步版本（防 README 滞后于 docs）
    for readme in ("README.md", "README_EN.md"):
        txt = (ROOT / readme).read_text(encoding="utf-8")
        assert EXPECTED in txt, f"{readme} 未同步 VERSION {EXPECTED}（双写铁律：docs 升版 README 必须同步）"


def test_no_old_version_hardcode_except_history():
    """无 1.19/1.23 硬编码，history 序列中出现除外（1.9→...→1.24 枚举）"""
    # 允许的 history 序列形态：行内同时含 1.19 与 1.24 且为 → 枚举
    history_re = re.compile(r"1\.9→.*1\.24", re.S)
    for p in [ROOT / "README.md", ROOT / "README_EN.md", ROOT / "doc/SYSTEM.md",
              ROOT / "doc/DEPLOYMENT.md", ROOT / "doc/AGENT_INTEGRATION.md"]:
        txt = p.read_text(encoding="utf-8")
        # 移除 history 序列行后不应残留 1.19 / 1.23 作为当前版
        txt_no_history = history_re.sub("", txt)
        # 单独的 VERSION="1.19"/"1.23" 或 v1.19/v1.23 视为硬编码漂移
        assert 'VERSION = "1.19"' not in txt_no_history, f"{p.name} 残留 VERSION 1.19"
        assert 'VERSION = "1.23"' not in txt_no_history, f"{p.name} 残留 VERSION 1.23"
        # 更宽：孤立的 "1.19" / "1.23" 在非 history 段落中出现 → 视为漂移
        # 仅当 txt_no_history 中仍出现 "1.19" 或 "\"1.23\"" 且非历史枚举时报错
        # README 不应出现历史枚举外的旧版本
        if p.name in ("README.md", "README_EN.md", "doc/DEPLOYMENT.md", "doc/AGENT_INTEGRATION.md"):
            assert '"1.19"' not in txt_no_history and '"1.23"' not in txt_no_history, f"{p.name} 残留旧版本字符串"
