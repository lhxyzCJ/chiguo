"""tests/test_v2_cli.py — v2 CLI（chiguo db ...）TDD。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli import main  # noqa: E402
from storage.sqlite.migrations import SCHEMA_VERSION  # noqa: E402


def _run(capsys, *argv):
    rc = main(list(argv))
    captured = capsys.readouterr()
    return rc, captured


def test_db_migrate_then_status(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    rc, cap = _run(capsys, "db", "migrate", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "db_migrate"
    assert out["applied"] == list(range(1, SCHEMA_VERSION + 1))

    rc, cap = _run(capsys, "db", "status", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "db_status"
    assert out["ok"] is True
    assert out["initialized"] is True
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["code_schema_version"] == SCHEMA_VERSION
    assert out["path"] == str(dbp)
    assert "events" in out["counts"]


def test_db_migrate_is_idempotent(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "db", "migrate", "--db", str(dbp))
    assert rc == 0
    assert json.loads(cap.out)["applied"] == []


def test_db_status_uninitialized_does_not_create_file(tmp_path, capsys):
    dbp = tmp_path / "missing" / "c.sqlite"
    rc, cap = _run(capsys, "db", "status", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["initialized"] is False
    assert out["schema_version"] == 0
    assert not dbp.exists(), "status 不应有创建数据库的副作用"


def test_db_integrity(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "db", "integrity", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["ok"] is True
    assert out["foreign_key_violations"] == []


def test_db_backup_default_dest(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "db", "backup", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    dest = Path(out["backup_path"])
    assert dest.exists()
    assert dest.parent == tmp_path / "backups"
    # 备份可打开且 schema 一致
    rc, cap = _run(capsys, "db", "status", "--db", str(dest))
    assert json.loads(cap.out)["schema_version"] == SCHEMA_VERSION


def test_db_status_corrupt_file_fails(tmp_path, capsys):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"garbage" * 128)
    rc, cap = _run(capsys, "db", "status", "--db", str(bad))
    assert rc == 1
    out = json.loads(cap.out)
    assert out["ok"] is False
    assert "error" in out


def test_unknown_command_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as ei:
        main(["db", "nope"])
    assert ei.value.code == 2
