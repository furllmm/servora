from servora.apps import AppManifest, AppService
from servora.deployment import capture_app_state, image_status, check_app_updates, build_app_update_preview


class DeploymentPodman:
    def __init__(self, image_id="sha256:one"):
        self.image_id = image_id

    def inspect_container(self, name):
        return {"Name": name, "Image": self.image_id}

    def image_metadata(self, name):
        return {"name": name, "id": self.image_id, "digest": getattr(self, "digest", "sha256:digest-" + self.image_id.split(":")[-1])}


def manifest():
    return AppManifest(
        name="demo",
        version="1.0.0",
        services=[AppService(name="web", image="nginx:latest")],
    )


def test_capture_and_current_status(tmp_path):
    p = DeploymentPodman()
    state = capture_app_state(p, manifest(), tmp_path)
    assert state["services"][0]["image_id"] == "sha256:one"
    assert image_status(p, manifest(), tmp_path)["status"] == "current"


def test_image_status_detects_local_image_change(tmp_path):
    p = DeploymentPodman("sha256:one")
    capture_app_state(p, manifest(), tmp_path)
    p.image_id = "sha256:two"
    result = image_status(p, manifest(), tmp_path)
    assert result["status"] == "update_available"
    assert result["services"][0]["recorded_image_id"] == "sha256:one"
    assert result["services"][0]["current_image_id"] == "sha256:two"
    assert result["services"][0]["recorded_image_digest"] == "sha256:digest-one"


def test_unrecorded_app_is_not_claimed_current(tmp_path):
    result = image_status(DeploymentPodman(), manifest(), tmp_path)
    assert result["status"] == "not_recorded"
    assert result["services"][0]["status"] == "not_recorded"

def test_image_digest_is_persisted(tmp_path):
    state = capture_app_state(DeploymentPodman(), manifest(), tmp_path)
    assert state["services"][0]["image_digest"] == "sha256:digest-one"


def test_matching_digest_can_keep_status_current(tmp_path):
    p = DeploymentPodman("sha256:one")
    capture_app_state(p, manifest(), tmp_path)
    p.image_id = "sha256:two"
    p.digest = "sha256:digest-one"
    result = image_status(p, manifest(), tmp_path)
    assert result["status"] == "current"
    assert result["services"][0]["status"] == "current"

def test_check_app_updates_refreshes_unique_images_without_redeploying(tmp_path):
    class RefreshPodman(DeploymentPodman):
        def __init__(self):
            super().__init__()
            self.refresh_calls = []

        def refresh_image(self, name):
            self.refresh_calls.append(name)
            return {"name": name, "status": "unchanged", "changed": False}

    p = RefreshPodman()
    capture_app_state(p, manifest(), tmp_path)
    result = check_app_updates(p, manifest(), tmp_path)
    assert p.refresh_calls == ["nginx:latest"]
    assert result["status"] == "current"
    assert result["deployment_recorded"] is True


def test_update_preview_is_read_only_and_marks_changed_image(tmp_path):
    p = DeploymentPodman("sha256:one")
    capture_app_state(p, manifest(), tmp_path)
    p.image_id = "sha256:two"
    result = build_app_update_preview(p, manifest(), tmp_path)
    assert result["requires_update"] is True
    assert result["update_service_count"] == 1
    assert result["services"][0]["action"] == "recreate"
    assert result["services"][0]["status"] == "update_available"
    assert load_deployment_state(tmp_path)["services"][0]["image_id"] == "sha256:one"


def load_deployment_state(root):
    import json
    from pathlib import Path
    return json.loads((Path(root) / "metadata" / "deployments" / "demo.json").read_text())
