"""tests/test_mem0_client_timeout.py — #402 底层 HTTP 真实超时（不碰真实 ollama/网络）。

- stub 注入：假 _m（embedding_model.client=指向 127.0.0.1:9 的真 ollama Client，
  llm.client=真 openai.OpenAI），调 _apply_client_timeouts 后断言 httpx 超时
  与 llm .timeout 已收敛。
- 降级：client 类型不对（object()）时不抛异常。
- 构造超时：_MEM0_CONSTRUCT_TIMEOUT 打桩 0.2 + _ensure_mem0 挂起 → available 快速 False。
"""
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import memory.mem0_backend as mb
from memory.mem0_backend import Mem0Backend


def _quiet_backend(**kw):
    kw.setdefault("llm_api_key", "fake-key")
    return Mem0Backend(**kw)


def test_apply_client_timeouts_stub():
    """真 Client 打桩：embed httpx timeout==10s，llm .timeout==60s。"""
    from ollama import Client as OllamaClient
    from openai import OpenAI
    b = _quiet_backend(embedder_base_url="http://127.0.0.1:9")
    b._m = SimpleNamespace(
        embedding_model=SimpleNamespace(
            client=OllamaClient(host="http://127.0.0.1:9")),
        llm=SimpleNamespace(client=OpenAI(api_key="x")),
    )
    b._apply_client_timeouts()
    assert b._m.embedding_model.client._client.timeout == httpx.Timeout(10.0)
    assert b._m.llm.client.timeout == 60.0
    print("  OK test_apply_client_timeouts_stub")


def test_apply_client_timeouts_degrade(caplog):
    """client 类型不对 → 不抛异常、只 warning，可用性不断。"""
    b = _quiet_backend()
    b._m = SimpleNamespace(
        embedding_model=SimpleNamespace(client=object()),
        llm=SimpleNamespace(client=object()),
    )
    with caplog.at_level(logging.WARNING):
        b._apply_client_timeouts()  # 不抛即过
    print("  OK test_apply_client_timeouts_degrade")


def test_construct_timeout_fast_false(monkeypatch):
    """_ensure_mem0 挂起 + 构造预算 0.2s → available 快速 False。"""
    import time
    monkeypatch.setattr(mb, "_MEM0_CONSTRUCT_TIMEOUT", 0.2)
    monkeypatch.setattr(mb, "_probe_ollama_tags", lambda url, timeout: None)
    b = _quiet_backend()
    b._available = None
    b._m = None
    monkeypatch.setattr(b, "_ensure_mem0", lambda: time.sleep(30))
    t0 = time.monotonic()
    assert b.available is False
    assert time.monotonic() - t0 < 10, "构造超时应快速降级"
    assert b._last_error and b._last_error[1] == "available", b._last_error
    print("  OK test_construct_timeout_fast_false")
