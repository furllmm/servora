"""Real Podman integration tests for Servora app lifecycle."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from servora.apps import install_app, uninstall_app
from servora.podman import Podman, PodmanError
from servora.runtime import Runtime
from servora.deployment import capture_app_state, image_status


pytestmark = pytest.mark.integration


def _podman() -> Podman:
    executable = os.environ.get("SERVORA_PODMAN", "podman")
    if not shutil.which(executable):
        pytest.skip(f"Podman executable not found: {executable}")
    if os.environ.get("SERVORA_INTEGRATION") != "1":
        pytest.skip("set SERVORA_INTEGRATION=1 to run real Podman integration tests")
    podman = Podman(executable=executable)
    try:
        podman.version()
    except (PodmanError, OSError) as exc:
        pytest.skip(f"Podman is unavailable: {exc}")
    return podman


def test_real_servora_app_install_state_and_uninstall(tmp_path: Path):
    podman = _podman()
    image = os.environ.get("SERVORA_TEST_IMAGE", "docker.io/library/alpine:3.20")
    app = f"it-{uuid.uuid4().hex[:10]}"
    volume = f"servora-{app}-data"
    network = f"servora-{app}-net"
    manifest = {
        "name": app,
        "version": "1.0",
        "description": "Servora Podman integration test",
        "services": [
            {
                "name": "web",
                "image": image,
                "volumes": [{"name": volume, "container_path": "/data"}],
                "networks": [network],
                "command": ["sh", "-c", "echo servora-app-integration && sleep 60"],
            }
        ],
    }
    runtime = Runtime(tmp_path / "runtime", mode="user")
    runtime.initialize()
    container = f"servora-{app}-web"

    try:
        result = install_app(
            podman,
            manifest,
            transaction_root=runtime.root,
        )
        assert result[0]["status"] == "created"
        assert result[0]["images"][0]["status"] in {"present", "pulled"}

        health = podman.container_health(container)
        assert health["status"] == "created"

        podman.start_container(container)
        health = podman.container_health(container)
        assert health["running"] is True
        assert "servora-app-integration" in podman.container_logs(container, tail=20)

        # Deployment capture must work against real inspect/image metadata.
        parsed = __import__("servora.apps", fromlist=["validate_app_manifest"]).validate_app_manifest(manifest)
        state = capture_app_state(podman, parsed, runtime.root)
        assert state["services"][0]["image_id"]

        status = image_status(podman, parsed, runtime.root)
        assert status["status"] == "current"
        assert status["services"][0]["status"] == "current"

        uninstall = uninstall_app(podman, manifest)
        assert uninstall
        with pytest.raises(PodmanError):
            podman.inspect_container(container)

        # uninstall preserves named volumes by design.
        assert podman.inspect_volume(volume)
        assert podman.inspect_network(network)
    finally:
        try:
            podman.remove_container(container, force=True)
        except Exception:
            pass
        try:
            podman.remove_volume(volume, force=True)
        except Exception:
            pass
        try:
            podman.remove_network(network)
        except Exception:
            pass
