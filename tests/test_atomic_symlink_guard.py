"""Issue #389 回归：atomic_write symlink 拦截 + 半截 tmp 清理 + load 误扶正防护。"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from chiguo_atomic import atomic_write
from state.persistence import StatePersistence


def _owner():
    return SimpleNamespace(
        last_tick=None, tick_seq=0, memories=[],
        holiday_parser=None,
        _sync_quiet_window=lambda: None,
        _apply_memory_dedup=lambda: None,
        _personality_initial_baseline={},
        personality=SimpleNamespace(tsundere_intensity=70.0),
        _bayesian_estimator=None, _bayesian_restored=None,
        memory_bridge=None, mono_anchor=None, wall_anchor=None,
    )


def _persist(base):
    return StatePersistence({"_base_dir": str(base)}, _owner())


def test_symlink_tmp_refused_and_unlinked(tmp_path):
    """预建 .tmp symlink → abort 且删链，目标文件不被触碰（mode/默认双分支）。"""
    victim = tmp_path / "victim.txt"
    for name, kw in (("out.txt", {"mode": 0o600}), ("plain.txt", {})):
        link = tmp_path / (name + ".tmp")
        link.symlink_to(victim)
        with pytest.raises(OSError):
            atomic_write(tmp_path / name, "pwned", **kw)
        assert not link.exists() and not link.is_symlink()
        assert not victim.exists()


def test_write_failure_cleans_partial_tmp(tmp_path, monkeypatch):
    """写中途 OSError → 半截 tmp 被清理（mode/默认双分支）。"""
    real_fdopen = os.fdopen

    def boom(fd, *a, **k):
        os.close(fd)
        raise OSError("injected write failure")

    monkeypatch.setattr(os, "fdopen", boom)
    for name, kw in (("w.txt", {"mode": 0o600}), ("p.txt", {})):
        p = tmp_path / name
        with pytest.raises(OSError):
            atomic_write(p, "data", **kw)
        assert not Path(str(p) + ".tmp").exists()
        assert not p.exists()
    monkeypatch.setattr(os, "fdopen", real_fdopen)


def test_load_rejects_corrupt_tmp(tmp_path):
    sp = _persist(tmp_path)
    (tmp_path / "chiguo_state.json.tmp").write_text("{not json")
    sp.load()
    assert not (tmp_path / "chiguo_state.json").exists()
    assert not (tmp_path / "chiguo_state.json.tmp").exists()


def test_load_rejects_tmp_without_version(tmp_path):
    sp = _persist(tmp_path)
    (tmp_path / "chiguo_state.json.tmp").write_text(json.dumps({"emotion": {}}))
    sp.load()
    assert not (tmp_path / "chiguo_state.json").exists()
    assert not (tmp_path / "chiguo_state.json.tmp").exists()


def test_load_rejects_symlink_tmp(tmp_path):
    sp = _persist(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("secret")
    (tmp_path / "chiguo_state.json.tmp").symlink_to(victim)
    sp.load()
    assert not (tmp_path / "chiguo_state.json").exists()
    assert victim.read_text() == "secret"


def test_load_promotes_valid_tmp(tmp_path):
    sp = _persist(tmp_path)
    payload = {"_version": 10, "emotion": {}, "cooldown": {}, "circadian": {}}
    (tmp_path / "chiguo_state.json.tmp").write_text(json.dumps(payload))
    sp.load()
    assert (tmp_path / "chiguo_state.json").exists()
    assert not (tmp_path / "chiguo_state.json.tmp").exists()
