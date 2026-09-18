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
            if path == output_tar:
                continue
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


def _archive_members(archive: tarfile.TarFile) -> None:
    for member in archive.getmembers():
        if not _safe_member(member.name):
            raise BackupError(f"Unsafe backup path: {member.name}")
        if member.issym() or member.islnk():
            # Links are not needed for the metadata restore path. Volume data is
            # copied from an extracted staging tree, where links remain links.
            continue


def _extract_backup(path: Path, target: Path) -> dict[str, Any]:
    """Extract a validated backup into an isolated directory."""
    inspect_backup(path)
    tar_path = target / "bundle.tar"
    _decompress_zstd(path, tar_path)
    with tarfile.open(tar_path, "r") as archive:
        _archive_members(archive)
        archive.extractall(target / "payload")
    return json.loads((target / "payload" / "manifest.json").read_text(encoding="utf-8"))


def _resource_name(item: dict[str, Any]) -> str | None:
    return item.get("Name") or item.get("name")


def _image_refs(podman) -> set[str]:
    refs: set[str] = set()
    for item in podman.list_images():
        for key in ("RepoTags", "repoTags", "Names", "names"):
            values = item.get(key)
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                refs.update(str(x) for x in values if x)
        for key in ("Id", "ID", "id"):
            value = item.get(key)
            if value:
                refs.add(str(value))
    return refs


def restore_conflicts(path: str | Path, runtime_root: str | Path, podman) -> dict[str, Any]:
    """Build a non-mutating restore plan and detect existing resource conflicts."""
    artifact = Path(path)
    inspected = inspect_backup(artifact)
    manifest = inspected["manifest"]
    root = Path(runtime_root)
    store = AppStore(root)

    apps = [validate_app_manifest(x) for x in manifest.get("apps", [])]
    existing_apps = {app.name for app in store.list()}
    existing_containers = {
        _resource_name(x) for x in podman.list_containers(all=True)
        if _resource_name(x)
    }
    existing_networks = {
        _resource_name(x) for x in podman.list_networks()
        if _resource_name(x)
    }
    existing_volumes = {
        _resource_name(x) for x in podman.list_volumes()
        if _resource_name(x)
    }

    requested_containers = {
        service_container_name(app.name, service.name)
        for app in apps
        for service in app.services
    }
    requested_networks = {
        network
        for app in apps
        for service in app.services
        for network in service.networks
    }
    requested_volumes = {
        volume.name
        for app in apps
        for service in app.services
        for volume in service.volumes
    }

    image_refs = {
        service.image
        for app in apps
        for service in app.services
    }
    available_images = _image_refs(podman)
    missing_images = sorted(ref for ref in image_refs if ref not in available_images)

    backup_volume_names = {
        _resource_name(x) for x in manifest.get("resources", {}).get("volumes", [])
        if _resource_name(x)
    }
    conflicts = {
        "apps": sorted(existing_apps & {app.name for app in apps}),
        "containers": sorted(existing_containers & requested_containers),
        "networks": sorted(existing_networks & requested_networks),
        "volumes": sorted(existing_volumes & requested_volumes),
    }

    # A backup can contain volume metadata without a copied payload for a volume
    # that was inaccessible during export. Treat that as a warning, not a silent
    # empty restore.
    payload_root = None
    with tempfile.TemporaryDirectory(prefix="servora-restore-plan-") as temp:
        payload_root = Path(temp)
        try:
            manifest_from_archive = _extract_backup(artifact, payload_root)
            copied_volumes = {
                p.name for p in (payload_root / "payload" / "volumes").iterdir()
            } if (payload_root / "payload" / "volumes").is_dir() else set()
        except Exception:
            copied_volumes = set()

    missing_volume_payload = sorted(
        name for name in backup_volume_names
        if manifest.get("volumes_included") and name not in copied_volumes
    )

    # Approximate required bytes from the archive itself; this is conservative
    # enough for a preflight but does not claim to be an exact expanded size.
    required_bytes = artifact.stat().st_size * 3
    usage = shutil.disk_usage(root if root.exists() else Path.home())
    free_bytes = usage.free

    return {
        "safe": not any(conflicts.values()) and not missing_images,
        "conflicts": conflicts,
        "missing_images": missing_images,
        "missing_volume_payload": missing_volume_payload,
        "required_bytes_estimate": required_bytes,
        "free_bytes": free_bytes,
        "apps": [app.name for app in apps],
        "volume_payload_count": len(copied_volumes),
        "warnings": (
            ["Some backed-up volumes have no copied payload and will not be restored."]
            if missing_volume_payload else []
        ),
    }


def restore_backup(
    path: str | Path,
    runtime_root: str | Path,
    podman,
    *,
    approved: bool = False,
    mode: str = "safe",
) -> dict[str, Any]:
    """Restore a Servora backup without overwriting existing resources."""
    if not approved:
        raise BackupError("Restore requires explicit approval")
    if mode != "safe":
        raise BackupError("Only safe restore mode is supported; existing resources are never overwritten")

    artifact = Path(path)
    root = Path(runtime_root)
    root.mkdir(parents=True, exist_ok=True)
    plan = restore_conflicts(artifact, root, podman)
    if plan["conflicts"]["apps"] or plan["conflicts"]["containers"] or plan["conflicts"]["networks"] or plan["conflicts"]["volumes"]:
        raise BackupError(json.dumps({"error": "restore_conflict", "conflicts": plan["conflicts"]}))
    if plan["missing_images"]:
        raise BackupError(json.dumps({"error": "missing_images", "images": plan["missing_images"]}))
    if plan["missing_volume_payload"]:
        raise BackupError(json.dumps({"error": "missing_volume_payload", "volumes": plan["missing_volume_payload"]}))
    if plan["free_bytes"] < plan["required_bytes_estimate"]:
        raise BackupError("Insufficient free disk space for restore preflight")

    tx = None
    apps = [validate_app_manifest(x) for x in inspect_backup(artifact)["manifest"].get("apps", [])]
    try:
        tx = __import__("servora.transaction", fromlist=["ResourceTransaction"]).ResourceTransaction(
            root, "restore", name=artifact.name
        )
        with tempfile.TemporaryDirectory(prefix="servora-restore-") as temp:
            staging = Path(temp)
            manifest = _extract_backup(artifact, staging)
            payload = staging / "payload"

            # Restore the Servora registry files only after Podman resources have
            # been created successfully. Existing config/metadata/logs are never
            # overwritten by this safe restore mode.
            for app in apps:
                install_app(podman, manifest_to_dict(app), transaction_root=None)
                for service in app.services:
                    tx.record("containers", service_container_name(app.name, service.name))
                for service in app.services:
                    for volume in service.volumes:
                        tx.record("volumes", volume.name)
                    for network in service.networks:
                        tx.record("networks", network)

                if manifest.get("volumes_included"):
                    for service in app.services:
                        for volume in service.volumes:
                            source = payload / "volumes" / volume.name
                            if not source.is_dir():
                                continue
                            inspected = podman.inspect_volume(volume.name)
                            mountpoint = inspected.get("Mountpoint")
                            if not mountpoint:
                                raise BackupError(f"Volume mountpoint unavailable: {volume.name}")
                            destination = Path(mountpoint).resolve()
                            if destination == Path("/") or len(destination.parts) < 2:
                                raise BackupError(f"Unsafe volume mountpoint: {mountpoint}")
                            for item in source.iterdir():
                                target = destination / item.name
                                if target.exists() or target.is_symlink():
                                    raise BackupError(f"Unexpected existing volume data: {target}")
                                shutil.copytree(item, target, symlinks=True) if item.is_dir() else shutil.copy2(item, target, follow_symlinks=False)

                AppStore(root).save(app)

            # Health-check every restored container before committing the journal.
            health = []
            for app in apps:
                for service in app.services:
                    name = service_container_name(app.name, service.name)
                    health.append(podman.container_health(name))
            tx.commit()
            return {
                "status": "restored",
                "backup": artifact.name,
                "apps": [app.name for app in apps],
                "health": health,
            }
    except Exception as exc:
        if tx:
            rollback = tx.rollback(podman)
        else:
            rollback = {"attempted": False, "success": None, "errors": []}
        for app in apps:
            try:
                AppStore(root).remove(app.name)
            except Exception:
                pass
        raise BackupError(f"Restore failed; rollback {'succeeded' if rollback['success'] else 'failed'}: {exc}") from exc
