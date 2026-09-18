from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(api[_-]?key|token|password|passwd|secret|authorization|credential|private[_-]?key)", re.I)
_MAX_DETAIL = 4000


class AuditError(ValueError):
    pass


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _redact(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_redact(x, depth + 1) for x in value[:100]]
    if isinstance(value, str):
        if len(value) > _MAX_DETAIL:
            return value[:_MAX_DETAIL] + "…"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_DETAIL]


class AuditLog:
    def __init__(self, root: str | Path, max_bytes: int = 2 * 1024 * 1024):
        if not isinstance(max_bytes, int) or max_bytes < 64 * 1024:
            raise AuditError("max_bytes is too small")
        self.path = Path(root) / "logs" / "audit.jsonl"
        self.max_bytes = max_bytes

    def _rotate(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        rotated = self.path.with_name(self.path.name + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            self.path.replace(rotated)
        except OSError as exc:
            raise AuditError("could not rotate audit log") from exc

    def append(
        self,
        event: str,
        actor: str,
        status: str = "ok",
        *,
        name: str | None = None,
        action: str | None = None,
        summary: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event, str) or not event or len(event) > 100:
            raise AuditError("invalid audit event")
        if actor not in {"user", "ai", "policy", "system"}:
            raise AuditError("invalid audit actor")
        if not isinstance(status, str) or not status or len(status) > 40:
            raise AuditError("invalid audit status")
        entry: dict[str, Any] = {
            "timestamp": int(time.time()),
            "event": event,
            "actor": actor,
            "status": status,
        }
        for key, value in {
            "name": name,
            "action": action,
            "summary": summary,
            "reason": reason,
        }.items():
            if value is not None:
                entry[key] = str(value)[:_MAX_DETAIL]
        if details:
            entry["details"] = _redact(details)
        encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(encoded.encode("utf-8")) > _MAX_DETAIL * 2:
            entry["details"] = "[details truncated]"
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate()
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
        except OSError as exc:
            raise AuditError("could not write audit log") from exc
        return entry

    def read(self, limit: int = 100, *, event: str | None = None, name: str | None = None) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise AuditError("limit must be between 1 and 500")
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AuditError("could not read audit log") from exc
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event and item.get("event") != event:
                continue
            if name and item.get("name") != name:
                continue
            rows.append(item)
            if len(rows) >= limit:
                break
        return rows
