import pytest

from servora.ai import AIPlanError, MockAIProvider, create_container_plan
from servora.ai_execution import analyze_plan, execute_plan, plan_to_manifest


def test_plan_becomes_normal_servora_manifest():
    plan = create_container_plan(MockAIProvider(), "nginx")
    manifest = plan_to_manifest(plan)
    checked, findings, approval = analyze_plan(plan)
    assert manifest["services"][0]["image"] == "nginx:alpine"
    assert checked["name"] == "example"
    assert approval is False
    assert isinstance(findings, list)


def test_forbidden_host_access_is_rejected():
    plan = create_container_plan(MockAIProvider(), "nginx")
    plan["privileged"] = True
    from servora.ai import validate_plan
    with pytest.raises(AIPlanError):
        validate_plan(plan)


class FakePodman:
    def __init__(self):
        self.created = []
        self.started = []
        self.removed = []

    def list_networks(self):
        return []

    def list_volumes(self):
        return []

    def create_network(self, name):
        return name

    def create_volume(self, name):
        return name

    def create_container(self, plan):
        self.created.append(plan)
        return "created-id"

    def start_container(self, name):
        self.started.append(name)
        return "started"

    def remove_container(self, name, force=False):
        self.removed.append((name, force))

    def inspect_container(self, name):
        return {"State": {"Status": "running"}}


def test_execute_uses_restricted_podman_flow(monkeypatch):
    plan = create_container_plan(MockAIProvider(), "nginx")
    manifest, findings, approval = analyze_plan(plan)
    fake = FakePodman()
    monkeypatch.setattr("servora.ai_execution.ensure_port_is_available", lambda port: None)
    result = execute_plan(fake, manifest, check_ports=True)
    assert result["container"] == "servora-example-example"
    assert fake.created[0]["image"] == "nginx:alpine"
    assert fake.started == ["servora-example-example"]


def test_execute_reports_running_health():
    plan = create_container_plan(MockAIProvider(), "nginx")
    manifest, _, _ = analyze_plan(plan)
    fake = FakePodman()
    result = execute_plan(fake, manifest, check_ports=False)
    assert result["health"]["status"] == "running"
    assert result["health"]["running"] is True
