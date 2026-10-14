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


def _podman_with_env(env: dict[str, str]) -> "Podman":
    executable = os.environ.get("SERVORA_PODMAN", "podman")
    if not shutil.which(executable):
        pytest.skip(f"Podman executable not found: {executable}")
    if os.environ.get("SERVORA_INTEGRATION") != "1":
        pytest.skip("set SERVORA_INTEGRATION=1 to run real Podman integration tests")
    podman = Podman(executable=executable, env=env)
    try:
        podman.version()
    except (PodmanError, OSError) as exc:
        pytest.skip(f"Podman is unavailable: {exc}")
    return podman


def test_portable_storage_conf_is_honoured_by_real_podman(tmp_path):
    """CONTAINERS_STORAGE_CONF must redirect graphroot/runroot/driver of a
    REAL podman invocation to the Servora portable directory."""
    from servora.runtime import Runtime

    runtime = Runtime(tmp_path / "Servora-it", mode="portable")
    runtime.initialize()
    podman = _podman_with_env(runtime.podman_environment())

    info = podman.info()
    store = info.get("store", {})
    assert store.get("GraphRoot") == str(runtime.storage_root), store.get("GraphRoot")
    assert store.get("RunRoot") == str(runtime.run_root), store.get("RunRoot")
    assert store.get("GraphDriverName") in {"overlay", "vfs"}

    # Named volume data must physically live inside the portable graphroot.
    suffix = uuid.uuid4().hex[:8]
    volume = f"servora-portable-{suffix}"
    created = False
    try:
        podman.create_volume(volume)
        created = True
        mountpoint = podman.inspect_volume(volume).get("Mountpoint", "")
        assert mountpoint.startswith(str(runtime.storage_root)), mountpoint
    finally:
        if created:
            try:
                podman.remove_volume(volume, force=True)
            except PodmanError:
                pass


def test_normal_mode_uses_system_storage_not_portable(tmp_path):
    """User-mode Podman (no env overrides) must keep using the system storage
    locations; Servora portable config must never leak into it."""
    podman = _podman_with_env({})
    info = podman.info()
    store = info.get("store", {})
    graphroot = store.get("GraphRoot", "")
    assert graphroot  # sane defaults, untouched by Servora
    assert "Servora" not in graphroot and ".servora" not in graphroot


def test_moved_portable_root_reconfigures_and_podman_follows(tmp_path):
    """After moving an EMPTY portable root, regenerated conf must make real
    podman use the NEW location."""
    from servora.runtime import Runtime

    old = tmp_path / "move-src"
    rt = Runtime(old, mode="portable")
    rt.initialize()
    shutil.copytree(old, tmp_path / "move-dst")
    moved = Runtime(tmp_path / "move-dst", mode="portable")
    moved.initialize()  # old path still exists but is empty -> safe rewrite
    podman = _podman_with_env(moved.podman_environment())
    store = podman.info().get("store", {})
    assert store.get("GraphRoot") == str(moved.storage_root)
