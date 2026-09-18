from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
import json

from .apps import AppManifestError, service_container_name, validate_app_manifest
from typing import Any


class ReliabilityError(ValueError):
    pass


def _check_port(port: int, host: str = "127.0.0.1") -> dict[str, Any]:
    try:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        return {"ok": False, "code": "port_unavailable", "port": port, "message": str(exc)}
    return {"ok": True, "port": port}


def preflight_manifest(podman, manifest: Any, runtime_root: str | Path, *, check_images: bool = False) -> dict[str, Any]:
    """Validate common launch hazards before mutating Podman state."""
    findings: list[dict[str, Any]] = []
    root = Path(runtime_root)

    if not root.exists():
        findings.append({"severity": "error", "code": "runtime_missing", "message": "Servora runtime directory does not exist"})
    elif not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        findings.append({"severity": "error", "code": "runtime_permission", "message": "Servora runtime directory is not accessible"})

    usage = shutil.disk_usage(root if root.exists() else Path.home())
    if usage.free < 256 * 1024 * 1024:
        findings.append({"severity": "error", "code": "low_disk_space", "message": "Less than 256 MiB free on the Servora filesystem", "free_bytes": usage.free})

    service_names = {s.name for s in manifest.services}
    images = None
    if check_images:
        try:
            images = podman.list_images()
        except Exception as exc:
            findings.append({"severity": "error", "code": "image_inventory_failed", "message": str(exc)})

    for service in manifest.services:
        missing = sorted(set(service.depends_on) - service_names)
        if missing:
            findings.append({"severity": "error", "code": "missing_dependency", "service": service.name, "dependency": missing[0]})
        for port in service.ports:
            result = _check_port(int(port["host"]))
            if not result["ok"]:
                result.update({"service": service.name})
                findings.append({"severity": "error", **result})
        if check_images and images is not None:
            refs = set()
            for item in images:
                for key in ("RepoTags", "Names", "Repository"):
                    value = item.get(key)
                    if isinstance(value, list):
                        refs.update(str(x) for x in value)
                    elif value:
                        refs.add(str(value))
            if service.image not in refs:
                findings.append({"severity": "warning", "code": "image_not_local", "service": service.name, "image": service.image, "message": "Image is not present locally; install will require an image pull or preloaded image"})

    return {"ok": not any(x["severity"] == "error" for x in findings), "findings": findings}


def validate_runtime_state(root: str | Path) -> dict[str, Any]:
    """Detect malformed Servora state without modifying it."""
    root = Path(root)
    required = ["config", "metadata", "logs", "podman", "apps", "backups"]
    findings: list[dict[str, Any]] = []
    if not root.exists():
        return {"ok": False, "findings": [{"severity": "error", "code": "runtime_missing", "message": "Servora runtime directory does not exist"}]}
    for name in required:
        path = root / name
        if not path.exists():
            findings.append({"severity": "error", "code": "missing_runtime_path", "path": name, "message": f"Missing runtime path: {name}"})
        elif not path.is_dir():
            findings.append({"severity": "error", "code": "invalid_runtime_path", "path": name, "message": f"Runtime path is not a directory: {name}"})
    config = root / "config" / "servora.json"
    if not config.exists():
        findings.append({"severity": "error", "code": "missing_config", "message": "Servora config file is missing"})
    else:
        try:
            import json
            data = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                findings.append({"severity": "error", "code": "invalid_config", "message": "Servora config is invalid"})
        except (OSError, ValueError):
            findings.append({"severity": "error", "code": "invalid_config", "message": "Servora config is unreadable or malformed"})
    return {"ok": not findings, "findings": findings}


def scan_reliability(podman, root: str | Path) -> dict[str, Any]:
    """Inspect managed state without deleting or repairing anything."""
    root = Path(root)
    findings: list[dict[str, Any]] = []
    managed_apps: dict[str, Any] = {}
    apps_dir = root / "apps"
    if apps_dir.exists():
        for path in sorted(apps_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                manifest = validate_app_manifest(raw)
                managed_apps[manifest.name] = manifest
            except (OSError, json.JSONDecodeError, AppManifestError) as exc:
                findings.append({"severity": "error", "code": "corrupt_app_state",
                                 "path": str(path), "message": str(exc)})

    for path, label in [
        (root / "config" / "recovery_policy.json", "recovery_policy"),
        (root / "metadata" / "recovery_state.json", "recovery_state"),
    ]:
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                findings.append({"severity": "error", "code": "corrupt_state",
                                 "path": str(path), "message": f"{label}: {exc}"})

    audit_path = root / "logs" / "audit.jsonl"
    if audit_path.exists():
        try:
            for line_no, line in enumerate(audit_path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        findings.append({"severity": "error", "code": "corrupt_audit_log",
                                         "path": str(audit_path), "line": line_no,
                                         "message": "invalid JSONL record"})
        except OSError as exc:
            findings.append({"severity": "error", "code": "state_read_error",
                             "path": str(audit_path), "message": str(exc)})

    containers = podman.list_containers(all=True)
    expected = {
        service_container_name(app.name, service.name)
        for app in managed_apps.values()
        for service in app.services
    }
    actual_managed = set()
    for item in containers:
        raw_name = item.get("Names") or item.get("Name") or item.get("name")
        names = raw_name if isinstance(raw_name, list) else [raw_name]
        for raw in names:
            if isinstance(raw, str) and raw.startswith("servora-"):
                actual_managed.add(raw)
    actual_managed.discard(None)
    for name in sorted(actual_managed - expected):
        findings.append({"severity": "warning", "code": "orphan_container",
                         "name": name, "message": "Servora-prefixed container is not referenced by the app registry"})
    for name in sorted(expected - actual_managed):
        findings.append({"severity": "warning", "code": "missing_container",
                         "name": name, "message": "App registry references a container that does not exist"})

    return {
        "ok": not any(x.get("severity") == "error" for x in findings),
        "findings": findings,
        "managed_apps": sorted(managed_apps),
        "expected_containers": sorted(expected),
    }
