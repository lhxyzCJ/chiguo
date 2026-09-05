#!/usr/bin/env python3
"""test_play_proof_provider_encapsulation.py — PlayProofProvider 封装回归（Issue #399）

Given: decision/base 经 PlayProofProvider.service 拿 NeteaseService，
       而非直接访问私有 _svc
When:  引擎构造 / toml mtime 热重载
Then:  engine.netease_service 与 provider.service 同一对象；base.py 无 _svc 残留
"""
import os
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import DecisionEngine
from netease.service import NeteaseService
from schedule.facade import PlayProofProvider

ROOT = Path(__file__).resolve().parent.parent


def _make_engine(tmp: str) -> DecisionEngine:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text((ROOT / "chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    return DecisionEngine(str(cfg_path), str(Path(tmp) / "chiguo_decisions.jsonl"))


def test_provider_service_property_returns_held_instance():
    """service property 返回构造时持有的同一 NeteaseService 实例。"""
    with tempfile.TemporaryDirectory() as td:
        provider = PlayProofProvider({"netease": {}}, td)
        assert isinstance(provider.service, NeteaseService)
        assert provider.service is provider.service, "service 应稳定返回同一对象"


def test_engine_netease_service_uses_public_property():
    """构造后 engine.netease_service is provider.service（Issue #399 init 处）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        assert eng.netease_service is eng._play_proof_provider.service


def test_reload_rebinds_netease_service_via_property():
    """热重载重建 provider 后 netease_service 重新绑定到新 provider.service。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        old_provider = eng._play_proof_provider
        old_text = Path(eng.config_path).read_text()
        new_text = old_text.replace("loneliness = 15.0", "loneliness = 99.0")
        assert new_text != old_text, "测试前置：toml 须含 loneliness = 15.0 可替换行"
        time.sleep(0.05)
        Path(eng.config_path).write_text(new_text)
        eng._maybe_reload_config()
        assert eng.config["emotion"]["loneliness"] == 99.0, "mtime 热重载应先生效"
        assert eng._play_proof_provider is not old_provider, "热重载应重建 provider"
        assert eng.netease_service is eng._play_proof_provider.service
        assert eng.netease_service is not old_provider.service


def test_base_has_no_private_svc_access():
    """审计：decision/base.py 不得直接访问 _svc（防 Issue #399 回退）。"""
    base_text = (ROOT / "decision" / "base.py").read_text()
    assert "_svc" not in base_text, "decision/base.py 残留 _svc 私有访问"
