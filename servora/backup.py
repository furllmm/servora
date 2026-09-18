from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .apps import AppStore, manifest_to_dict
from .reliability import validate_backup_artifact


class BackupError(ValueError):
    pass


FORMAT_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    p = Path(name)
    return not name.startswith("/") and ".." not in p.parts


def _write_tar(source_root: Path, output_tar: Path) -> None:
    with tarfile.open(output_tar, "w") as archive:
        for path in sorted(source_root.rglob("*")):
            rel = path.relative_to(source_root).as_posix()
            if not _safe_member(rel):
                raise BackupError(f"Unsafe backup path: {rel}")
            archive.add(path, arcname=rel, recursive=False)


def _compress_zstd(source: Path, target: Path) -> None:
    try:
        with target.open("wb") as out:
            result = subprocess.run(["zstd", "-q", "-c", str(source)], stdout=out, stderr=subprocess.PIPE, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError("zstd is required to create .srv.zst backups") from exc
    if result.returncode != 0:
        raise BackupError(result.stderr.strip() or "zstd compression failed")


def _decompress_zstd(source: Path, target: Path) -> None:
    try:
        with target.open("wb") as out:
            result = subprocess.run(["zstd", "-q", "-d", "-c", str(source)], stdout=out, stderr=subprocess.PIPE, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError("zstd is required to read .srv.zst backups") from exc
    if result.returncode != 0:
        raise BackupError(result.stderr.strip() or "zstd decompression failed")


def _podman_resources(podman) -> dict[str, Any]:
    return {
        "images": podman.list_images(),
        "networks": podman.list_networks(),
        "volumes": podman.list_volumes(),
        "containers": podman.list_containers(all=True),
    }


def create_backup(runtime_root: str | Path, output: str | Path, podman, *, include_volumes: bool = True) -> dict[str, Any]:
    """Create a portable Servora .srv.zst backup without stopping containers."""
    root = Path(runtime_root)
    destination = Path(output)
    if destination.suffix != ".zst" or not destination.name.endswith(".srv.zst"):
        raise BackupError("Backup filename must end with .srv.zst")
    if not root.exists():
        raise BackupError("Servora runtime does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="servora-backup-") as temp:
        staging = Path(temp)
        apps = AppStore(root)
        manifest = {
            "format": "servora-backup",
            "format_version": FORMAT_VERSION,
            "servora_version": __import__("servora").__version__,
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "resources": _podman_resources(podman),
            "apps": [manifest_to_dict(app) for app in apps.list()],
            "volumes_included": include_volumes,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        for relative in ("config", "metadata", "logs", "apps"):
            source = root / relative
            if source.exists():
                shutil.copytree(source, staging / relative)

        if include_volumes:
            volume_dir = staging / "volumes"
            volume_dir.mkdir()
            for item in manifest["resources"]["volumes"]:
                name = item.get("Name") or item.get("name")
                if not name:
                    continue
                try:
                    inspected = podman.inspect_volume(name)
                except AttributeError:
                    inspected = None
                if not isinstance(inspected, dict):
                    continue
                mountpoint = inspected.get("Mountpoint")
                if mountpoint and Path(mountpoint).is_dir():
                    target = volume_dir / name
                    shutil.copytree(mountpoint, target, symlinks=True)

        tar_path = staging / "bundle.tar"
        _write_tar(staging, tar_path)
        _compress_zstd(tar_path, destination)

    checksum = _sha256(destination)
    validation = validate_backup_artifact(destination, checksum=checksum)
    if not validation["ok"]:
        raise BackupError(json.dumps(validation))
    return {"path": str(destination), "size_bytes": destination.stat().st_size, "sha256": checksum, "validation": validation}


def inspect_backup(path: str | Path) -> dict[str, Any]:
    """Validate a backup and return its manifest without extracting it."""
    artifact = Path(path)
    validation = validate_backup_artifact(artifact)
    if not validation["ok"]:
        raise BackupError(json.dumps(validation))
    with tempfile.TemporaryDirectory(prefix="servora-backup-inspect-") as temp:
        tar_path = Path(temp) / "bundle.tar"
        _decompress_zstd(artifact, tar_path)
        with tarfile.open(tar_path, "r") as archive:
            try:
                member = archive.getmember("manifest.json")
            except KeyError as exc:
                raise BackupError("Backup manifest is missing") from exc
            if not _safe_member(member.name):
                raise BackupError("Unsafe manifest path")
            data = json.loads(archive.extractfile(member).read().decode("utf-8"))
            if data.get("format") != "servora-backup" or data.get("format_version") != FORMAT_VERSION:
                raise BackupError("Unsupported Servora backup format")
            return {"manifest": data, "validation": validation}


def restore_preview(path: str | Path) -> dict[str, Any]:
    """Return a safe restore preview; no Podman or filesystem state is changed."""
    inspected = inspect_backup(path)
    manifest = inspected["manifest"]
    return {
        "format": manifest["format"],
        "format_version": manifest["format_version"],
        "servora_version": manifest.get("servora_version"),
        "apps": [x.get("name") for x in manifest.get("apps", [])],
        "volume_count": len(manifest.get("resources", {}).get("volumes", [])) if manifest.get("volumes_included") else 0,
        "volumes_included": bool(manifest.get("volumes_included")),
        "resource_counts": {
            key: len(manifest.get("resources", {}).get(key, []))
            for key in ("containers", "images", "networks", "volumes")
        },
        "sha256": _sha256(Path(path)),
    }
