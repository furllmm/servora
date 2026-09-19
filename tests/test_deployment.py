from servora.apps import AppManifest, AppService
from servora.deployment import (capture_app_state, image_status, check_app_updates,\n                                build_app_update_preview, update_app_images, load_app_state, save_app_state)


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


def test_digest_mismatch_wins_over_matching_image_id(tmp_path):
    p = DeploymentPodman("sha256:one")
    p.digest = "sha256:digest-one"
    capture_app_state(p, manifest(), tmp_path)
    p.digest = "sha256:digest-two"
    result = image_status(p, manifest(), tmp_path)
    assert result["status"] == "update_available"
    assert result["services"][0]["status"] == "update_available"


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


def multi_service_manifest():
    return AppManifest(
        name="stack",
        version="1.0.0",
        services=[
            AppService(name="db", image="postgres:old"),
            AppService(name="backend", image="backend:latest", depends_on=["db"]),
            AppService(name="frontend", image="frontend:latest", depends_on=["backend"]),
        ],
    )


class MultiServiceImageUpdatePodman:
    def __init__(self, fail_new_backend=False):
        self.fail_new_backend = fail_new_backend
        self.images = {
            "postgres:old": "sha256:db",
            "backend:latest": "sha256:backend-new",
            "frontend:latest": "sha256:frontend",
        }
        self.containers = {
            "servora-stack-db": {"image": "sha256:db", "running": True},
            "servora-stack-backend": {"image": "sha256:backend-old", "running": True},
            "servora-stack-frontend": {"image": "sha256:frontend", "running": True},
        }
        self.calls = []

    def inspect_container(self, name):
        item = self.containers.get(name)
        if item is None:
            raise RuntimeError("missing container")
        return {"Name": name, "Image": item["image"]}

    def image_metadata(self, name):
        image_id = self.images.get(name)
        if image_id is None:
            raise RuntimeError("missing image")
        return {
            "name": name,
            "id": image_id,
            "digest": "sha256:digest-" + image_id.split(":")[-1],
        }

    def container_health(self, name):
        item = self.containers.get(name)
        if item is None:
            raise RuntimeError("missing container")
        return {
            "name": name,
            "status": "running" if item["running"] else "stopped",
            "running": item["running"],
            "healthcheck": None,
        }

    def stop_container(self, name):
        self.calls.append(("stop", name))
        self.containers[name]["running"] = False
        return name

    def remove_container(self, name, force=False):
        self.calls.append(("remove", name))
        self.containers.pop(name, None)
        return name

    def create_container(self, plan):
        self.calls.append(("create", plan["name"], plan["image"]))
        if self.fail_new_backend and plan["image"] == "backend:latest":
            raise RuntimeError("new backend failed to create")
        self.containers[plan["name"]] = {"image": plan["image"], "running": False}
        return plan["name"]

    def start_container(self, name):
        self.calls.append(("start", name))
        self.containers[name]["running"] = True
        return name


def _capture_multi_state(p, root):
    capture_app_state(p, multi_service_manifest(), root)
    state = load_app_state(root)
    state["services"][1]["image_id"] = "sha256:backend-old"
    state["services"][1]["image_digest"] = "sha256:digest-backend-old"
    save_app_state(root, "stack", state)


def test_multi_service_update_recreates_only_changed_service(tmp_path):
    p = MultiServiceImageUpdatePodman()
    _capture_multi_state(p, tmp_path)

    result = update_app_images(p, multi_service_manifest(), tmp_path)

    assert result["status"] == "updated"
    assert result["updated_services"] == ["backend"]
    assert p.containers["servora-stack-db"]["image"] == "sha256:db"
    assert p.containers["servora-stack-frontend"]["image"] == "sha256:frontend"
    assert p.containers["servora-stack-backend"]["image"] == "backend:latest"
    assert p.containers["servora-stack-db"]["running"] is True
    assert p.containers["servora-stack-frontend"]["running"] is True
    assert p.containers["servora-stack-backend"]["running"] is True

    changed_calls = [
        call for call in p.calls
        if call[1] == "servora-stack-backend"
    ]
    assert changed_calls == [
        ("stop", "servora-stack-backend"),
        ("remove", "servora-stack-backend"),
        ("create", "servora-stack-backend", "backend:latest"),
        ("start", "servora-stack-backend"),
    ]


def test_multi_service_update_failure_restores_only_changed_service(tmp_path):
    p = MultiServiceImageUpdatePodman(fail_new_backend=True)
    _capture_multi_state(p, tmp_path)

    try:
        update_app_images(p, multi_service_manifest(), tmp_path)
    except RuntimeError as exc:
        assert "rollback succeeded" in str(exc)
    else:
        raise AssertionError("expected update failure")

    assert p.containers["servora-stack-backend"]["image"] == "sha256:backend-old"
    assert p.containers["servora-stack-db"]["image"] == "sha256:db"
    assert p.containers["servora-stack-frontend"]["image"] == "sha256:frontend"
    assert p.containers["servora-stack-db"]["running"] is True
    assert p.containers["servora-stack-frontend"]["running"] is True
    assert load_app_state(tmp_path)["services"][1]["image_id"] == "sha256:backend-old"


class ImageUpdatePodman:
    def __init__(self, image_id="sha256:two", fail_create=False):
        self.image_id = image_id
        self.fail_create = fail_create
        self.containers = {"servora-demo-web": {"image": "sha256:one", "running": True}}
        self.calls = []

    def inspect_container(self, name):
        item = self.containers.get(name)
        if item is None:
            raise RuntimeError("missing container")
        return {"Name": name, "Image": item["image"]}

    def image_metadata(self, name):
        return {"name": name, "id": self.image_id, "digest": "sha256:digest-" + self.image_id.split(":")[-1]}

    def container_health(self, name):
        item = self.containers.get(name)
        if item is None:
            raise RuntimeError("missing container")
        return {"name": name, "status": "running" if item["running"] else "stopped",
                "running": item["running"], "healthcheck": None}

    def stop_container(self, name):
        self.calls.append(("stop", name))
        self.containers[name]["running"] = False
        return name

    def remove_container(self, name, force=False):
        self.calls.append(("remove", name))
        self.containers.pop(name, None)
        return name

    def create_container(self, plan):
        self.calls.append(("create", plan["image"]))
        if self.fail_create and plan["image"] == self.image_id:
            raise RuntimeError("new image failed to create")
        self.containers[plan["name"]] = {"image": plan["image"], "running": False}
        return plan["name"]

    def start_container(self, name):
        self.calls.append(("start", name))
        self.containers[name]["running"] = True
        return name


def test_image_update_recreates_only_changed_service_and_records_new_image(tmp_path):
    p = ImageUpdatePodman()
    capture_app_state(p, manifest(), tmp_path)
    result = update_app_images(p, manifest(), tmp_path)
    assert result["status"] == "updated"
    assert result["updated_services"] == ["web"]
    assert p.containers["servora-demo-web"]["image"] == "sha256:two"
    assert p.containers["servora-demo-web"]["running"] is True
    assert load_app_state(tmp_path)["services"][0]["image_id"] == "sha256:two"


def test_image_update_failure_rolls_back_exact_old_image(tmp_path):
    p = ImageUpdatePodman(fail_create=True)
    capture_app_state(p, manifest(), tmp_path)
    try:
        update_app_images(p, manifest(), tmp_path)
    except RuntimeError as exc:
        assert "rollback succeeded" in str(exc)
    else:
        raise AssertionError("expected update failure")
    assert p.containers["servora-demo-web"]["image"] == "sha256:one"
    assert p.containers["servora-demo-web"]["running"] is True
    assert load_app_state(tmp_path)["services"][0]["image_id"] == "sha256:one"


def test_image_update_current_is_noop(tmp_path):
    p = ImageUpdatePodman(image_id="sha256:one")
    capture_app_state(p, manifest(), tmp_path)
    result = update_app_images(p, manifest(), tmp_path)
    assert result["status"] == "unchanged"
    assert result["updated_services"] == []
    assert p.calls == []
