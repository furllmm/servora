"""Persistent runtime image state for installed Servora apps."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .apps import AppManifest, service_container_name

def _image_id(data: dict[str, Any]) -> str | None:
    value = data.get("Image") or data.get("ImageID") or data.get("ImageId")
    return str(value) if value else None


def _image_digest(data: dict[str, Any]) -> str | None:
    value = data.get("Digest") or data.get("ImageDigest") or data.get("digest")
    if not value:
        repo_digests = data.get("RepoDigests") or data.get("repo_digests") or []
        if isinstance(repo_digests, str):
            repo_digests = [repo_digests]
        if repo_digests:
            first = str(repo_digests[0])
            value = first.split("@", 1)[1] if "@" in first else None
    return str(value) if value else None

def _path(root: str | Path, app_name: str) -> Path:
    directory = Path(root) / "metadata" / "deployments"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{app_name}.json"

def save_app_state(root: str | Path, app_name: str, state: dict[str, Any]) -> None:
    target = _path(root, app_name)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)

def load_app_state(root: str | Path, app_name: str) -> dict[str, Any] | None:
    target = _path(root, app_name)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))

def capture_app_state(podman, manifest: AppManifest, root: str | Path) -> dict[str, Any]:
    services = []
    for service in manifest.services:
        name = service_container_name(manifest.name, service.name)
        data = podman.inspect_container(name)
        image_data = podman.image_metadata(service.image)
        services.append({"service": service.name, "container": name,
                         "image": service.image, "image_id": _image_id(data),
                         "image_digest": _image_digest(image_data)})
    state = {"app": manifest.name, "version": manifest.version, "services": services}
    save_app_state(root, manifest.name, state)
    return state

def image_status(podman, manifest: AppManifest, root: str | Path) -> dict[str, Any]:
    state = load_app_state(root, manifest.name)
    services = []
    for service in manifest.services:
        recorded = next((x for x in (state or {}).get("services", [])
                         if x.get("service") == service.name), None)
        try:
            metadata = podman.image_metadata(service.image)
            current_id = metadata.get("id")
            current_digest = metadata.get("digest") or _image_digest(metadata)
        except Exception as exc:
            services.append({"service": service.name, "image": service.image,
                             "status": "unknown", "error": str(exc)})
            continue
        recorded_id = recorded.get("image_id") if recorded else None
        recorded_digest = recorded.get("image_digest") if recorded else None
        if not recorded_id and not recorded_digest:
            status = "not_recorded"
        elif not current_id and not current_digest:
            status = "unknown"
        elif (recorded_id and current_id == recorded_id) or (recorded_digest and current_digest == recorded_digest):
            status = "current"
        else:
            status = "update_available"
        services.append({"service": service.name, "image": service.image,
                         "recorded_image_id": recorded_id,
                         "current_image_id": current_id,
                         "recorded_image_digest": recorded_digest,
                         "current_image_digest": current_digest,
                         "status": status})
    overall = ("update_available" if any(x["status"] == "update_available" for x in services)
               else "unknown" if any(x["status"] == "unknown" for x in services)
               else "not_recorded" if any(x["status"] == "not_recorded" for x in services)
               else "current")
    return {"app": manifest.name, "version": manifest.version,
            "status": overall, "services": services}
