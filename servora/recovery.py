from __future__ import annotations

from typing import Any

from .podman import PodmanError


ALLOWED_ACTIONS = {"start", "stop", "restart"}


class RecoveryError(ValueError):
    pass


def validate_recovery_action(action: Any) -> dict[str, str]:
    if not isinstance(action, dict):
        raise RecoveryError("recovery action must be an object")
    name = action.get("action")
    if name not in ALLOWED_ACTIONS:
        raise RecoveryError("unsupported recovery action")
    reason = action.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 1000:
        raise RecoveryError("invalid recovery reason")
    return {"action": name, "reason": reason}


def execute_recovery(podman, container: str, action: dict[str, str]) -> dict[str, Any]:
    if not isinstance(container, str) or not container or len(container) > 128:
        raise RecoveryError("Invalid container name")
    checked = validate_recovery_action(action)
    try:
        fn = {
            "start": podman.start_container,
            "stop": podman.stop_container,
            "restart": podman.restart_container,
        }[checked["action"]]
        result = fn(container)
        health = podman.container_health(container)
    except (PodmanError, ValueError) as exc:
        raise RecoveryError(str(exc)) from exc
    return {
        "container": container,
        "action": checked["action"],
        "reason": checked["reason"],
        "result": result,
        "health": health,
    }
