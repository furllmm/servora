from pathlib import Path

from servora.apps import validate_app_manifest
from servora.reliability import preflight_manifest, validate_runtime_state


class FakePodman:
    def list_images(self):
        return [{"RepoTags": ["nginx:alpine"]}]


def test_preflight_detects_missing_local_image(tmp_path: Path):
    m = validate_app_manifest({"name": "demo", "services": [{"name": "web", "image": "nginx:missing"}]})
    result = preflight_manifest(FakePodman(), m, tmp_path, check_images=True)
    assert result["ok"] is True
    assert any(x["code"] == "image_not_local" for x in result["findings"])


def test_runtime_state_is_valid(tmp_path: Path):
    for name in ["config", "metadata", "logs", "podman", "apps", "backups"]:
        (tmp_path / name).mkdir()
    (tmp_path / "config" / "servora.json").write_text('{"version": 1, "mode": "user"}', encoding="utf-8")
    assert validate_runtime_state(tmp_path)["ok"] is True


def test_runtime_state_detects_missing_path(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "servora.json").write_text('{"version": 1}', encoding="utf-8")
    result = validate_runtime_state(tmp_path)
    assert result["ok"] is False
    assert any(x["code"] == "missing_runtime_path" for x in result["findings"])
