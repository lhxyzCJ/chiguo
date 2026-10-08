"""tests/test_memory_server.py — 常驻只读记忆边车 memory/server.py 契约测试。

只测边车进程本身（不改业务代码）：经 stdin/stdout 跑真实子进程
`python memory/server.py`，覆盖 ping/attention/memory_search 成功路径、
未知 cmd/空 query 拒绝、脏行+空行包容、stdin EOF 退出码。

隔离说明（复用 tests/conftest.py 全局机制，不自建）：
- CWD：conftest 会话级 fixture 已固定为项目根（边车要求 CWD=项目根）；
- os.environ：conftest 函数级 fixture 快照/还原，子进程 env 显式构造
 （含 CHIGUO_MEM0_DISABLED=1，mem0 恒不可用 → memory_search 确定性
  返回 count=0，不碰真实记忆库/网络）；
- 只读语义：边车仅服务 attention/memory_search 查询（写路径不进边车），
  不污染项目根状态文件（靠 conftest 会话守卫兜底，本文件不断言 mtime）。

用法: uv run pytest tests/test_memory_server.py -q --confcutdir=tests
"""
import json
import os
import select
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "memory" / "server.py"
RESP_TIMEOUT = 120  # 边车冷启动含 import，首响应预算放宽


def _server_env():
    env = dict(os.environ)
    env["CHIGUO_MEM0_DISABLED"] = "1"  # 确定性：mem0 恒不可用
    return env


class ServerProc:
    """边车子进程小封装：发一行 JSON 请求 → 读一行 JSON 响应。"""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
            env=_server_env(),
        )

    def request(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return self.read_resp()

    def raw(self, line):
        """写原始行（脏行/空行包容测试用）。"""
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_resp(self, timeout=RESP_TIMEOUT):
        fd = self.proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        assert ready, f"{timeout}s 内边车无响应（进程存活={self.proc.poll() is None}）"
        line = self.proc.stdout.readline()
        assert line.strip(), "边车返回空行（stdout 应保持纯 JSON 行）"
        return json.loads(line)

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


import pytest  # noqa: E402  (import 位置：helper 定义后，保持单文件自包含)


@pytest.fixture(scope="module")
def server():
    """模块级单边车进程：启动开销（import）只付一次，各用例串行问答。"""
    srv = ServerProc()
    yield srv
    srv.close()


def test_ping(server):
    r = server.request({"id": "p1", "cmd": "ping"})
    assert r["ok"] is True
    assert r["action"] == "ping"
    assert r["id"] == "p1"  # 响应必须回显请求 id（bridge 按 id 匹配）


def test_attention_shape(server):
    r = server.request({"id": "a1", "cmd": "attention"})
    assert r["ok"] is True
    assert r["action"] == "attention"
    assert r["id"] == "a1"
    # 形状同 --attention：action/attention/emotion/week_num/today_exceptions
    for key in ("attention", "emotion", "week_num", "today_exceptions"):
        assert key in r, f"attention 响应缺键: {key}"
    assert r["week_num"] == r["attention"]["week_num"]
    assert r["today_exceptions"] == r["attention"]["today_exceptions"]
    assert isinstance(r["today_exceptions"], list)


def test_memory_search_success(server):
    # CHIGUO_MEM0_DISABLED=1 → 后端不可用 → 确定性空结果（不抛、ok 仍 true）
    r = server.request({"id": "m1", "cmd": "memory_search", "query": "咖啡"})
    assert r["ok"] is True
    assert r["action"] == "memory_search"
    assert r["query"] == "咖啡"  # query 原样回显
    assert r["id"] == "m1"
    assert r["count"] == len(r["memories"])
    assert r["count"] == 0 and r["memories"] == []


def test_memory_search_limit_clamp(server):
    # limit 越界/非法 → 回落缺省 5，不断言条数（空后端），只断言不崩且 ok
    for limit in (999, 0, -3, "x", None):
        r = server.request({"id": f"lim-{limit}", "cmd": "memory_search",
                            "query": "q", "limit": limit})
        assert r["ok"] is True, f"非法 limit={limit!r} 不应拒绝: {r}"
        assert r["action"] == "memory_search"


def test_unknown_cmd_rejected(server):
    r = server.request({"id": "e1", "cmd": "bogus_cmd"})
    assert r["ok"] is False
    assert r["reason"]  # 非空原因
    assert r["id"] == "e1"  # 拒绝响应同样回显 id


def test_empty_query_rejected(server):
    for req in ({"id": "e2", "cmd": "memory_search", "query": ""},
                {"id": "e3", "cmd": "memory_search"}):
        r = server.request(req)
        assert r["ok"] is False, f"空 query 应拒绝: {req}"
        assert r["id"] == req["id"]


def test_dirty_and_blank_lines_tolerated(server):
    # 脏行（非 JSON）+ 空行不得破坏行协议：后续正常请求仍有响应
    server.raw("not json {{{")
    server.raw("")
    server.raw("   ")
    r = server.request({"id": "p2", "cmd": "ping"})
    assert r == {"ok": True, "action": "ping", "id": "p2"}


def test_eof_exits_zero_and_ready_on_stderr():
    # stdin EOF → exit 0；就绪诊断进 stderr（stdout 保持纯净，communicate 一次取全）
    p = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT),
        env=_server_env(),
    )
    out, err = p.communicate(input='{"id":"x","cmd":"ping"}\n', timeout=RESP_TIMEOUT)
    assert p.returncode == 0, f"EOF 退出码应为 0，实际 {p.returncode}"
    assert json.loads(out.strip())["action"] == "ping"
    assert "[memory-server] ready" in err  # 诊断进 stderr，不污染 stdout 行协议
