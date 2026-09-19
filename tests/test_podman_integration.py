"""Opt-in integration tests for a real rootless Podman installation.

Run with:
    SERVORA_INTEGRATION=1 pytest -q tests/test_podman_integration.py

These tests intentionally use the small official Alpine image and never remove
images, so a local image cache can be reused. Containers, volumes and networks
created by the tests are always cleaned up.
"""

from __future__ import annotations

import os
import shutil
import uuid

import pytest

from servora.podman import Podman, PodmanError


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


def test_real_podman_image_and_container_lifecycle():
    podman = _podman()
    image = os.environ.get("SERVORA_TEST_IMAGE", "docker.io/library/alpine:3.20")
    suffix = uuid.uuid4().hex[:12]
    container = f"servora-it-{suffix}"
    volume = f"servora-it-vol-{suffix}"
    network = f"servora-it-net-{suffix}"

    created_container = False
    created_volume = False
    created_network = False
    try:
        if not podman.image_exists(image):
            podman.pull_image(image)

        metadata = podman.image_metadata(image)
        assert metadata["name"] == image
        assert metadata["id"]
        assert metadata["size"] is not None

        podman.create_volume(volume)
        created_volume = True
        assert podman.inspect_volume(volume)

        podman.create_network(network)
        created_network = True
        assert podman.inspect_network(network)

        podman.create_container(
            {
                "name": container,
                "image": image,
                "volumes": [
                    {"name": volume, "container_path": "/data"},
                ],
                "networks": [network],
                "command": ["sh", "-c", "echo servora-integration && sleep 30"],
            }
        )
        created_container = True

        health_before = podman.container_health(container)
        assert health_before["status"] == "created"

        podman.start_container(container)
        running = podman.container_health(container)
        assert running["running"] is True
        assert running["status"] == "running"

        logs = podman.container_logs(container, tail=20)
        assert "servora-integration" in logs

        podman.stop_container(container)
        stopped = podman.container_health(container)
        assert stopped["status"] in {"exited", "stopped"}
    finally:
        if created_container:
            try:
                podman.remove_container(container, force=True)
            except PodmanError:
                pass
        if created_network:
            try:
                podman.remove_network(network)
            except PodmanError:
                pass
        if created_volume:
            try:
                podman.remove_volume(volume, force=True)
            except PodmanError:
                pass
