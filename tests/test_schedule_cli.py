#!/usr/bin/env python3
"""test_schedule_cli.py — daemon 三新子命令输出契约(批次 5,二十轮 A4)"""

import contextlib, io, json, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

import chiguo_daemon as D


def _tmp_cfg(td, sched_overrides=None):
    src = Path("chiguo_proactive.toml").read_text()
    cfg_p = Path(td) / "test.toml"
    cfg_p.write_text(re.sub(r"(?m)^lancedb_path\s*=.*$",
                            f'lancedb_path = "{Path(td) / "no_lancedb"}"', src))
    return str(cfg_p)


def _run(fn, *args):
    out = io.StringIO()
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn(*args)
        code = 0
    except SystemExit as e:
        code = e.code or 0
    return code, json.loads(out.getvalue()), out.getvalue(), err.getvalue()


def test_attention_shape_and_zero_write():
    with tempfile.TemporaryDirectory() as td:
        cfg_p = _tmp_cfg(td)
        code, r, _, _ = _run(D._cmd_attention, cfg_p)
        assert code == 0 and r["ok"] is True
        assert set(r["attention"]) == {"t1", "t2", "t3", "week_num", "today_exceptions"}
        assert isinstance(r["attention"]["t1"], list) and isinstance(r["attention"]["t3"], dict)
        assert isinstance(r["emotion"], dict), "emotion 快照(缺失 → {})"
        assert list(Path(td).iterdir()) == [Path(td, "test.toml")], f"轻量读零写, got {list(Path(td).iterdir())}"
    print("  OK test_attention_shape_and_zero_write")


def test_schedule_change_success_and_shapes():
    with tempfile.TemporaryDirectory() as td:
        cfg_p = _tmp_cfg(td)
        code, r, _, _ = _run(D._cmd_schedule_change,
                             '{"kind": "reminder", "when": {"date": "2026-08-20"}, "label": "交材料"}',
                             cfg_p)
        assert code == 0 and r["action"] == "schedule_change" and r["ok"] is True
        assert "8月20日" in r["text"] and "周四" in r["text"], f"A4 text=确认文案(含星期+日期), got {r['text']}"
        # 畸形 JSON → ok:false + bad_json + 不写入(十九轮安全钉)
        before = Path(td, "schedule_overrides.json").read_text()
        code, r, _, err = _run(D._cmd_schedule_change, "{not json", cfg_p)
        assert code == 1 and r["ok"] is False and r["reason"] == "bad_json"
        assert r["question"] == "处理失败,再试一次?"
        assert Path(td, "schedule_overrides.json").read_text() == before, "畸形 JSON 不写入"
        assert "畸形" in err, "stderr 诊断"
        # ApiRejection → reason 类别 + H5 question
        code, r, _, _ = _run(D._cmd_schedule_change,
                             '{"kind": "reminder", "when": {"date": "2026-08-01"}, "label": "过去"}',
                             cfg_p)
        assert code == 1 and r["ok"] is False and r["reason"] == "past_date"
        assert "过去了" in r["question"] and r.get("missing") == ["date"], f"got {r}"
        # remove 路由
        code, r, _, _ = _run(D._cmd_schedule_change,
                             '{"kind": "remove", "match": {"date": "2026-08-20", "label": "交材料"}}',
                             cfg_p)
        assert code == 0 and r["ok"] is True
    print("  OK test_schedule_change_success_and_shapes")


def test_schedule_recall_shape():
    with tempfile.TemporaryDirectory() as td:
        cfg_p = _tmp_cfg(td)
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": [
            {"id": "a1", "type": "anniversary", "name": "哥哥的生日", "date": "05-11",
             "note": "", "created_at": "2026-01-01"}]}))
        code, r, _, _ = _run(D._cmd_schedule_recall, "生日", cfg_p)
        assert code == 0 and r["action"] == "schedule_recall" and r["ok"] is True
        assert any("生日" in m.get("label", "") for m in r["matches"]), f"got {r}"
        assert r["query"] == "生日"
    print("  OK test_schedule_recall_shape")


if __name__ == "__main__":
    print("test_schedule_cli.py\n")
    tests = [test_attention_shape_and_zero_write, test_schedule_change_success_and_shapes,
             test_schedule_recall_shape]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
