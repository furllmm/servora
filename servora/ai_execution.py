from __future__ import annotations

from typing import Any

from .ai import AIPlanError
from .ai_import import RiskFinding, scan_risks
from .apps import AppManifestError, validate_app_manifest
from .service import ensure_port_is_available


def plan_to_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    """Convert an AI create-container plan into a normal Servora manifest."""
    if not isinstance(plan, dict) or plan.get("action") != "create_container":
        raise AIPlanError("Unsupported plan")
    service = {
        "name": plan["name"],
        "image": plan["image"],
        "ports": plan.get("ports", []),
        "volumes": plan.get("volumes", []),
        "environment": plan.get("environment", {}),
        "networks": plan.get("networks", []),
        "command": plan.get("command", []),
    }
    return {
        "name": plan["name"],
        "version": "ai-plan-1",
        "description": str(plan.get("description", "AI-generated container")),
        "services": [service],
        "metadata": {"source": "ai-plan", "provider": plan.get("provider", "unknown")},
    }


def analyze_plan(plan: dict[str, Any]) -> tuple[dict[str, Any], list[RiskFinding], bool]:
    """Validate and risk-scan an AI plan without touching Podman."""
    manifest = plan_to_manifest(plan)
    try:
        from .apps import manifest_to_dict
        manifest = manifest_to_dict(validate_app_manifest(manifest))
    except AppManifestError as exc:
        raise AIPlanError(str(exc)) from exc

    raw_service = manifest["services"][0]
    raw = {"services": {raw_service["name"]: raw_service}}
    findings = scan_risks({"type": "ai_plan", "raw": raw}, manifest)
    requires_approval = any(f.severity in {"critical", "high", "medium"} for f in findings)
    return manifest, findings, requires_approval


def execute_plan(podman, manifest: dict[str, Any], check_ports: bool = True) -> dict[str, Any]:
    """Create and launch one AI-approved container through the restricted Podman API."""
    service = manifest["services"][0]
    container_name = f"servora-{manifest['name']}-{service['name']}"
    if check_ports:
        for port in service["ports"]:
            ensure_port_is_available(port["host"])

    created = False
    try:
        for network in sorted(set(service["networks"])):
            if not any((x.get("Name") or x.get("name")) == network for x in podman.list_networks()):
                podman.create_network(network)
        for volume in sorted({v["name"] for v in service["volumes"]}):
            if not any((x.get("Name") or x.get("name")) == volume for x in podman.list_volumes()):
                podman.create_volume(volume)

        result = podman.create_container({
            "name": container_name,
            "image": service["image"],
            "ports": service["ports"],
            "volumes": service["volumes"],
            "environment": service["environment"],
            "networks": service["networks"],
            "command": service["command"],
        })
        created = True
        start_result = podman.start_container(container_name)
        health = podman.container_health(container_name)
        return {"container": container_name, "created": result, "started": start_result, "health": health}
    except Exception:
        if created:
            try:
                podman.remove_container(container_name, force=True)
            except Exception:
                pass
        raise
