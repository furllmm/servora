from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


class TransactionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResourceTransaction:
    """Small persistent journal for resources created by one operation."""

    def __init__(self, root: str | Path, operation: str, name: str | None = None):
        self.root = Path(root)
        self.path = self.root / "metadata" / "operations"
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = self.path / f"{uuid.uuid4().hex}.json"
        self.data = {
            "id": self.file.stem,
            "operation": operation,
            "name": name,
            "status": "active",
            "started_at": _now(),
            "finished_at": None,
            "resources": {"containers": [], "volumes": [], "networks": []},
            "rollback": {"attempted": False, "success": None, "errors": []},
        }
        self._save()

    def _save(self) -> None:
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.file)

    def record(self, kind: str, name: str) -> None:
        if kind not in self.data["resources"]:
            raise TransactionError(f"Unsupported resource kind: {kind}")
        if name not in self.data["resources"][kind]:
            self.data["resources"][kind].append(name)
            self._save()

    def commit(self) -> None:
        self.data["status"] = "committed"
        self.data["finished_at"] = _now()
        self._save()

    def rollback(self, podman) -> dict:
        self.data["rollback"]["attempted"] = True
        errors = []
        for name in reversed(self.data["resources"]["containers"]):
            try:
                podman.remove_container(name, force=True)
            except Exception as exc:
                errors.append(str(exc))
        for name in reversed(self.data["resources"]["volumes"]):
            try:
                podman.remove_volume(name, force=True)
            except Exception as exc:
                errors.append(str(exc))
        for name in reversed(self.data["resources"]["networks"]):
            try:
                podman.remove_network(name)
            except Exception as exc:
                errors.append(str(exc))
        self.data["rollback"]["errors"] = errors
        self.data["rollback"]["success"] = not errors
        self.data["status"] = "rolled_back" if not errors else "rollback_failed"
        self.data["finished_at"] = _now()
        self._save()
        return self.data["rollback"]


def list_operations(root: str | Path, limit: int = 50) -> list[dict]:
    path = Path(root) / "metadata" / "operations"
    if not path.exists():
        return []
    result = []
    for item in sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            result.append(json.loads(item.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if len(result) >= max(1, limit):
            break
    return result
