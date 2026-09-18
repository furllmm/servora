from pathlib import Path

from servora.audit import AuditLog


def test_audit_append_read_and_redact(tmp_path: Path):
    log = AuditLog(tmp_path, max_bytes=65536)
    log.append(
        "test.event",
        "ai",
        name="demo",
        details={"api_key": "secret", "nested": {"password": "hidden"}, "ok": "value"},
    )
    rows = log.read()
    assert rows[0]["event"] == "test.event"
    assert rows[0]["details"]["api_key"] == "[redacted]"
    assert rows[0]["details"]["nested"]["password"] == "[redacted]"
    assert rows[0]["details"]["ok"] == "value"


def test_audit_limit_and_filter(tmp_path: Path):
    log = AuditLog(tmp_path, max_bytes=65536)
    for i in range(5):
        log.append("event.a" if i % 2 == 0 else "event.b", "system", name=f"c{i}")
    assert len(log.read(limit=2)) == 2
    rows = log.read(event="event.a")
    assert len(rows) == 3
    assert rows[0]["name"] == "c4"


def test_audit_rotates(tmp_path: Path):
    log = AuditLog(tmp_path, max_bytes=65536)
    log.append("event", "system", details={"blob": "x" * 3000})
    log.append("event", "system", details={"blob": "y" * 3000})
    log.max_bytes = 100
    log.append("event", "system", details={"blob": "z"})
    assert log.path.exists()
    assert log.path.with_name("audit.jsonl.1").exists()
