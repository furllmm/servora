"""Persistent runtime image state for installed Servora apps."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .apps import AppManifest, service_container_name, resolve_service_order

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



def check_app_updates(podman, manifest: AppManifest, root: str | Path) -> dict[str, Any]:
    """Refresh each unique app image reference and report update candidates.

    This only refreshes the local image store; running containers are not changed.
    """
    before_state = load_app_state(root, manifest.name)
    unique_images = sorted({service.image for service in manifest.services})
    refreshed = []
    for image in unique_images:
        result = podman.refresh_image(image)
        refreshed.append(result)
    status = image_status(podman, manifest, root)
    return {
        "app": manifest.name,
        "version": manifest.version,
        "status": status["status"],
        "refreshed": refreshed,
        "services": status["services"],
        "deployment_recorded": before_state is not None,
    }


def update_app_images(podman, manifest: AppManifest, root: str | Path) -> dict[str, Any]:
    """Safely recreate only services whose recorded image has changed.

    The update is image-focused: volumes, networks and the app manifest are
    preserved. Only containers backed by a changed image are recreated.
    The previous image IDs are retained for rollback.
    """
    state = load_app_state(root, manifest.name)
    if not state:
        raise ValueError("Deployment state is not recorded; automatic image update is unavailable")

    status = image_status(podman, manifest, root)
    if status["status"] in {"not_recorded", "unknown"}:
        raise ValueError(f"Automatic image update requires a known deployment state: {status['status']}")

    changed = [item for item in status["services"] if item["status"] == "update_available"]
    if not changed:
        return {
            "app": manifest.name,
            "version": manifest.version,
            "status": "unchanged",
            "updated_services": [],
            "rollback": {"attempted": False, "success": None},
        }

    by_service = {service.name: service for service in manifest.services}
    recorded = {item.get("service"): item for item in state.get("services", [])}
    ordered = {service.name: index for index, service in enumerate(
        resolve_service_order(manifest)
    )}
    changed.sort(key=lambda item: ordered[item["service"]])

    original_running: dict[str, bool] = {}
    for item in changed:
        name = service_container_name(manifest.name, item["service"])
        try:
            original_running[name] = bool(podman.container_health(name).get("running"))
        except Exception:
            original_running[name] = False

    def plan(service: Any, image: str) -> dict[str, Any]:
        return {
            "name": service_container_name(manifest.name, service.name),
            "image": image,
            "ports": service.ports,
            "volumes": [
                {"name": v.name, "container_path": v.container_path, "read_only": v.read_only}
                for v in service.volumes
            ],
            "environment": service.environment,
            "networks": service.networks,
            "command": service.command,
        }

    changed_names = {service_container_name(manifest.name, item["service"]) for item in changed}
    created: list[str] = []
    started: list[str] = []
    stopped: list[str] = []

    try:
        # Stop dependents first, then remove only changed containers.
        for service in reversed(resolve_service_order(manifest)):
            name = service_container_name(manifest.name, service.name)
            if name not in changed_names:
                continue
            if original_running.get(name):
                podman.stop_container(name)
                stopped.append(name)
            podman.remove_container(name, force=True)

        # Recreate dependency-first from the freshly refreshed image tags.
        for service in resolve_service_order(manifest):
            name = service_container_name(manifest.name, service.name)
            if name not in changed_names:
                continue
            podman.create_container(plan(service, service.image))
            created.append(name)

        for service in __import__("servora.apps", fromlist=["resolve_service_order"]).resolve_service_order(manifest):
            name = service_container_name(manifest.name, service.name)
            if name not in changed_names or not original_running.get(name):
                continue
            podman.start_container(name)
            started.append(name)
            health = podman.container_health(name)
            if health.get("status") in {"unhealthy", "dead", "exited", "stopped"}:
                raise RuntimeError(f"Updated service {service.name} is not running: {health.get('status')}")

        new_state = capture_app_state(podman, manifest, root)
        return {
            "app": manifest.name,
            "version": manifest.version,
            "status": "updated",
            "updated_services": [item["service"] for item in changed],
            "deployment": new_state,
            "rollback": {"attempted": False, "success": None},
        }
    except Exception as update_error:
        rollback = {"attempted": True, "success": False, "error": None}
        try:
            for name in reversed(started):
                try:
                    podman.stop_container(name)
                except Exception:
                    pass
            for name in reversed(created):
                try:
                    podman.remove_container(name, force=True)
                except Exception:
                    pass

            # Restore the exact image IDs recorded before the update.
            for service in __import__("servora.apps", fromlist=["resolve_service_order"]).resolve_service_order(manifest):
                name = service_container_name(manifest.name, service.name)
                if name not in changed_names:
                    continue
                old = recorded.get(service.name) or {}
                old_image = old.get("image_id")
                if not old_image:
                    raise RuntimeError(f"No recorded image ID for rollback of {service.name}")
                podman.create_container(plan(service, old_image))
            for service in __import__("servora.apps", fromlist=["resolve_service_order"]).resolve_service_order(manifest):
                name = service_container_name(manifest.name, service.name)
                if name not in changed_names or not original_running.get(name):
                    continue
                podman.start_container(name)
                health = podman.container_health(name)
                if health.get("status") in {"unhealthy", "dead", "exited", "stopped"}:
                    raise RuntimeError(f"Rollback service {service.name} is not running: {health.get('status')}")

            # Do not overwrite the recorded deployment state until an update
            # succeeds; it therefore remains a record of the pre-update image.
            rollback["success"] = True
        except Exception as rollback_error:
            rollback["error"] = str(rollback_error)
        raise RuntimeError(
            f"Image update failed; rollback {'succeeded' if rollback['success'] else 'failed'}: {update_error}"
        ) from update_error

def build_app_update_preview(podman, manifest: AppManifest, root: str | Path) -> dict[str, Any]:
    """Build a read-only update plan from recorded and current image state.

    The function never pulls images, changes containers, or writes deployment state.
    """
    status = image_status(podman, manifest, root)
    services = []
    for item in status["services"]:
        service = {**item, "action": "none", "requires_update": item["status"] == "update_available"}
        if item["status"] == "update_available":
            service["action"] = "recreate"
        elif item["status"] == "not_recorded":
            service["action"] = "record"
        elif item["status"] == "unknown":
            service["action"] = "inspect"
        services.append(service)

    update_services = [x for x in services if x["requires_update"]]
    return {
        "app": manifest.name,
        "version": manifest.version,
        "status": status["status"],
        "requires_update": bool(update_services),
        "update_service_count": len(update_services),
        "summary": (
            f"{len(update_services)} service(s) have a newer local image"
            if update_services else "No image update is currently detected"
        ),
        "services": services,
    }

