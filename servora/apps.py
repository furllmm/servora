from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
import json
from pathlib import Path

from .transaction import ResourceTransaction


class AppManifestError(ValueError):
    pass


_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_IMAGE = re.compile(r"^[^\s]+$")


@dataclass(frozen=True)
class AppVolume:
    name: str
    container_path: str
    read_only: bool = False


@dataclass(frozen=True)
class AppService:
    name: str
    image: str
    ports: list[dict[str, Any]] = field(default_factory=list)
    volumes: list[AppVolume] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    networks: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AppManifest:
    name: str
    version: str
    description: str
    services: list[AppService]
    metadata: dict[str, Any] = field(default_factory=dict)


def _require_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise AppManifestError(f"Invalid {label}")
    return value


def _parse_port(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AppManifestError("Port mapping must be an object")
    try:
        host, container = int(raw["host"]), int(raw["container"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AppManifestError("Port mapping needs numeric host and container") from exc
    protocol = raw.get("protocol", "tcp")
    if protocol not in {"tcp", "udp", "sctp"}:
        raise AppManifestError("Invalid port protocol")
    if not 1 <= host <= 65535 or not 1 <= container <= 65535:
        raise AppManifestError("Invalid port")
    return {"host": host, "container": container, "protocol": protocol}


def _parse_volume(raw: Any) -> AppVolume:
    if isinstance(raw, str):
        parts = raw.split(":")
        if len(parts) not in (2, 3):
            raise AppManifestError("Invalid volume mapping")
        name, path = parts[0], parts[1]
        read_only = len(parts) == 3 and parts[2] == "ro"
        if len(parts) == 3 and parts[2] not in {"ro", "rw"}:
            raise AppManifestError("Invalid volume mode")
    elif isinstance(raw, dict):
        name, path = raw.get("name"), raw.get("container_path")
        read_only = bool(raw.get("read_only", False))
    else:
        raise AppManifestError("Volume mapping must be an object or string")
    _require_name(name, "volume name")
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise AppManifestError("Container volume path must be an absolute path")
    return AppVolume(name, path, read_only)


def validate_app_manifest(raw: dict[str, Any]) -> AppManifest:
    if not isinstance(raw, dict):
        raise AppManifestError("Manifest must be an object")
    name = _require_name(raw.get("name", ""), "app name")
    version = raw.get("version", "1.0")
    if not isinstance(version, str) or not version.strip():
        raise AppManifestError("Invalid app version")
    services = raw.get("services")
    if not isinstance(services, list) or not services:
        raise AppManifestError("At least one service is required")

    service_names: set[str] = set()
    parsed: list[AppService] = []
    for raw_service in services:
        if not isinstance(raw_service, dict):
            raise AppManifestError("Service must be an object")
        service_name = _require_name(raw_service.get("name", ""), "service name")
        if service_name in service_names:
            raise AppManifestError(f"Duplicate service: {service_name}")
        service_names.add(service_name)
        image = raw_service.get("image", "")
        if not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise AppManifestError(f"Invalid image for service {service_name}")

        environment = raw_service.get("environment", {})
        if not isinstance(environment, dict):
            raise AppManifestError("Environment must be an object")
        clean_env: dict[str, str] = {}
        for key, value in environment.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise AppManifestError("Invalid environment variable name")
            if isinstance(value, (dict, list)):
                raise AppManifestError(f"Environment value must be scalar: {key}")
            clean_env[key] = str(value)

        command = raw_service.get("command", [])
        if isinstance(command, str):
            command = [command]
        if not isinstance(command, list) or not all(isinstance(x, str) and "\x00" not in x for x in command):
            raise AppManifestError("Command must be a list of strings")

        networks = raw_service.get("networks", [])
        if not isinstance(networks, list) or not all(isinstance(x, str) and _NAME.fullmatch(x) for x in networks):
            raise AppManifestError("Invalid service networks")
        if len(networks) != len(set(networks)):
            raise AppManifestError(f"Duplicate network in service {service_name}")

        depends_on = raw_service.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(x, str) for x in depends_on):
            raise AppManifestError("depends_on must be a list")

        ports = [_parse_port(p) for p in raw_service.get("ports", [])]
        seen_ports: set[tuple[int, str]] = set()
        for p in ports:
            key = (p["host"], p["protocol"])
            if key in seen_ports:
                raise AppManifestError(f"Duplicate host port: {p['host']}/{p['protocol']}")
            seen_ports.add(key)
        volumes = [_parse_volume(v) for v in raw_service.get("volumes", [])]
        if len({v.name for v in volumes}) != len(volumes):
            raise AppManifestError(f"Duplicate volume in service {service_name}")

        parsed.append(AppService(service_name, image, ports, volumes, clean_env, networks, command, depends_on))

    for service in parsed:
        unknown = set(service.depends_on) - service_names
        if unknown:
            raise AppManifestError(f"Unknown dependency: {sorted(unknown)[0]}")

    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise AppManifestError("metadata must be an object")
    return AppManifest(name, version, str(raw.get("description", "")), parsed, metadata)


def manifest_to_dict(manifest: AppManifest) -> dict[str, Any]:
    return {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "metadata": manifest.metadata,
        "services": [
            {
                "name": s.name,
                "image": s.image,
                "ports": s.ports,
                "volumes": [{"name": v.name, "container_path": v.container_path, "read_only": v.read_only} for v in s.volumes],
                "environment": s.environment,
                "networks": s.networks,
                "command": s.command,
                "depends_on": s.depends_on,
            }
            for s in manifest.services
        ],
    }


def service_container_name(app_name: str, service_name: str) -> str:
    _require_name(app_name, "app name")
    _require_name(service_name, "service name")
    return f"servora-{app_name}-{service_name}"


def resolve_service_order(manifest: AppManifest) -> list[AppService]:
    """Return a deterministic dependency-first service order."""
    by_name = {s.name: s for s in manifest.services}
    state: dict[str, int] = {name: 0 for name in by_name}
    order: list[AppService] = []

    def visit(name: str, chain: list[str]) -> None:
        if state[name] == 2:
            return
        if state[name] == 1:
            cycle = chain[chain.index(name):] + [name]
            raise AppManifestError("Circular service dependency: " + " -> ".join(cycle))
        state[name] = 1
        for dep in sorted(by_name[name].depends_on):
            visit(dep, chain + [name])
        state[name] = 2
        order.append(by_name[name])

    for name in sorted(by_name):
        visit(name, [])
    return order


def _resource_users(info: Any) -> set[str]:
    """Extract container names/IDs referenced by a Podman resource inspect result."""
    users: set[str] = set()
    if isinstance(info, dict):
        for key, value in info.items():
            if key in {"Name", "Names"}:
                if isinstance(value, list):
                    users.update(str(x) for x in value if x)
                elif value:
                    users.add(str(value))
            elif key == "Containers" and isinstance(value, dict):
                for container_id, details in value.items():
                    if isinstance(details, dict):
                        name = details.get("Name") or details.get("Names")
                        if isinstance(name, list):
                            users.update(str(x) for x in name if x)
                        elif name:
                            users.add(str(name))
                    elif container_id:
                        users.add(str(container_id))
            else:
                users.update(_resource_users(value))
    elif isinstance(info, list):
        for item in info:
            users.update(_resource_users(item))
    return users


def _check_resource_conflict(podman, resource_type: str, name: str, app_name: str) -> None:
    inspect = getattr(podman, f"inspect_{resource_type}", None)
    if inspect is None:
        return
    try:
        info = inspect(name)
    except Exception:
        return
    prefix = f"servora-{app_name}-"
    conflicts = sorted(x for x in _resource_users(info) if x and not x.startswith(prefix))
    if conflicts:
        raise AppManifestError(
            f"{resource_type.capitalize()} '{name}' is already used by another container: {conflicts[0]}"
        )



def _ensure_images(podman, manifest: AppManifest) -> list[dict[str, Any]]:
    """Ensure every service image is locally available before mutating app resources."""
    image_exists = getattr(podman, "image_exists", None)
    pull_image = getattr(podman, "pull_image", None)
    if image_exists is None or pull_image is None:
        return [{"image": image, "status": "unchecked"} for image in sorted({s.image for s in manifest.services})]
    results: list[dict[str, Any]] = []
    for image in sorted({s.image for s in manifest.services}):
        if image_exists(image):
            results.append({"image": image, "status": "present"})
            continue
        result = pull_image(image)
        results.append({"image": image, "status": "pulled", "result": result})
    return results

def install_app(podman, raw_manifest: dict[str, Any], check_ports: bool = True,
               transaction_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Create an app stack with a persistent rollback journal.

    Only networks/volumes/containers created by this operation are rolled back.
    Existing shared resources are preserved.
    """
    manifest = validate_app_manifest(raw_manifest)
    tx = ResourceTransaction(transaction_root, "install", name=manifest.name) if transaction_root else None
    created: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        image_results = _ensure_images(podman, manifest)
        existing_networks = {
            (x.get("Name") or x.get("name")) for x in podman.list_networks()
        }
        all_networks = sorted({n for s in manifest.services for n in s.networks})
        for network in all_networks:
            if network not in existing_networks:
                podman.create_network(network)
                if tx:
                    tx.record("networks", network)
            else:
                _check_resource_conflict(podman, "network", network, manifest.name)

        existing_volumes = {
            (x.get("Name") or x.get("name")) for x in podman.list_volumes()
        }
        all_volumes = sorted({v.name for s in manifest.services for v in s.volumes})
        for volume in all_volumes:
            if volume not in existing_volumes:
                podman.create_volume(volume)
                if tx:
                    tx.record("volumes", volume)
            else:
                _check_resource_conflict(podman, "volume", volume, manifest.name)

        for service in resolve_service_order(manifest):
            name = service_container_name(manifest.name, service.name)
            if check_ports:
                from .service import ensure_port_is_available
                for port in service.ports:
                    ensure_port_is_available(port["host"])
            plan = {
                "name": name,
                "image": service.image,
                "ports": service.ports,
                "volumes": [{"name": v.name, "container_path": v.container_path, "read_only": v.read_only} for v in service.volumes],
                "environment": service.environment,
                "networks": service.networks,
                "command": service.command,
            }
            result = podman.create_container(plan)
            created.append(name)
            if tx:
                tx.record("containers", name)
            results.append({"service": service.name, "container": name, "result": result, "images": image_results})
        if tx:
            tx.commit()
    except Exception:
        if tx:
            tx.rollback(podman)
        else:
            for name in reversed(created):
                try:
                    podman.remove_container(name, force=True)
                except Exception:
                    pass
        raise
    return results


def _installed_container_names(podman) -> set[str]:
    """Return names of containers currently known to Podman."""
    names: set[str] = set()
    for item in podman.list_containers(all=True):
        name = item.get("Names") or item.get("Name") or item.get("name")
        if isinstance(name, list):
            names.update(str(x) for x in name if x)
        elif name:
            names.add(str(name))
    return names


def _app_operation_result(manifest: AppManifest, action: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [x for x in results if x["status"] == "failed"]
    missing = [x for x in results if x["status"] == "missing"]
    status = "failed" if failed else ("partial" if missing else "ok")
    return {"app": manifest.name, "action": action, "status": status, "services": results}


def start_app(podman, manifest: AppManifest) -> dict[str, Any]:
    """Start an installed app in deterministic dependency-first order."""
    names = _installed_container_names(podman)
    results = []
    for service in resolve_service_order(manifest):
        name = service_container_name(manifest.name, service.name)
        if name not in names:
            results.append({"service": service.name, "container": name, "status": "missing"})
            continue
        try:
            result = podman.start_container(name)
            results.append({"service": service.name, "container": name, "status": "started", "result": result})
        except Exception as exc:
            results.append({"service": service.name, "container": name, "status": "failed", "error": str(exc)})
    return _app_operation_result(manifest, "start", results)


def stop_app(podman, manifest: AppManifest) -> dict[str, Any]:
    """Stop an installed app in reverse dependency order."""
    names = _installed_container_names(podman)
    results = []
    for service in reversed(resolve_service_order(manifest)):
        name = service_container_name(manifest.name, service.name)
        if name not in names:
            results.append({"service": service.name, "container": name, "status": "missing"})
            continue
        try:
            result = podman.stop_container(name)
            results.append({"service": service.name, "container": name, "status": "stopped", "result": result})
        except Exception as exc:
            results.append({"service": service.name, "container": name, "status": "failed", "error": str(exc)})
    return _app_operation_result(manifest, "stop", results)


def restart_app(podman, manifest: AppManifest) -> dict[str, Any]:
    """Restart an installed app using explicit reverse-stop/forward-start semantics."""
    stopped = stop_app(podman, manifest)
    if stopped["status"] == "failed":
        return {"app": manifest.name, "action": "restart", "status": "failed", "stop": stopped,
                "start": {"app": manifest.name, "action": "start", "status": "skipped", "services": []}}
    started = start_app(podman, manifest)
    status = "failed" if started["status"] == "failed" else (
        "partial" if stopped["status"] == "partial" or started["status"] == "partial" else "ok"
    )
    return {"app": manifest.name, "action": "restart", "status": status, "stop": stopped, "start": started}

def app_health(podman, manifest: AppManifest) -> dict[str, Any]:
    """Aggregate the runtime health of every service in an installed app."""
    services = []
    for service in manifest.services:
        name = service_container_name(manifest.name, service.name)
        try:
            health = podman.container_health(name)
        except Exception as exc:
            health = {"name": name, "status": "missing", "running": False, "healthcheck": None, "error": str(exc)}
        services.append({"service": service.name, **health})

    statuses = [x["status"] for x in services]
    if any(s == "unhealthy" for s in statuses):
        overall = "unhealthy"
    elif any(s in {"missing", "exited", "dead", "stopped"} for s in statuses):
        overall = "degraded"
    elif any(s == "starting" for s in statuses):
        overall = "starting"
    elif all(s in {"healthy", "running"} for s in statuses):
        overall = "healthy"
    else:
        overall = "unknown"
    return {"name": manifest.name, "version": manifest.version, "status": overall, "services": services}


class AppStore:
    """Persistent registry for Servora-managed apps."""
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "apps"
        self.path.mkdir(parents=True, exist_ok=True)

    def _file(self, name: str) -> Path:
        _require_name(name, "app name")
        return self.path / f"{name}.json"

    def save(self, manifest: AppManifest) -> None:
        target = self._file(manifest.name)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest_to_dict(manifest), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)

    def get(self, name: str) -> AppManifest | None:
        target = self._file(name)
        if not target.exists():
            return None
        return validate_app_manifest(json.loads(target.read_text(encoding="utf-8")))

    def list(self) -> list[AppManifest]:
        result = []
        for item in sorted(self.path.glob("*.json")):
            try:
                result.append(validate_app_manifest(json.loads(item.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, AppManifestError):
                continue
        return result

    def remove(self, name: str) -> None:
        self._file(name).unlink(missing_ok=True)


def uninstall_app(podman, manifest: AppManifest, *, remove_volumes: bool = False,
                  remove_networks: bool = False) -> dict[str, Any]:
    """Remove Servora-managed containers; persistent data is preserved by default."""
    removed = []
    for service in reversed(manifest.services):
        name = service_container_name(manifest.name, service.name)
        try:
            podman.remove_container(name, force=True)
            removed.append(name)
        except Exception:
            # Missing containers should not make uninstall fail.
            pass

    removed_volumes = []
    if remove_volumes:
        remove_volume = getattr(podman, "remove_volume", None)
        if remove_volume:
            for volume in sorted({v.name for s in manifest.services for v in s.volumes}):
                try:
                    remove_volume(volume, force=True)
                    removed_volumes.append(volume)
                except Exception:
                    pass

    # Networks are intentionally opt-in because they may be shared.
    removed_networks = []
    if remove_networks:
        remove_network = getattr(podman, "remove_network", None)
        if remove_network:
            for network in sorted({n for s in manifest.services for n in s.networks}):
                try:
                    remove_network(network)
                    removed_networks.append(network)
                except Exception:
                    pass
    return {"removed_containers": removed, "removed_volumes": removed_volumes,
            "removed_networks": removed_networks}


def update_app(podman, old_manifest: AppManifest, raw_manifest: dict[str, Any], *,
               store: AppStore | None = None, check_ports: bool = True,
               transaction_root: str | Path | None = None) -> dict[str, Any]:
    """Best-effort transactional update: old definition is restored if new install fails."""
    new_manifest = validate_app_manifest(raw_manifest)
    if new_manifest.name != old_manifest.name:
        raise AppManifestError("App name cannot change during update")
    # Prepare new images before removing the working old stack.
    try:
        _ensure_images(podman, new_manifest)
    except Exception as exc:
        raise AppManifestError(f"Update image preparation failed; existing app was preserved: {exc}") from exc
    uninstall_app(podman, old_manifest)
    try:
        created = install_app(podman, raw_manifest, check_ports=check_ports, transaction_root=transaction_root)
        if store:
            store.save(new_manifest)
        return {"app": manifest_to_dict(new_manifest), "created": created, "updated": True,
                "rollback": {"attempted": False, "success": None}}
    except Exception as update_error:
        rollback = {"attempted": True, "success": False, "error": None}
        try:
            install_app(podman, manifest_to_dict(old_manifest), check_ports=check_ports, transaction_root=transaction_root)
            if store:
                store.save(old_manifest)
            rollback["success"] = True
        except Exception as restore_error:
            rollback["error"] = str(restore_error)
        raise AppManifestError(f"Update failed; rollback {'succeeded' if rollback['success'] else 'failed'}: {update_error}") from update_error
