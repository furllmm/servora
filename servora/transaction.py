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
            "rollback": {
                "attempted": False,
                "success": None,
                "errors": [],
                "completed": {"containers": [], "volumes": [], "networks": []},
            },
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
        if self.data["status"] == "rolled_back":

            return dict(self.data["rollback"])
        self.data["rollback"]["attempted"] = True
        rollback = self.data["rollback"]
        rollback.setdefault("completed", {"containers": [], "volumes": [], "networks": []})
        completed = rollback["completed"]
        errors = []

        def remove(kind: str, name: str, callback) -> None:
            if name in completed[kind]:
                return
            try:
                callback()
                completed[kind].append(name)
                self._save()
            except Exception as exc:
                errors.append(str(exc))

        for name in reversed(self.data["resources"]["containers"]):
            remove("containers", name, lambda name=name: podman.remove_container(name, force=True))
        for name in reversed(self.data["resources"]["volumes"]):
            remove("volumes", name, lambda name=name: podman.remove_volume(name, force=True))
        for name in reversed(self.data["resources"]["networks"]):
            remove("networks", name, lambda name=name: podman.remove_network(name))

        rollback["errors"] = errors
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
