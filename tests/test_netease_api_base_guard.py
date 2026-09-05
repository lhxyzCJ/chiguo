#!/usr/bin/env python3
"""test_netease_api_base_guard.py — P2-07 NETEASE_API_BASE 回环校验防 env 污染外泄。

契约：
- env/显式参数指向非回环 host → 构造时拦截并回退 http://localhost:3000（stderr 留痕）
- 回环全覆盖：localhost / 127.0.0.0/8 / ::1 / ::ffff:127.0.0.1 变体均放行
- 拒绝外发：污染 env 后 _api_get 实际请求的 host 恒为回环（opener.open 录制断言）
"""
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_net import is_local_host, is_local_url
from netease.bridge import NeteaseBridge

DEFAULT = "http://localhost:3000"


# ── 1. env 污染拦截 ─────────────────────────────

def test_polluted_env_falls_back_to_default(tmp_path, monkeypatch, capsys):
    """污染 env 为 http://evil.com → 拦截并回退默认"""
    monkeypatch.setenv("NETEASE_API_BASE", "http://evil.com")
    b = NeteaseBridge(str(tmp_path))
    assert b.api_base == DEFAULT
    assert "非回环" in capsys.readouterr().err


def test_explicit_non_loopback_base_rejected(tmp_path, monkeypatch, capsys):
    """显式 api_base 为外部 host → 同样拦截回退"""
    monkeypatch.delenv("NETEASE_API_BASE", raising=False)
    b = NeteaseBridge(str(tmp_path), api_base="http://8.8.8.8:3000")
    assert b.api_base == DEFAULT
    assert "非回环" in capsys.readouterr().err


# ── 2. 回环放行 / 非回环拦截 ─────────────────────

@pytest.mark.parametrize("base", [
    "http://localhost:3000",
    "http://localhost:4000",
    "http://127.0.0.1:3000",
    "http://127.0.0.2:3000",      # 127/8 非 .1 同属回环
    "http://127.1.2.3:4000",
    "http://[::1]:3000",
    "http://[::ffff:127.0.0.1]:3000",  # IPv4 映射变体
])
def test_loopback_bases_allowed(tmp_path, monkeypatch, capsys, base):
    monkeypatch.delenv("NETEASE_API_BASE", raising=False)
    b = NeteaseBridge(str(tmp_path), api_base=base)
    assert b.api_base == base
    assert "非回环" not in capsys.readouterr().err


@pytest.mark.parametrize("base", [
    "http://evil.com",
    "http://evil.com:3000",
    "http://8.8.8.8:3000",
    "http://192.168.1.1:3000",
    "http://10.0.0.1:3000",
    "http://0.0.0.0:3000",
    "not a url",
])
def test_non_loopback_bases_rejected(tmp_path, monkeypatch, base):
    monkeypatch.delenv("NETEASE_API_BASE", raising=False)
    b = NeteaseBridge(str(tmp_path), api_base=base)
    assert b.api_base == DEFAULT


def test_default_base_no_warning(tmp_path, monkeypatch, capsys):
    """默认构造（无 env）静默接受，不打扰正常路径"""
    monkeypatch.delenv("NETEASE_API_BASE", raising=False)
    b = NeteaseBridge(str(tmp_path))
    assert b.api_base == DEFAULT
    assert "非回环" not in capsys.readouterr().err


# ── 3. chiguo_net 回环覆盖 ───────────────────────

@pytest.mark.parametrize("host,expected", [
    ("localhost", True),
    ("LOCALHOST", True),
    ("127.0.0.1", True),
    ("127.0.0.2", True),
    ("127.255.255.254", True),
    ("::1", True),
    ("::ffff:127.0.0.1", True),
    ("evil.com", False),
    ("8.8.8.8", False),
    ("192.168.1.1", False),
    ("10.0.0.1", False),
    ("0.0.0.0", False),
    ("", False),
])
def test_is_local_host_loopback_coverage(host, expected):
    assert is_local_host(host) is expected


def test_is_local_url_uses_loopback_check():
    assert is_local_url("http://127.0.0.2:3000/x") is True
    assert is_local_url("http://[::ffff:127.0.0.1]:3000/x") is True
    assert is_local_url("http://evil.com/x") is False


# ── 4. 拒绝外发：污染后请求仍只发往回环 ──────────

class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"code": 200}'


class _FakeOpener:
    def __init__(self, calls):
        self._calls = calls

    def open(self, req, timeout=None):
        self._calls.append(req.full_url)
        return _FakeResp()


def test_polluted_env_request_never_leaves_loopback(tmp_path, monkeypatch):
    """污染 env 后 _api_get 实际 open 的 URL host 恒为回环"""
    monkeypatch.setenv("NETEASE_API_BASE", "http://evil.com:3000")
    b = NeteaseBridge(str(tmp_path), retry_count=0)
    calls = []
    monkeypatch.setattr("chiguo_net.build_no_proxy_opener", lambda: _FakeOpener(calls))
    out = b._api_get("/login/status")
    assert out == {"code": 200}
    assert len(calls) == 1
    host = urllib.request.urlsplit(calls[0]).hostname
    assert host == "localhost", f"凭据外发至非回环: {calls[0]}"
