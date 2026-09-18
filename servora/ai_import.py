from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import re
from typing import Any
from urllib.parse import urlparse

from .apps import AppManifestError, manifest_to_dict, validate_app_manifest


class AIImportError(ValueError):
    pass


@dataclass(frozen=True)
class RiskFinding:
    severity: str
    code: str
    message: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ImportResult:
    manifest: dict[str, Any]
    source: dict[str, Any]
    findings: list[RiskFinding]
    requires_approval: bool
    engine: str = "servora-static-analyzer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "source": self.source,
            "findings": [f.to_dict() for f in self.findings],
            "requires_approval": self.requires_approval,
            "engine": self.engine,
        }


_IMAGE_RE = re.compile(r"^[^\s]+$")
_DANGEROUS_COMMANDS = {"sh", "bash", "ash", "zsh", "fish", "powershell", "pwsh", "cmd"}


def _clean_name(value: str, fallback: str = "imported-app") -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", str(value).lower()).strip("-_")
    value = value[:64]
    if not value or not re.match(r"^[a-z0-9]", value):
        value = fallback
    return value


def _port(raw: Any) -> dict[str, Any]:
    if isinstance(raw, int):
        return {"host": raw, "container": raw, "protocol": "tcp"}
    if isinstance(raw, str):
        proto = "tcp"
        value = raw
        if "/" in value:
            value, proto = value.rsplit("/", 1)
        bits = value.split(":")
        if len(bits) == 1:
            host = container = int(bits[0])
        elif len(bits) == 2:
            host, container = int(bits[0]), int(bits[1])
        else:
            # Compose may contain IP:host:container; ignore the bind address here.
            host, container = int(bits[-2]), int(bits[-1])
        return {"host": host, "container": container, "protocol": proto}
    if isinstance(raw, dict):
        return {"host": int(raw["published"]), "container": int(raw["target"]), "protocol": raw.get("protocol", "tcp")}
    raise AIImportError("Unsupported compose port mapping")


def _environment(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        result = {}
        for item in raw:
            if not isinstance(item, str):
                raise AIImportError("Compose environment list must contain strings")
            if "=" in item:
                k, v = item.split("=", 1)
            else:
                k, v = item, ""
            result[k] = v
        return result
    raise AIImportError("Unsupported compose environment")


def _volume(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        bits = raw.split(":")
        if len(bits) < 2:
            raise AIImportError("Compose volume needs source and target")
        source, target = bits[0], bits[1]
        mode = bits[2] if len(bits) > 2 else "rw"
        if source.startswith("/") or source == "." or source.startswith("./") or source.startswith("../"):
            # Host bind mounts are intentionally represented as a risk instead of silently becoming named volumes.
            return {"name": _clean_name(source.replace("/", "-"), "host-bind"), "container_path": target, "read_only": mode == "ro", "_host_bind": True, "_source": source}
        return {"name": _clean_name(source), "container_path": target, "read_only": mode == "ro"}
    if isinstance(raw, dict):
        source = raw.get("source") or raw.get("type")
        target = raw.get("target")
        if not source or not target:
            raise AIImportError("Compose volume object needs source and target")
        if str(raw.get("type", "volume")) == "bind":
            return {"name": _clean_name(str(source).replace("/", "-"), "host-bind"), "container_path": target, "read_only": bool(raw.get("read_only", False)), "_host_bind": True, "_source": source}
        return {"name": _clean_name(str(source)), "container_path": target, "read_only": bool(raw.get("read_only", False))}
    raise AIImportError("Unsupported compose volume")


def _parse_compose(raw: dict[str, Any], source_label: str = "compose") -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict) or not raw["services"]:
        raise AIImportError("Compose source must contain a non-empty services object")
    name = _clean_name(raw.get("name") or raw.get("x-servora-name") or source_label)
    services = []
    for service_name, service in raw["services"].items():
        if not isinstance(service, dict):
            raise AIImportError(f"Invalid compose service: {service_name}")
        image = service.get("image")
        if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
            raise AIImportError(f"Compose service '{service_name}' needs a concrete image")
        ports = [_port(x) for x in service.get("ports", [])]
        volumes = [_volume(x) for x in service.get("volumes", [])]
        # Host bind mounts cannot be represented safely by the current AppManifest volume model.
        for v in volumes:
            v.pop("_host_bind", None)
            v.pop("_source", None)
        networks = service.get("networks", [])
        if isinstance(networks, dict):
            networks = list(networks)
        depends = service.get("depends_on", [])
        if isinstance(depends, dict):
            depends = list(depends)
        command = service.get("command", [])
        if isinstance(command, str):
            command = [command]
        services.append({
            "name": _clean_name(str(service_name)),
            "image": image,
            "ports": ports,
            "volumes": volumes,
            "environment": _environment(service.get("environment")),
            "networks": [_clean_name(str(n)) for n in networks],
            "command": command,
            "depends_on": [_clean_name(str(d)) for d in depends],
        })
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": f"Imported from {source_label}",
        "metadata": {"source_type": "compose", "imported_by": "servora"},
        "services": services,
    }
    try:
        return manifest_to_dict(validate_app_manifest(manifest))
    except AppManifestError as exc:
        raise AIImportError(str(exc)) from exc


def manifest_from_image(image: str, *, name: str | None = None, port: int | None = None) -> dict[str, Any]:
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise AIImportError("Invalid OCI image reference")
    app_name = _clean_name(name or image.split("/")[-1].split(":")[0], "imported-app")
    service = {"name": "app", "image": image, "ports": [], "volumes": [], "environment": {}, "networks": [], "command": [], "depends_on": []}
    if port is not None:
        service["ports"] = [{"host": int(port), "container": int(port), "protocol": "tcp"}]
    return manifest_to_dict(validate_app_manifest({"name": app_name, "version": "1.0.0", "description": f"Imported OCI image {image}", "metadata": {"source_type": "oci_image"}, "services": [service]}))


def _url_finding(url: str) -> RiskFinding | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return RiskFinding("high", "unsafe_source_url", "Source URL must use HTTP(S) without embedded credentials.", "source.url")
    return None


def scan_risks(source: dict[str, Any], manifest: dict[str, Any]) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    if source.get("url"):
        f = _url_finding(str(source["url"]))
        if f:
            findings.append(f)
    original = source.get("raw", {})
    if isinstance(original, dict):
        for service_name, service in (original.get("services") or {}).items():
            if not isinstance(service, dict):
                continue
            base = f"services.{service_name}"
            if service.get("privileged") is True:
                findings.append(RiskFinding("critical", "privileged", "Privileged container requested.", f"{base}.privileged"))
            if str(service.get("network_mode", "")) == "host":
                findings.append(RiskFinding("high", "host_network", "Host networking bypasses container network isolation.", f"{base}.network_mode"))
            for mount in service.get("volumes", []) or []:
                text = json.dumps(mount).lower()
                if "docker.sock" in text:
                    findings.append(RiskFinding("critical", "docker_socket", "Docker/Podman socket access can control the host container engine.", f"{base}.volumes"))
                if isinstance(mount, str) and (mount.startswith("/") or mount.startswith("./") or mount.startswith("../")):
                    findings.append(RiskFinding("high", "host_mount", "Host bind mount detected; imported manifests cannot safely preserve it automatically.", f"{base}.volumes"))
            for dev in service.get("devices", []) or []:
                findings.append(RiskFinding("high", "host_device", "Host device access requested.", f"{base}.devices"))
            for cap in service.get("cap_add", []) or []:
                findings.append(RiskFinding("high", "capability", f"Additional Linux capability requested: {cap}.", f"{base}.cap_add"))
            if service.get("security_opt"):
                findings.append(RiskFinding("high", "security_opt", "Custom security options requested.", f"{base}.security_opt"))
            command = service.get("command")
            command_text = " ".join(command) if isinstance(command, list) else str(command or "")
            if any(re.search(rf"(^|\s){re.escape(x)}(\s|$)", command_text.lower()) for x in _DANGEROUS_COMMANDS):
                findings.append(RiskFinding("medium", "shell_command", "Shell command detected; review before installation.", f"{base}.command"))
            resources = service.get("deploy", {}).get("resources", {}) if isinstance(service.get("deploy"), dict) else {}
            limits = resources.get("limits", {}) if isinstance(resources, dict) else {}
            if not limits:
                findings.append(RiskFinding("low", "unbounded_resources", "No explicit resource limits were found.", f"{base}.deploy.resources.limits"))
    for service in manifest.get("services", []):
        for p in service.get("ports", []):
            if int(p["host"]) < 1024:
                findings.append(RiskFinding("medium", "privileged_port", "Host port below 1024 may require elevated privileges.", f"services.{service['name']}.ports"))
    return findings


def import_source(source: dict[str, Any]) -> ImportResult:
    if not isinstance(source, dict):
        raise AIImportError("Source must be an object")
    kind = source.get("type")
    raw = source.get("raw")
    if kind == "oci_image":
        manifest = manifest_from_image(str(source.get("image", "")), name=source.get("name"), port=source.get("port"))
    elif kind == "compose":
        if not isinstance(raw, dict):
            raise AIImportError("Compose import requires parsed JSON/YAML data in raw")
        manifest = _parse_compose(raw, str(source.get("name") or "compose-import"))
    elif kind == "manifest":
        if not isinstance(raw, dict):
            raise AIImportError("Manifest import requires an object")
        manifest = manifest_to_dict(validate_app_manifest(raw))
    else:
        raise AIImportError("Supported source types: oci_image, compose, manifest")
    findings = scan_risks(source, manifest)
    requires_approval = any(f.severity in {"critical", "high", "medium"} for f in findings)
    return ImportResult(manifest, {k: v for k, v in source.items() if k != "raw"}, findings, requires_approval)
