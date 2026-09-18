from servora.apps import AppManifest, AppService
from servora.deployment import capture_app_state, image_status


class DeploymentPodman:
    def __init__(self, image_id="sha256:one"):
        self.image_id = image_id

    def inspect_container(self, name):
        return {"Name": name, "Image": self.image_id}

    def image_metadata(self, name):
        return {"name": name, "id": self.image_id}


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


def test_unrecorded_app_is_not_claimed_current(tmp_path):
    result = image_status(DeploymentPodman(), manifest(), tmp_path)
    assert result["status"] == "current"
    assert result["services"][0]["status"] == "not_recorded"
