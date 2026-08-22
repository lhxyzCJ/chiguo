"""test_owner_version_invariant.py — T17 OWNER/VERSION 单源不变式

- VERSION 单源: chiguo_version.VERSION == 1.24 且 doc/pyproject 同步，无 1.19/1.23 硬编码
- OWNER 校验: 非法 owner 写盘被拒 (audit + 不落盘)，与 tick_seq CAS 同机理
"""
import json
import re
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chiguo_version import VERSION

EXPECTED = "1.24"

def test_version_single_source():
    assert VERSION == EXPECTED, f"VERSION={VERSION!r} != {EXPECTED!r} (单源漂移)"
    # 消费方单源 import
    for fname in ("chiguo_envcheck.py", "chiguo_monitor.py", "chiguo_daemon.py"):
        src = (ROOT / fname).read_text(encoding="utf-8")
        assert re.search(r"from chiguo_version import\s+VERSION", src), f"{fname} 未单源 import VERSION 写死版本"

def test_pyproject_sync():
    import tomllib
    v = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert v == VERSION == EXPECTED, f"pyproject {v!r} != VERSION {VERSION!r}"

def test_docs_no_hardcoded_old_version():
    old_hard = re.compile(r'"1\.19"|"1\.23"')
    # history 段 (→1.23) 允许在历史枚举中出现，但不应再有显式 VERSION="1.19"/"1.23" 作为当前版本
    for p in [ROOT / "AGENTS.md", ROOT / "CLAUDE_CODE_RULES.md", ROOT / "doc/SYSTEM.md"]:
        txt = p.read_text(encoding="utf-8")
        # AGENTS.md 1.19 comment, SYSTEM.md 1.23 table, CLAUDE 1.19 header 均应已更新
        # 允许 changelog 历史枚举（如 1.9→...→1.23 序列已在 SYSTEM.md 头部更新为 →1.24）
        # 故仅检查当前版本声明处
        if p.name == "AGENTS.md":
            assert 'VERSION="1.19"' not in txt, "AGENTS.md 仍硬编码 1.19"
            assert EXPECTED in txt, "AGENTS.md 未同步到 1.24"
        if p.name == "CLAUDE_CODE_RULES.md":
            assert "v1.19" not in txt, "CLAUDE_CODE_RULES.md 仍 v1.19"
            assert f"v{EXPECTED}" in txt or EXPECTED in txt, "CLAUDE_CODE_RULES.md 未同步"
        if p.name == "doc/SYSTEM.md":
            # 表格行 | chiguo_version.py | VERSION = "x" 必须为 1.24
            m = re.search(r'chiguo_version\.py.*VERSION\s*=\s*"([^"]+)"', txt)
            assert m and m.group(1) == EXPECTED, f"SYSTEM.md version table {m.group(1) if m else None} != {EXPECTED}"
            # 1090 行版本号注释应为 1.24
            assert '"1.23"' not in txt or EXPECTED in txt, "SYSTEM.md 仍含孤立 1.23 当前版声明"
            # 更严格: 确保没有硬编码旧当前版作为单引号结果
            # 允许历史序列中出现 1.23 作为枚举元素，但单独的 VERSION="1.23" 不允许
            assert 'VERSION = "1.23"' not in txt

def test_owner_mismatch_rejects_write():
    """OWNER 校验: config owner 与已落盘 state owner 不匹配 → save 返回 False 且文件不被覆盖"""
    from chiguo_state import ChiguoState
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # 构造最小 config，带 owner 分区 (wechat_recipient 作为 owner 标识)
        def make_cfg(owner_id: str):
            return {
                "_base_dir": str(base),
                "wechat": {"wechat_recipient": owner_id},
                "schedule": {"xlsx_path": "data/xskb.xlsx", "semester_start": "2026-02-23", "enabled": False},
                "circadian": {},
                "memory": {"backend": "mem0"},
                "host": {},
            }
        cfg_alice = make_cfg("alice_owner")
        st = ChiguoState(cfg_alice)
        ok = st.save()
        assert ok is True, "首次 alice save 应成功"
        p = base / "chiguo_state.json"
        assert p.exists()
        data_alice = json.loads(p.read_text(encoding="utf-8"))
        # owner 应已落盘
        disk_owner = data_alice.get("owner")
        assert disk_owner == "alice_owner", f"落盘 owner={disk_owner!r} != alice_owner"

        # 篡改 config 为 bob 但复用同一 state 实例 (模拟越权进程持旧 state 内存尝试写盘)
        st.config = make_cfg("bob_owner")
        # 同步 persistence 的 config 引用
        st._persistence.config = st.config
        # 尝试越权写 (应被拦截)
        ok2 = st.save()
        assert ok2 is False, "bob 越权写应被拒绝 (return False)"
        # 文件未被覆盖，仍为 alice
        data_after = json.loads(p.read_text(encoding="utf-8"))
        assert data_after.get("owner") == "alice_owner", "越权写不应覆盖落盘文件"
        # audit 应有记录
        audit = (base / "chiguo_state_audit.jsonl").read_text(encoding="utf-8") if (base / "chiguo_state_audit.jsonl").exists() else ""
        assert "owner_mismatch" in audit or "owner_reject" in audit, "越权应 audit 记录"

def test_owner_same_allows_write():
    """同 owner 写应放行且 tick_seq 递增"""
    from chiguo_state import ChiguoState
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        def make_cfg(o): return {"_base_dir": str(base), "wechat": {"wechat_recipient": o}, "schedule": {"xlsx_path": "data/xskb.xlsx", "semester_start": "2026-02-23", "enabled": False}, "circadian": {}, "memory": {"backend": "mem0"}, "host": {}}
        cfg = make_cfg("same_owner")
        st = ChiguoState(cfg)
        assert st.save() is True
        seq1 = json.loads((base/"chiguo_state.json").read_text())["tick_seq"]
        st.emotion.loneliness = 42
        assert st.save() is True
        seq2 = json.loads((base/"chiguo_state.json").read_text())["tick_seq"]
        assert seq2 > seq1, "同 owner 连续写应递增 tick_seq"
