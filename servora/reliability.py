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


def preflight_manifest(podman, manifest: Any, runtime_root: str | Path, *, check_images: bool = False, min_free_bytes: int = 256 * 1024 * 1024) -> dict[str, Any]:
    """Validate common launch hazards before mutating Podman state."""
    findings: list[dict[str, Any]] = []
    root = Path(runtime_root)

    if not root.exists():
        findings.append({"severity": "error", "code": "runtime_missing", "message": "Servora runtime directory does not exist"})
    elif not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        findings.append({"severity": "error", "code": "runtime_permission", "message": "Servora runtime directory is not accessible"})

    usage = shutil.disk_usage(root if root.exists() else Path.home())
    if min_free_bytes < 0:
        raise ReliabilityError("min_free_bytes must be non-negative")
    if usage.free < min_free_bytes:
        findings.append({"severity": "error", "code": "low_disk_space", "message": "Insufficient free space on the Servora filesystem", "free_bytes": usage.free, "required_bytes": min_free_bytes})

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
        else:
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                findings.append({"severity": "error", "code": "runtime_permission", "path": name, "message": f"Runtime path is not readable/writable: {name}"})

    for name in ("config", "metadata", "logs", "podman", "apps", "backups"):
        path = root / name
        if not path.is_dir():
            continue
        probe = path / ".servora-write-test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            try:
                if probe.exists():
                    probe.unlink()
            except OSError:
                pass
            findings.append({"severity": "error", "code": "runtime_write_failed", "path": name, "message": f"Runtime path is not writable: {name}", "error": str(exc)})
    usage = shutil.disk_usage(root)
    disk = {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": round((usage.free / usage.total) * 100, 2) if usage.total else 0,
    }
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
    return {"ok": not findings, "findings": findings, "disk": disk}


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



def validate_backup_artifact(path: str | Path, *, checksum: str | None = None) -> dict[str, Any]:
    """Validate a Servora backup artifact without extracting or modifying it."""
    artifact = Path(path)
    findings: list[dict[str, Any]] = []
    if not artifact.exists() or not artifact.is_file():
        return {"ok": False, "findings": [{"severity": "error", "code": "backup_missing", "message": "Backup artifact does not exist"}]}
    if artifact.stat().st_size == 0:
        findings.append({"severity": "error", "code": "backup_empty", "message": "Backup artifact is empty"})

    if checksum is not None:
        import hashlib
        if len(checksum) != 64 or any(c not in "0123456789abcdefABCDEF" for c in checksum):
            findings.append({"severity": "error", "code": "invalid_checksum", "message": "Checksum must be a SHA-256 hex digest"})
        else:
            digest = hashlib.sha256()
            try:
                with artifact.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest().lower() != checksum.lower():
                    findings.append({"severity": "error", "code": "checksum_mismatch", "message": "Backup checksum does not match"})
            except OSError as exc:
                findings.append({"severity": "error", "code": "backup_read_error", "message": str(exc)})

    suffixes = artifact.name.lower()
    if suffixes.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        import tarfile
        try:
            with tarfile.open(artifact, mode="r:*") as archive:
                members = archive.getmembers()
                for member in members:
                    member_path = Path(member.name)
                    if member.name.startswith("/") or ".." in member_path.parts:
                        findings.append({"severity": "error", "code": "unsafe_backup_path", "member": member.name, "message": "Backup contains a path traversal entry"})
                        break
                if not members:
                    findings.append({"severity": "error", "code": "backup_empty_archive", "message": "Backup archive contains no entries"})
        except (OSError, tarfile.TarError) as exc:
            findings.append({"severity": "error", "code": "invalid_backup_archive", "message": str(exc)})
    elif suffixes.endswith(".zst"):
        import subprocess
        try:
            result = subprocess.run(["zstd", "-t", str(artifact)], capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                findings.append({"severity": "error", "code": "invalid_backup_compression", "message": result.stderr.strip() or "zstd validation failed"})
        except (OSError, subprocess.SubprocessError) as exc:
            findings.append({"severity": "error", "code": "backup_validator_unavailable", "message": "zstd validation tool is unavailable or failed", "error": str(exc)})
    else:
        findings.append({"severity": "warning", "code": "unknown_backup_format", "message": "Backup format is not recognized"})

    return {"ok": not any(x.get("severity") == "error" for x in findings), "findings": findings, "path": str(artifact), "size_bytes": artifact.stat().st_size}

def repair_reliability(podman, root: str | Path, action: str, *, name: str,
                       approved: bool = False) -> dict[str, Any]:
    """Perform one narrowly-scoped reliability repair after explicit approval."""
    if not approved:
        return {"status": "approval_required", "action": action, "name": name}
    if action == "remove_orphan_container":
        scan = scan_reliability(podman, root)
        orphan_names = {x.get("name") for x in scan["findings"] if x.get("code") == "orphan_container"}
        if name not in orphan_names:
            raise ReliabilityError("Container is not a detected Servora orphan")
        result = podman.remove_container(name, force=True)
        return {"status": "repaired", "action": action, "name": name, "result": result}
    if action == "restore_missing_app":
        apps = Path(root) / "apps"
        manifest_path = apps / f"{name}.json"
        if not manifest_path.exists():
            raise ReliabilityError("App manifest not found")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = validate_app_manifest(raw)
        except (OSError, json.JSONDecodeError, AppManifestError) as exc:
            raise ReliabilityError("App manifest is corrupted and cannot be restored safely") from exc
        expected = {service_container_name(manifest.name, service.name) for service in manifest.services}
        actual = {
            x for item in podman.list_containers(all=True)
            for x in ((item.get("Names") if isinstance(item.get("Names"), list) else [item.get("Names") or item.get("Name") or item.get("name")]))
            if isinstance(x, str)
        }
        missing = sorted(expected - actual)
        if not missing:
            return {"status": "nothing_to_do", "action": action, "name": name}
        from .apps import install_app
        created = install_app(podman, raw, transaction_root=root)
        return {"status": "repaired", "action": action, "name": name, "created": created}
    raise ReliabilityError("Unsupported reliability repair action")
