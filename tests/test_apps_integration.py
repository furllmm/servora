"""Real Podman integration tests for Servora app lifecycle."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from servora.apps import install_app, uninstall_app, validate_app_manifest
from servora.podman import Podman, PodmanError
from servora.runtime import Runtime
from servora.deployment import capture_app_state, image_status, load_deployment_state


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

        parsed = validate_app_manifest(manifest)
        state = capture_app_state(podman, parsed, runtime.root)
        assert state["services"][0]["image_id"]

        status = image_status(podman, parsed, runtime.root)
        assert status["status"] == "current"
        assert status["services"][0]["status"] == "current"

        uninstall = uninstall_app(podman, manifest)
        assert uninstall
        with pytest.raises(PodmanError):
            podman.inspect_container(container)

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


def _run_raw(podman: Podman, *args: str) -> str:
    result = podman._run(*args)
    return result.stdout.strip()


def test_real_image_update_recreates_container_and_records_new_image(tmp_path: Path):
    """Exercise deployment update against two real locally pulled images."""
    podman = _podman()
    from servora.deployment import update_app_images

    old_image = os.environ.get("SERVORA_OLD_IMAGE", "docker.io/library/alpine:3.20")
    new_image = os.environ.get("SERVORA_NEW_IMAGE", "docker.io/library/alpine:3.21")
    app = f"update-it-{uuid.uuid4().hex[:10]}"
    local_tag = f"localhost/servora-it-{uuid.uuid4().hex[:12]}:latest"
    container = f"servora-{app}-web"

    manifest = {
        "name": app,
        "version": "1.0",
        "description": "real image update integration test",
        "services": [
            {
                "name": "web",
                "image": local_tag,
                "command": ["sh", "-c", "echo servora-update && sleep 60"],
            }
        ],
    }
    runtime = Runtime(tmp_path / "runtime", mode="user")
    runtime.initialize()

    try:
        if not podman.image_exists(old_image):
            podman.pull_image(old_image)
        if not podman.image_exists(new_image):
            podman.pull_image(new_image)

        _run_raw(podman, "tag", old_image, local_tag)
        result = install_app(podman, manifest, transaction_root=runtime.root)
        assert result[0]["status"] == "created"

        parsed = validate_app_manifest(manifest)
        podman.start_container(container)
        old_state = capture_app_state(podman, parsed, runtime.root)
        old_id = old_state["services"][0]["image_id"]

        _run_raw(podman, "tag", new_image, local_tag)
        current = podman.image_metadata(local_tag)
        assert current["id"] != old_id

        status = image_status(podman, parsed, runtime.root)
        assert status["status"] == "update_available"

        update = update_app_images(podman, parsed, runtime.root)
        assert update["status"] == "updated"
        assert update["updated_services"] == ["web"]

        health = podman.container_health(container)
        assert health["running"] is True
        assert "servora-update" in podman.container_logs(container, tail=20)

        new_state = capture_app_state(podman, parsed, runtime.root)
        assert new_state["services"][0]["image_id"] != old_id
    finally:
        try:
            podman.remove_container(container, force=True)
        except Exception:
            pass
        try:
            podman.remove_image(local_tag, force=True)
        except Exception:
            pass


def test_real_image_update_failure_rolls_back_to_recorded_image(tmp_path: Path):
    """Force the new image to exit and verify real rollback to the old ID."""
    podman = _podman()
    from servora.deployment import update_app_images

    old_image = os.environ.get("SERVORA_OLD_IMAGE", "docker.io/library/alpine:3.20")
    new_image = os.environ.get("SERVORA_NEW_IMAGE", "docker.io/library/alpine:3.21")
    app = f"rollback-it-{uuid.uuid4().hex[:10]}"
    local_tag = f"localhost/servora-rollback-{uuid.uuid4().hex[:12]}:latest"
    container = f"servora-{app}-web"

    manifest = {
        "name": app,
        "version": "1.0",
        "description": "real rollback integration test",
        "services": [
            {
                "name": "web",
                "image": local_tag,
                "command": [
                    "sh", "-c",
                    "case \"$(cat /etc/alpine-release)\" in "
                    "3.21*) echo servora-new-failure; exit 1;; "
                    "*) echo servora-old-running; sleep 60;; "
                    "esac",
                ],
            }
        ],
    }
    runtime = Runtime(tmp_path / "runtime", mode="user")
    runtime.initialize()

    try:
        if not podman.image_exists(old_image):
            podman.pull_image(old_image)
        if not podman.image_exists(new_image):
            podman.pull_image(new_image)

        _run_raw(podman, "tag", old_image, local_tag)
        parsed = validate_app_manifest(manifest)
        install_app(podman, manifest, transaction_root=runtime.root)
        podman.start_container(container)

        old_state = capture_app_state(podman, parsed, runtime.root)
        old_id = old_state["services"][0]["image_id"]
        assert old_id

        _run_raw(podman, "tag", new_image, local_tag)
        assert podman.image_metadata(local_tag)["id"] != old_id
        assert image_status(podman, parsed, runtime.root)["status"] == "update_available"

        with pytest.raises(RuntimeError, match="rollback succeeded"):
            update_app_images(podman, parsed, runtime.root)

        restored = podman.inspect_container(container)
        assert restored["Image"] == old_id
        health = podman.container_health(container)
        assert health["running"] is True
        assert "servora-old-running" in podman.container_logs(container, tail=20)

        # Failed update must not replace the persisted deployment state.
        recorded = load_deployment_state(runtime.root, parsed.name)
        assert recorded["services"][0]["image_id"] == old_id
    finally:
        try:
            podman.remove_container(container, force=True)
        except Exception:
            pass
        try:
            podman.remove_image(local_tag, force=True)
        except Exception:
            pass
