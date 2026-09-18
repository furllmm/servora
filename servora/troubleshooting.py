from __future__ import annotations

from typing import Any

from .ai import AIProvider
from .ai_execution import analyze_plan
from .podman import PodmanError


class TroubleshootingError(ValueError):
    pass


def collect_container_diagnostics(podman, name: str, tail: int = 200) -> dict[str, Any]:
    """Collect bounded, non-mutating diagnostics for an existing container."""
    if not isinstance(name, str) or not name or len(name) > 128:
        raise TroubleshootingError("Invalid container name")
    if not isinstance(tail, int) or tail < 0 or tail > 1000:
        raise TroubleshootingError("tail must be between 0 and 1000")
    try:
        health = podman.container_health(name)
        inspect = podman.inspect_container(name)
        logs = podman.container_logs(name, tail)
    except (PodmanError, ValueError) as exc:
        raise TroubleshootingError(str(exc)) from exc
    return {
        "name": name,
        "health": health,
        "inspect": inspect,
        "logs": logs,
    }


def build_troubleshooting_prompt(diagnostics: dict[str, Any]) -> str:
    """Create a bounded prompt that asks for diagnosis, not arbitrary execution."""
    return (
        "Diagnose this Servora container. Return ONLY JSON with keys "
        "summary, findings, recommendations, confidence, actions. The actions list may contain only start, stop, or restart with a reason. Do not return shell commands "
        "or request privileged host access. Recommendations must be safe, reversible, "
        "and require user approval before any mutation.\n\n"
        + str(diagnostics)[:50000]
    )


def troubleshoot_container(provider: AIProvider, diagnostics: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(diagnostics, dict):
        raise TroubleshootingError("diagnostics must be an object")
    try:
        result = provider.generate(build_troubleshooting_prompt(diagnostics))
    except Exception as exc:
        raise TroubleshootingError(str(exc)) from exc
    required = {"summary", "findings", "recommendations", "confidence"}
    if not required.issubset(result):
        raise TroubleshootingError("AI diagnosis is missing required fields")
    if not isinstance(result["findings"], list) or not isinstance(result["recommendations"], list):
        raise TroubleshootingError("AI diagnosis lists are invalid")
    actions = result.get("actions", [])
    if not isinstance(actions, list):
        raise TroubleshootingError("AI diagnosis actions are invalid")
    allowed = {"start", "stop", "restart"}
    for action in actions:
        if not isinstance(action, dict) or action.get("action") not in allowed:
            raise TroubleshootingError("AI diagnosis contains an unsupported recovery action")
        if "reason" in action and (not isinstance(action["reason"], str) or len(action["reason"]) > 1000):
            raise TroubleshootingError("AI diagnosis contains an invalid recovery reason")
    return result
