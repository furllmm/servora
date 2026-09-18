from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .recovery import execute_recovery, validate_recovery_action


class RecoveryPolicyError(ValueError):
    pass


DEFAULT_POLICY = {
    "enabled": False,
    "rules": {
        "unhealthy": {"action": "restart", "max_attempts": 2, "cooldown_seconds": 300},
        "exited": {"action": "start", "max_attempts": 1, "cooldown_seconds": 300},
    },
}


class RecoveryPolicyStore:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "config" / "recovery_policy.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(DEFAULT_POLICY))
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryPolicyError("Recovery policy is corrupted") from exc
        return validate_policy(value)

    def save(self, policy: dict[str, Any]) -> dict[str, Any]:
        checked = validate_policy(policy)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(checked, indent=2) + "\n", encoding="utf-8")
        return checked


def validate_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise RecoveryPolicyError("policy must be an object")
    enabled = policy.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RecoveryPolicyError("enabled must be boolean")
    rules = policy.get("rules", {})
    if not isinstance(rules, dict):
        raise RecoveryPolicyError("rules must be an object")
    checked = {"enabled": enabled, "rules": {}}
    for state, raw in rules.items():
        if state not in {"unhealthy", "exited", "dead"}:
            raise RecoveryPolicyError("unsupported recovery state")
        if not isinstance(raw, dict):
            raise RecoveryPolicyError("rule must be an object")
        action = validate_recovery_action({"action": raw.get("action"), "reason": "policy"})
        attempts = raw.get("max_attempts", 1)
        cooldown = raw.get("cooldown_seconds", 300)
        if not isinstance(attempts, int) or not 0 <= attempts <= 5:
            raise RecoveryPolicyError("max_attempts must be between 0 and 5")
        if not isinstance(cooldown, int) or not 0 <= cooldown <= 86400:
            raise RecoveryPolicyError("cooldown_seconds must be between 0 and 86400")
        checked["rules"][state] = {
            "action": action["action"],
            "max_attempts": attempts,
            "cooldown_seconds": cooldown,
        }
    return checked


class RecoveryController:
    def __init__(self, podman, root: str | Path):
        self.podman = podman
        self.store = RecoveryPolicyStore(root)
        self.state_path = Path(root) / "metadata" / "recovery_state.json"

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def evaluate(self, name: str, approved: bool = False) -> dict[str, Any]:
        policy = self.store.load()
        health = self.podman.container_health(name)
        state = health.get("status", "unknown")
        rule = policy["rules"].get(state)
        result: dict[str, Any] = {"enabled": policy["enabled"], "state": state, "action": None, "status": "no_action"}
        if not policy["enabled"] or not rule:
            return result
        if not approved:
            result.update({"status": "approval_required", "action": rule["action"]})
            return result

        now = int(time.time())
        key = f"{name}:{state}:{rule['action']}"
        previous = self._state().get(key, {"attempts": 0, "last": 0})
        if previous["attempts"] >= rule["max_attempts"]:
            result["status"] = "max_attempts_reached"
            return result
        if now - previous["last"] < rule["cooldown_seconds"]:
            result["status"] = "cooldown"
            return result

        action = {"action": rule["action"], "reason": f"automatic recovery policy for {state}"}
        output = execute_recovery(self.podman, name, action)
        state_data = self._state()
        state_data[key] = {"attempts": previous["attempts"] + 1, "last": now}
        self._save_state(state_data)
        result.update({"status": "recovered", "action": action, "result": output})
        return result
